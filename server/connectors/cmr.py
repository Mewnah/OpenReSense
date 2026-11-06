"""NASA CMR (Common Metadata Repository): Earth science collections and granules from all NASA DAACs."""

from .base import Query, asset, bounds as _bounds, page, query_bbox, rect as _rect, result
from ._util import check_times, json_of

BASE = "https://cmr.earthdata.nasa.gov/search"
SEARCH_UI = "https://search.earthdata.nasa.gov/search/granules"  # CMR's own /concepts/<id>.html pages answer 400
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif")
REL = "http://esipfed.org/ns/fedsearch/1.1/"

CONNECTORS = [{
    "id": "cmr", "name": "NASA CMR (Earthdata)", "provider": "NASA EOSDIS Common Metadata Repository",
    "targets": ["earth"], "levels": ["collection", "product"], "needs_collection": True,
    "supports": ["text", "bbox", "time", "instrument", "mission"],
    "url": "https://search.earthdata.nasa.gov/",
    "description": "Every NASA Earth-science collection and granule across the DAACs (Landsat, MODIS, HLS, "
                   "ICESat-2, SMAP ...). Most downloads need a free Earthdata login (token in settings).",
}]


def _bbox_str(q: Query):
    box = query_bbox(q)
    return ",".join(f"{v:g}" for v in box) if box else None


def _merge(polys: list[dict]):
    if len(polys) < 2:
        return polys[0] if polys else None
    parts = [c for p in polys for c in ([p["coordinates"]] if p["type"] == "Polygon" else p["coordinates"])]
    return {"type": "MultiPolygon", "coordinates": parts}


def _doi_url(doi):
    return f"https://doi.org/{doi}" if doi else None


def _collection_footprint(umm: dict):
    """(GeoJSON geometry, bbox) from the collection's bounding rectangles and polygons."""
    geom = (umm.get("SpatialExtent") or {}).get("HorizontalSpatialDomain", {}).get("Geometry") or {}
    rects = [(r["WestBoundingCoordinate"], r["SouthBoundingCoordinate"], r["EastBoundingCoordinate"],
              r["NorthBoundingCoordinate"]) for r in geom.get("BoundingRectangles", [])]
    polys = [_rect(*r) for r in rects]
    for g in geom.get("GPolygons", []):
        ring = [[p["Longitude"], p["Latitude"]] for p in g["Boundary"]["Points"]]
        polys.append({"type": "Polygon", "coordinates": [ring + [ring[0]]]})
    if len(rects) == 1 and len(polys) == 1:
        bbox = list(rects[0])
    elif rects:
        bbox = [min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects)]
    else:
        bbox = _bounds(polys[0]) if len(polys) == 1 else None
    return _merge(polys), bbox


def _collection_result(item: dict) -> dict:
    u, meta = item["umm"], item["meta"]
    cid = meta["concept-id"]
    platforms = u.get("Platforms") or []
    instruments = sorted({i["ShortName"] for p in platforms for i in p.get("Instruments", [])})
    temporal = (u.get("TemporalExtents") or [{}])[0]
    ranges = temporal.get("RangeDateTimes") or [{}]
    related = u.get("RelatedUrls") or []
    thumb = next((r["URL"] for r in related if r.get("Type") == "GET RELATED VISUALIZATION"
                  and r["URL"].lower().endswith(IMAGE_EXT)), None)
    landing = next((r["URL"] for r in related if r.get("Type") == "DATA SET LANDING PAGE"), None)
    footprint, bbox = _collection_footprint(u)
    return result(
        id=cid, connector="cmr", level="collection", title=u.get("EntryTitle") or u.get("ShortName"),
        description=(u.get("Abstract") or "").strip()[:700] or None, target="earth",
        mission=", ".join(p["ShortName"] for p in platforms) or None,
        instrument=", ".join(instruments) or None,
        start=ranges[0].get("BeginningDateTime"),
        end=None if temporal.get("EndsAtPresentFlag") else ranges[-1].get("EndingDateTime"),
        footprint=footprint, bbox=bbox, thumbnail=thumb,
        assets=[asset(thumb, role="thumbnail")] if thumb else [],
        landing=_doi_url((u.get("DOI") or {}).get("DOI")) or landing or f"{SEARCH_UI}?p={cid}",
        collection=cid,
        extra={"provider": meta.get("provider-id"), "short_name": u.get("ShortName"), "version": u.get("Version"),
               "platforms": [p["ShortName"] for p in platforms], "instruments": instruments,
               "processing_level": (u.get("ProcessingLevel") or {}).get("Id"), "cloud_hosted": u.get("CloudHosted")})


def _granule_footprint(g: dict):
    polys = []
    for rings in g.get("polygons", []):  # first ring is the outer boundary; each is "lat lon lat lon ..."
        v = [float(x) for x in rings[0].split()]
        polys.append({"type": "Polygon", "coordinates": [[[v[i + 1], v[i]] for i in range(0, len(v), 2)]]})
    for b in g.get("boxes", []):
        s, w, n, e = (float(x) for x in b.split())
        polys.append(_rect(w, s, e, n))
    return _merge(polys)


def _granule_result(g: dict) -> dict:
    assets, thumb = [], None
    links = [(l["rel"], l["href"]) for l in g.get("links", []) if l["href"].startswith("http")]  # s3:// URIs need AWS credentials
    # collection-level links (search pages, user guides, DOI) trail the granule's files; keep the files' own directories
    files = [h for r, h in links if r == REL + "data#" and "." in h.rsplit("/", 1)[-1]]
    dirs = {h.rsplit("/", 1)[0] for h in files}
    for rel, href in links:
        if rel == REL + "data#" and href in files:
            assets.append(asset(href, role="data"))
        elif rel == REL + "browse#":
            assets.append(asset(href, role="browse"))
            if href.lower().endswith(IMAGE_EXT):
                thumb = thumb or href
        elif rel == REL + "metadata#" and not href.endswith("s3credentials") and (not dirs or href.rsplit("/", 1)[0] in dirs):
            assets.append(asset(href, role="metadata"))
    footprint = _granule_footprint(g)
    return result(
        id=g["id"], connector="cmr", level="product", title=g.get("producer_granule_id") or g["title"],
        description=g.get("dataset_id"), target="earth", start=g.get("time_start"), end=g.get("time_end"),
        footprint=footprint, bbox=_bounds(footprint) if footprint else None, thumbnail=thumb, browse=thumb,
        assets=assets,
        landing=f"{SEARCH_UI}?p={g.get('collection_concept_id')}&g={g['id']}", collection=g.get("collection_concept_id"),
        extra={k: g[k] for k in ("data_center", "cloud_cover", "day_night_flag", "granule_size") if k in g})


def _common_params(q: Query) -> dict:
    p = {"page_size": q.limit, "offset": q.offset}
    if q.start or q.end:
        p["temporal"] = f"{q.start or ''},{q.end or ''}"
    if box := _bbox_str(q):
        p["bounding_box"] = box
    for key, value in (("instrument", q.instrument), ("platform", q.mission)):
        if value:  # substring match, case-insensitive
            p[f"{key}[]"] = f"*{value}*"
            p[f"options[{key}][pattern]"] = "true"
            p[f"options[{key}][ignore_case]"] = "true"
    return p


async def search(conn_id: str, q: Query, client) -> dict:
    check_times(q)
    if q.target and q.target != "earth":
        return page([], total=0, notes=["NASA CMR only holds Earth data."])
    params = _common_params(q)
    if q.level == "collection":
        params["has_granules"] = "true"
        if q.text:
            params["keyword"] = " ".join(w + "*" for w in q.text.split())
        else:
            params["sort_key[]"] = "-usage_score"  # with a keyword CMR's relevance order is the default
        url, parse, key = f"{BASE}/collections.umm_json", _collection_result, "items"
    else:
        if not q.collection:
            raise ValueError("product search needs a collection id (pick a collection-level result first)")
        params["collection_concept_id"] = q.collection
        params["sort_key[]"] = "-start_date"
        if q.text:
            params["readable_granule_name[]"] = f"*{q.text}*"
            params["options[readable_granule_name][pattern]"] = "true"
        url, parse, key = f"{BASE}/granules.json", _granule_result, "entry"
    r = await client.get(url, params=params)
    if r.status_code == 400:
        raise ValueError("CMR rejected the query: " + "; ".join(r.json().get("errors", [r.text[:200]])))
    r.raise_for_status()
    body = json_of(r)
    items = body["items"] if key == "items" else body["feed"]["entry"]
    total = int(r.headers.get("CMR-Hits", len(items)))
    end = q.offset + len(items)
    return page([parse(i) for i in items], total=total, next_offset=end if end < total else None,
                notes=["Most downloads need a free NASA Earthdata login; set the token in Settings."])
