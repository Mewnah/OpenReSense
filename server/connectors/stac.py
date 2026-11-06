"""Generic STAC API client serving several public catalogs (one connector id per endpoint)."""
import json
import time
from urllib.parse import urlsplit

import httpx

from .base import Query, asset, page, result
from .base import bounds as _bounds, query_bbox, rect as _rect, vocab as _vocab
from ._util import check_times, json_of

EARTH = ["earth"]
ENDPOINTS = {
    "stac:earth-search": dict(
        url="https://earth-search.aws.element84.com/v1", name="Earth Search (Element 84)",
        provider="Element 84 / AWS Open Data", targets=EARTH, home="https://element84.com/earth-search/",
        description="Open-access COGs on AWS: Sentinel-2 L2A/C1, Sentinel-1 GRD, Landsat C2 L2, NAIP, Copernicus DEM."),
    "stac:planetary-computer": dict(
        url="https://planetarycomputer.microsoft.com/api/stac/v1", name="Microsoft Planetary Computer",
        provider="Microsoft", targets=EARTH, home="https://planetarycomputer.microsoft.com/catalog",
        description="~140 Earth datasets (Landsat, Sentinel, MODIS, NAIP, DEMs, land cover ...) as COG/Zarr on Azure. "
                    "Asset URLs are returned unsigned; the backend signs them when opening.",
        notes=["Planetary Computer asset URLs are unsigned; OpenReSense signs them when you open an asset."]),
    "stac:cdse": dict(
        url="https://stac.dataspace.copernicus.eu/v1", name="Copernicus Data Space Ecosystem",
        provider="ESA / European Commission", targets=EARTH, home="https://dataspace.copernicus.eu/",
        description="Official ESA/Copernicus archive: Sentinel-1/2/3/5P, Copernicus Contributing Missions, DEMs, land products.",
        notes=["Downloading CDSE data needs a free Copernicus Data Space account."]),
    "stac:usgs-astro": dict(
        url="https://stac.astrogeology.usgs.gov/api", name="USGS Astrogeology STAC",
        provider="USGS Astrogeology Science Center", targets=["moon", "mars", "europa"],
        home="https://stac.astrogeology.usgs.gov/",
        description="Planetary cloud-optimized products: controlled CTX/HiRISE/THEMIS (Mars), Kaguya TC and LOLA (Moon), "
                    "Galileo and Voyager (Europa).",
        notes=["Some planetary footprints use 0-360 longitudes; they are converted to -180..180."]),
    "stac:dea": dict(
        url="https://explorer.dea.ga.gov.au/stac", name="Digital Earth Australia",
        provider="Geoscience Australia", targets=EARTH, sort_newest=True, flat_geometry=True, home="https://www.dea.ga.gov.au/",
        description="Landsat and Sentinel analysis-ready data, water and land-cover products over Australia."),
    "stac:deafrica": dict(
        url="https://explorer.digitalearth.africa/stac", name="Digital Earth Africa",
        provider="Digital Earth Africa", targets=EARTH, sort_newest=True, flat_geometry=True, home="https://www.digitalearthafrica.org/",
        description="Sentinel-2, Landsat, Sentinel-1 and derived services over Africa.",
        notes=["The Digital Earth Africa catalog endpoint is flaky; the collection list is rebuilt from the root catalog "
               "(ids only, no titles)."]),
    "stac:usgs-landsat": dict(
        url="https://landsatlook.usgs.gov/stac-server", name="USGS Landsat (LandsatLook)",
        provider="USGS EROS", targets=EARTH, home="https://landsatlook.usgs.gov/",
        description="Complete Landsat Collection 2 archive (Level-1/2, ARD, Level-3) with browse images.",
        notes=["Landsat files are in a requester-pays S3 bucket; direct downloads need a USGS EROS login."]),
}

CONNECTORS = [{"id": cid, "name": e["name"], "provider": e["provider"], "targets": e["targets"],
               "levels": ["collection", "product"], "needs_collection": True, "supports": ["text", "bbox", "time"],
               "url": e["home"], "description": e["description"]} for cid, e in ENDPOINTS.items()]

TIMEOUT = 25  # stay inside the 30 s connector budget (Explorer servers can hang)
CACHE_TTL = 6 * 3600
MAX_PAGES = 20  # hops when paging deep into a result set
_collections_cache: dict[str, tuple[float, list]] = {}
_next_links: dict = {}  # (conn_id, request body json, offset) -> (method, url, body) of the page starting there


# ---- helpers ---------------------------------------------------------------------------------

def _rfc3339(value: str | None, end=False):
    if value and len(value) == 10:  # a bare date
        return value + ("T23:59:59Z" if end else "T00:00:00Z")
    return value


def _valid_bbox(b):
    """Normalise a STAC bbox to [W,S,E,N] in -180..180 degrees (W>E = antimeridian), or None if it is not degrees."""
    if not b:
        return None
    w, s, e, n = (b[0], b[1], b[2], b[3]) if len(b) == 4 else (b[0], b[1], b[3], b[4])
    s, n = min(s, n), max(s, n)  # Planetary Computer publishes some global extents with S and N swapped
    if abs(s) > 90 or abs(n) > 90 or w < -360 or e > 360:
        return None
    if e - w >= 360:
        return [-180, s, 180, n]
    if w < -180:  # unwrapped west of the antimeridian
        return [w + 360, s, e, n]
    if e > 180:  # 0..360 convention
        return [w - 360, s, e - 360, n] if w >= 180 else [w, s, e - 360, n]
    return [w, s, e, n]


def _spans(w, e):
    return [(w, e)] if w <= e else [(w, 180), (-180, e)]


def _overlap(a, b) -> bool:
    return any(x0 <= y1 and y0 <= x1 for x0, x1 in _spans(a[0], a[2]) for y0, y1 in _spans(b[0], b[2])) \
        and a[1] <= b[3] and b[1] <= a[3]


def _clip_strip(ring, lo, hi):
    """Sutherland-Hodgman clip of a closed ring to lo <= x <= hi."""
    pts = ring[:-1]
    for edge, inside in ((lo, lambda x: x >= lo), (hi, lambda x: x <= hi)):
        out = []
        for a, b in zip(pts[-1:] + pts[:-1], pts):
            cut = [edge, a[1] + (b[1] - a[1]) * (edge - a[0]) / (b[0] - a[0])] if a[0] != b[0] else None
            if inside(b[0]):
                if not inside(a[0]):
                    out.append(cut)
                out.append(b)
            elif inside(a[0]):
                out.append(cut)
        pts = out
    return pts + pts[:1] if len(pts) >= 3 else None


def _fold_lons(geometry):
    """Polygon/MultiPolygon with unwrapped longitudes (-181 .. 181, or 0..360) -> within -180..180, split at +-180."""
    polys = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    if all(-180 <= p[0] <= 180 for poly in polys for ring in poly for p in ring):
        return geometry
    out = []
    for poly in polys:
        for k in (-360, 0, 360):  # copy of the ring shifted by k that lands in -180..180
            if (r := _clip_strip([[x + k, y] for x, y in poly[0]], -180, 180)):
                out.append([r])
    if not out:
        return None
    return {"type": "Polygon", "coordinates": out[0]} if len(out) == 1 else {"type": "MultiPolygon", "coordinates": out}


def _vocab_target(name: str):
    targets = _vocab()["targets"]
    name = name.strip().lower()
    return name if name in targets else next((k for k, v in targets.items() if v.lower() == name), None)


def _planetary_targets(coll: dict) -> list[str]:
    names = (coll.get("summaries") or {}).get("ssys:targets") or []
    names = names or coll.get("keywords") or []
    return list(dict.fromkeys(t for n in names if (t := _vocab_target(str(n)))))


def _first(*values):
    return next((v for v in values if v), None)


def _join(value):
    return ", ".join(map(str, value)) if isinstance(value, list) else value


def _https(href: str, alternate: dict | None):
    if not href.startswith("s3://"):
        return href
    if alternate and (alt := (alternate.get("https") or {}).get("href")):
        return alt
    bucket, _, key = href[5:].partition("/")
    region = ".af-south-1" if bucket.startswith("deafrica-") else ""  # non-default-region buckets reject the global endpoint
    return f"https://{bucket}.s3{region}.amazonaws.com/{key}"


# ---- collections -------------------------------------------------------------------------------

async def _get_json(client, url, **params):
    r = await client.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return json_of(r)


async def _all_collections(conn_id: str, client) -> list[dict]:
    hit = _collections_cache.get(conn_id)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    base = ENDPOINTS[conn_id]["url"]
    colls, url, params = [], f"{base}/collections", {"limit": 500}
    try:
        for _ in range(10):
            data = await _get_json(client, url, **params)
            colls += data["collections"]
            url = next((l["href"] for l in data.get("links", []) if l["rel"] == "next"), None)
            if not url:
                break
            params = {}
    except (ValueError, KeyError, httpx.DecodingError):  # DEA Africa's /collections answers HTML errors: use the root catalog's children
        root = await _get_json(client, base)
        colls = [{"id": l["href"].rsplit("/", 1)[-1]} for l in root.get("links", [])
                 if l["rel"] == "child" and "/collections/" in l["href"]]
    _collections_cache[conn_id] = (time.time(), colls)
    return colls


def _collection_result(conn_id: str, c: dict) -> dict:
    ext = c.get("extent") or {}
    bbox = _valid_bbox(((ext.get("spatial") or {}).get("bbox") or [None])[0])
    interval = ((ext.get("temporal") or {}).get("interval") or [[None, None]])[0]
    links = {l["rel"]: l["href"] for l in c.get("links", [])}
    thumb = ((c.get("assets") or {}).get("thumbnail") or {}).get("href")
    summaries = c.get("summaries") or {}
    targets = _planetary_targets(c) if ENDPOINTS[conn_id]["targets"] != EARTH else EARTH
    return result(
        id=c["id"], connector=conn_id, level="collection", title=c.get("title") or c["id"],
        description=c.get("description"), target=targets[0] if targets else None,
        mission=_join(summaries.get("platform") or summaries.get("constellation")),
        instrument=_join(summaries.get("instruments")), start=interval[0], end=interval[1],
        footprint=_rect(*bbox) if bbox else None, bbox=bbox, thumbnail=thumb,
        assets=[asset(thumb, role="thumbnail")] if thumb else [],
        landing=_first(links.get("cite-as"), links.get("about"), links.get("describedby"),
                       f"{ENDPOINTS[conn_id]['url']}/collections/{c['id']}"),
        collection=c["id"],
        extra={k: v for k, v in {
            "license": c.get("license"), "keywords": c.get("keywords"), "targets": targets or None,
            "providers": [p.get("name") for p in c.get("providers", [])] or None,
            "gsd": summaries.get("gsd")}.items() if v})


def _collection_matches(c: dict, q: Query, box, words) -> bool:
    if words:
        hay = " ".join(str(c.get(k, "")) for k in ("id", "title", "description")).lower() + " " + \
              " ".join(map(str, c.get("keywords", []))).lower()
        if not all(w in hay for w in words):
            return False
    if box:
        cb = _valid_bbox((((c.get("extent") or {}).get("spatial") or {}).get("bbox") or [None])[0])
        if cb and not _overlap(cb, box):
            return False
    interval = ((((c.get("extent") or {}).get("temporal") or {}).get("interval")) or [[None, None]])[0]
    start, end = _rfc3339(q.start), _rfc3339(q.end, True)
    if (end and interval[0] and interval[0] > end) or (start and interval[1] and interval[1] < start):
        return False
    return True


async def _collection_search(conn_id: str, q: Query, client) -> dict:
    endpoint = ENDPOINTS[conn_id]
    colls = await _all_collections(conn_id, client)
    words = q.text.lower().split()
    box = query_bbox(q) if endpoint["targets"] == EARTH else q.bbox
    hits = [c for c in colls if _collection_matches(c, q, box, words)]
    if q.target and endpoint["targets"] != EARTH:
        hits = [c for c in hits if q.target in _planetary_targets(c)]
    hits.sort(key=lambda c: not all(w in f"{c['id']} {c.get('title', '')}".lower() for w in words))
    rows = [_collection_result(conn_id, c) for c in hits[q.offset:q.offset + q.limit]]
    end = q.offset + len(rows)
    return page(rows, total=len(hits), next_offset=end if end < len(hits) else None, notes=endpoint.get("notes"))


# ---- items ---------------------------------------------------------------------------------------

def _asset_role(key: str, a: dict) -> str:
    roles, kind = a.get("roles") or [], a.get("type") or ""
    if "thumbnail" in roles or key == "thumbnail":
        return "thumbnail"
    if key in ("rendered_preview", "preview", "reduced_resolution_browse") or ("overview" in roles and "data" not in roles):
        return "browse"
    if "metadata" in roles or key.endswith("metadata") or kind.startswith(("text/", "application/xml", "application/json")):
        return "metadata"
    if "data" in roles or any(t in kind for t in ("tiff", "jp2", "hdf", "netcdf", "zarr")) or not roles:
        return "data"
    return "other"


def _item_assets(item: dict) -> list[dict]:
    out = []
    for key, a in (item.get("assets") or {}).items():
        href = _https(a["href"], a.get("alternate"))
        role = _asset_role(key, a)
        name = None
        if "." not in urlsplit(href).path.rsplit("/", 1)[-1]:  # CDSE download links have no extension: borrow it from the s3 URI
            ext = a["href"].rsplit("/", 1)[-1].rpartition(".")[2] if "." in a["href"].rsplit("/", 1)[-1] else ""
            name = f"{key}.{ext}" if ext else key
        out.append(asset(href, role=role, type=a.get("type"), size=a.get("file:size"), name=name))
    return out


def _item_bbox(item: dict):
    return _valid_bbox(item.get("bbox")) or (_bounds(item["geometry"]) if (item.get("geometry") or {}).get("type") in
                                             ("Polygon", "MultiPolygon") else None)


def _item_result(conn_id: str, item: dict, planetary: bool) -> dict:
    p = item.get("properties") or {}
    geometry = item.get("geometry")
    if geometry and geometry["type"] in ("Polygon", "MultiPolygon") and geometry.get("coordinates"):
        geometry = _fold_lons(geometry)
    assets = _item_assets(item)
    by_role = {a["role"]: a["url"] for a in reversed(assets) if a["role"] in ("thumbnail", "browse")}
    links = {l["rel"]: l["href"] for l in item.get("links", [])}
    targets = [_vocab_target(str(t)) for t in p.get("ssys:targets", [])] if planetary else EARTH
    return result(
        id=item["id"], connector=conn_id, level="product", title=p.get("title") or item["id"],
        description=p.get("description"), target=_first(*targets), mission=_join(_first(p.get("platform"), p.get("missions"))),
        instrument=_join(p.get("instruments")), start=_first(p.get("start_datetime"), p.get("datetime")),
        end=p.get("end_datetime"), footprint=geometry, bbox=_item_bbox(item),
        thumbnail=by_role.get("thumbnail") or by_role.get("browse"), browse=by_role.get("browse") or by_role.get("thumbnail"),
        assets=assets, landing=_first(*(h for h in (links.get("canonical"), links.get("self")) if h and h.startswith("http"))), collection=item.get("collection"),
        extra={k: p[k] for k in ("eo:cloud_cover", "gsd", "constellation", "proj:code", "proj:epsg", "processing:level",
                                 "sat:orbit_state", "license") if p.get(k) is not None})


def _search_body(conn_id: str, q: Query) -> dict:
    body = {"collections": [q.collection], "limit": q.limit}
    box = query_bbox(q) if ENDPOINTS[conn_id]["targets"] == EARTH else q.bbox
    if box and box[0] > box[2] and ENDPOINTS[conn_id].get("flat_geometry"):  # Explorer servers ignore MultiPolygon 'intersects'
        raise ValueError(f"{ENDPOINTS[conn_id]['name']} cannot search across the antimeridian; search W..180 and -180..E separately")
    if box and box[0] > box[2]:  # antimeridian: a bbox with W>E is not accepted everywhere
        body["intersects"] = {"type": "MultiPolygon", "coordinates": _rect(*box)["coordinates"]}
    elif box and box != (-180, -90, 180, 90):
        body["bbox"] = list(box)
    if q.start or q.end:
        # explicit bounds: several servers (USGS, CDSE, Explorer) reject or crash on open '..' ranges
        body["datetime"] = f"{_rfc3339(q.start) or '1900-01-01T00:00:00Z'}/{_rfc3339(q.end, True) or '2100-01-01T00:00:00Z'}"
    if ENDPOINTS[conn_id].get("sort_newest"):  # these servers list oldest first by default
        body["sortby"] = [{"field": "datetime", "direction": "desc"}]
    return body


async def _fetch(client, spec):
    method, url, body = spec
    r = await (client.post(url, json=body, timeout=TIMEOUT) if method == "POST" else client.get(url, timeout=TIMEOUT))
    if r.status_code in (400, 422):
        raise ValueError(f"STAC server rejected the query: {r.text[:300]}")
    r.raise_for_status()
    data = json_of(r)
    nxt = next((l for l in data.get("links", []) if l["rel"] == "next"), None)
    if nxt is None:
        return data, None
    method = nxt.get("method", "GET").upper()
    nbody = {**body, **nxt["body"]} if nxt.get("merge") and nxt.get("body") else nxt.get("body")
    return data, (method, nxt["href"], nbody)


async def _product_search(conn_id: str, q: Query, client) -> dict:
    if not q.collection:
        raise ValueError("product search needs a collection id (pick a collection-level result first)")
    endpoint = ENDPOINTS[conn_id]
    body = _search_body(conn_id, q)
    key = (conn_id, json.dumps(body, sort_keys=True))
    skip = q.offset % q.limit
    pos = q.offset - skip
    if len(_next_links) > 2000:
        _next_links.clear()
    # STAC pages by opaque next links: resume from the nearest page start we have seen, else walk from the beginning.
    at = max((o for o in range(0, pos + 1, q.limit) if (key, o) in _next_links), default=0)
    spec = _next_links.get((key, at)) or ("POST", endpoint["url"] + "/search", body)
    if (pos - at) // q.limit > MAX_PAGES:
        raise ValueError("offset too deep for a paged STAC search; narrow the area or dates")
    items, matched = [], None
    while True:
        data, nxt = await _fetch(client, spec)
        matched = _first(data.get("numberMatched"), (data.get("context") or {}).get("matched"), matched)
        if nxt:
            _next_links[(key, at + q.limit)] = nxt
        if at >= pos:
            items += data["features"]
        at += q.limit
        if nxt is None or (at > pos and len(items) >= skip + q.limit):
            break
        spec = nxt
    more = nxt is not None or len(items) > skip + q.limit
    items = items[skip:skip + q.limit]
    end = q.offset + len(items)
    has_next = end < matched if matched is not None else more
    planetary = endpoint["targets"] != EARTH
    notes = list(endpoint.get("notes") or [])
    if q.text:
        notes.append("Free text is not applied to STAC item search; it only filters collections.")
    return page([_item_result(conn_id, i, planetary) for i in items], total=matched,
                next_offset=end if has_next and items else None, notes=notes)


async def search(conn_id: str, q: Query, client) -> dict:
    check_times(q)
    if q.target and q.target not in ENDPOINTS[conn_id]["targets"]:
        return page([], total=0, notes=[f"{ENDPOINTS[conn_id]['name']} has no data for target {q.target!r}."])
    if q.level == "collection":
        return await _collection_search(conn_id, q, client)
    return await _product_search(conn_id, q, client)
