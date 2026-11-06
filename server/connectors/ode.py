"""PDS Geosciences Node Orbital Data Explorer (ODE) REST connector.

Collection = one ODE "IIPT" set (instrument host / instrument / product type), id "target|ihid|iid|pt".
Product = one ODE product with its real data/label/browse file URLs and a footprint.
API manual: https://oderest.rsl.wustl.edu/ODE_REST_V2.1.6.pdf
"""
import asyncio
import re
import time
from urllib.parse import urlencode

import httpx

from .base import Query, asset, page, result
from ._util import check_times, json_of

API = "https://oderest.rsl.wustl.edu/live2/"
TARGET_DB = {"mars": "mars", "phobos": "mars", "deimos": "mars", "moon": "moon", "mercury": "mercury", "venus": "venus"}
TIMEOUT = httpx.Timeout(25, connect=10)
IIPT_TTL = 3600

CONNECTORS = [{
    "id": "ode", "name": "PDS Orbital Data Explorer", "provider": "NASA PDS Geosciences Node (WUSTL)",
    "targets": list(TARGET_DB), "levels": ["collection", "product"], "needs_collection": True,
    "supports": ["text", "bbox", "time", "instrument"],
    "url": "https://ode.rsl.wustl.edu/",
    "description": "Mars, Moon, Mercury and Venus orbital products (HiRISE, CTX, THEMIS, LROC, MESSENGER MDIS, "
                   "Magellan...) with footprints, browse images and direct PDS file links. Search a collection "
                   "(instrument + product type) first, then its products by area, time or product id.",
}]

_iipt_cache: dict[str, tuple[float, list]] = {}


def _list(x) -> list:  # ODE's JSON gives a bare object when there is exactly one element
    return x if isinstance(x, list) else [] if x is None else [x]


async def _get(client: httpx.AsyncClient, **params) -> dict:
    r = await client.get(API, params={"output": "JSON", **params}, timeout=TIMEOUT)
    r.raise_for_status()
    res = json_of(r)["ODEResults"]
    if res.get("Status") != "Success":
        raise ValueError(f"ODE: {res.get('Error') or res.get('Status')}")
    return res


async def _iipt(client: httpx.AsyncClient, db: str) -> list:
    hit = _iipt_cache.get(db)
    if hit and time.monotonic() - hit[0] < IIPT_TTL:
        return hit[1]
    res = await _get(client, target=db, query="iipt", odemetadb=db)
    sets = _list(res["IIPTSets"]["IIPTSet"])
    _iipt_cache[db] = (time.monotonic(), sets)
    return sets


def _valid_targets(s: dict) -> set[str]:
    return {t.upper() for t in _list(s["ValidTargets"]["ValidTarget"])}


def _iso(t: str | None) -> str | None:
    if not t:
        return None
    return t if t.endswith("Z") else t + "Z"


def _footprint(p: dict) -> dict | None:
    """ODE WKT -> GeoJSON in -180..180 (C0 footprint preferred; it is already split at the antimeridian)."""
    for key in ("Footprint_C0_geometry", "Footprint_geometry"):
        wkt = p.get(key) or ""
        if "EMPTY" in wkt or not wkt:
            continue
        stack: list[list] = [[]]
        for m in re.finditer(r"\(|\)|(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)", wkt):
            if m.group() == "(":
                stack.append([])
            elif m.group() == ")":
                done = stack.pop()
                stack[-1].append(done)
            else:
                lon = float(m.group(1))
                stack[-1].append((lon - 360 if lon > 180 else lon, float(m.group(2))))

        def polygons(node):  # a polygon is a list of rings; a ring is a list of coordinate tuples
            if node and node[0] and isinstance(node[0][0], tuple):
                return [node]
            return [x for child in node for x in polygons(child)]

        polys = polygons(stack[0])
        if len(polys) == 1:
            return {"type": "Polygon", "coordinates": [[list(c) for c in ring] for ring in polys[0]]}
        if polys:
            return {"type": "MultiPolygon", "coordinates": [[[list(c) for c in ring] for ring in poly] for poly in polys]}
    return None


def _bbox(p: dict) -> list | None:
    def num(name):
        v = p.get(name, p.get(name + "_text"))
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    w, e, s, n = num("Westernmost_longitude"), num("Easternmost_longitude"), num("Minimum_latitude"), num("Maximum_latitude")
    if None in (w, e, s, n):
        return None
    if e - w >= 360 or (w, e) == (0, 360):
        return [-180, s, 180, n]
    return [round((w + 180) % 360 - 180, 5), s, round((e + 180) % 360 - 180, 5), n]  # W > E when it crosses the antimeridian


def _assets(p: dict) -> tuple[list, str | None, str | None]:
    assets, thumb, browse = [], None, None
    for f in _list(p.get("Product_files", {}).get("Product_file")):
        name, url, kind = f["FileName"], f["URL"], f.get("Type")
        size = int(float(f["KBytes"]) * 1024) if f.get("KBytes") else None
        ext = name.lower().rsplit(".", 1)[-1]
        if kind == "Product":
            role = "label" if ext in ("lbl", "xml") else "data"
        elif kind == "Browse" and ext in ("jpg", "jpeg", "png", "tif", "tiff", "jp2", "gif"):
            if "THUMB" in f.get("Description", "").upper() or "THUMB" in name.upper():
                role, thumb = "thumbnail", thumb or url
            else:
                role = "browse"
                if ext in ("jpg", "jpeg", "png") and browse is None:
                    browse = url
        else:
            continue  # catalog docs, KML and shapefile derivatives
        assets.append(asset(url, name, role, type=ext.upper(), size=size))
    return assets, thumb, browse


def _collection_result(target: str, s: dict) -> dict:
    ihid, iid, pt = s["IHID"], s["IID"], s["PT"]
    n = s.get("NumberProducts")
    return result(
        id=f"{target}|{ihid}|{iid}|{pt}", connector="ode", level="collection",
        title=f"{ihid} {iid} — {pt}", description=f"{s['PTName']} ({s['IHName']}, {s['IName']})",
        target=target, mission=s["IHName"], instrument=s["IName"],
        start=_iso(s.get("MinObservationTime")), end=_iso(s.get("MaxObservationTime")),
        landing=f"https://ode.rsl.wustl.edu/{TARGET_DB[target]}/",
        collection=f"{target}|{ihid}|{iid}|{pt}",
        extra={"products": int(n) if n else None, "dataset": s["DataSetId"],
               "footprints": s["ValidFootprints"] == "T", "observation times": s["ValidObservationTimes"] == "T"})


async def _collections(q: Query, client: httpx.AsyncClient) -> dict:
    if q.target and q.target not in TARGET_DB:
        raise ValueError(f"ODE has no data for target {q.target!r}")
    targets = [q.target] if q.target else ["mars", "moon", "mercury", "venus"]
    sets = await asyncio.gather(*(_iipt(client, TARGET_DB[t]) for t in targets))
    words = q.text.lower().split()
    hits = []
    for t, group in zip(targets, sets):
        for s in group:
            if t.upper() not in _valid_targets(s):
                continue
            hay = " ".join(str(s.get(k, "")) for k in ("IHID", "IHName", "IID", "IName", "PT", "PTName", "DataSetId")).lower()
            if not all(w in hay for w in words):
                continue
            if q.instrument and q.instrument.lower() not in f"{s['IID']} {s['IName']}".lower():
                continue
            if q.mission and q.mission.lower() not in f"{s['IHID']} {s['IHName']}".lower():
                continue
            hits.append((t, s))
    merged: dict[tuple, tuple] = {}
    for t, s in hits:  # several dataset versions share one instrument/product-type id; a product search spans them all
        k = (t, s["IHID"], s["IID"], s["PT"])
        if k in merged:
            merged[k][1]["NumberProducts"] = int(merged[k][1].get("NumberProducts") or 0) + int(s.get("NumberProducts") or 0)
        else:
            merged[k] = (t, dict(s))
    hits = sorted(merged.values(), key=lambda h: -int(h[1].get("NumberProducts") or 0))
    shown = hits[q.offset:q.offset + q.limit]
    end = q.offset + len(shown)
    return page([_collection_result(t, s) for t, s in shown], len(hits), end if end < len(hits) else None)


def _lon360_box(bbox) -> dict:
    w, s, e, n = bbox
    if e - w >= 360:
        w, e = 0, 360
    return {"minlat": s, "maxlat": n, "westernlon": w % 360, "easternlon": e % 360 or (360 if e > w else 0)}


async def _products(q: Query, client: httpx.AsyncClient) -> dict:
    try:
        target, ihid, iid, pt = (q.collection or "").split("|")
        db = TARGET_DB[target]
    except (ValueError, KeyError):
        raise ValueError("ODE product search needs a collection id ('target|ihid|iid|producttype') from a collection-level result") from None
    params = {"target": target, "ihid": ihid, "iid": iid, "pt": pt}
    sets = await _iipt(client, db)
    info = next((s for s in sets if (s["IHID"], s["IID"], s["PT"]) == (ihid, iid, pt)), None)
    if info and q.bbox and info["ValidFootprints"] != "T":
        raise ValueError(f"ODE has no footprints for {ihid} {iid} {pt}, so it cannot be searched by area; pick a map-projected product type")
    if info and (q.start or q.end) and info["ValidObservationTimes"] != "T":
        raise ValueError(f"ODE has no observation times for {ihid} {iid} {pt}, so it cannot be searched by time")
    if q.bbox:
        params.update(_lon360_box(q.bbox))
    if q.start or q.end:
        params["minobtime"] = q.start or "1900-01-01"
        if q.end:
            params["maxobtime"] = q.end
    if q.text:
        params["pdsid"] = f"*{q.text.strip()}*"
    count, res = await asyncio.gather(
        _get(client, query="product", results="c", **params),
        _get(client, query="product", results="copmf", limit=q.limit, offset=q.offset + 1, **params))  # ODE offsets are 1-based
    total = int(count["Count"])
    products = _list(res["Products"].get("Product") if isinstance(res.get("Products"), dict) else None)
    out = []
    for p in products:
        assets, thumb, browse = _assets(p)
        pid = p["pdsid"]
        if not thumb:  # ODE renders a tiny JPEG thumbnail on demand
            thumb = f"{API}?" + urlencode({"query": "thumbnail", "target": target, "ihid": ihid, "iid": iid, "pt": pt, "pdsid": pid})
        extra = {k: p[k] for k in ("Map_scale", "Map_resolution", "Incidence_angle", "Emission_angle", "Phase_angle",
                                   "Solar_longitude", "Data_Set_Id", "Product_version_id") if p.get(k)}
        out.append(result(
            id=f"{iid}-{pt}-{pid}", connector="ode", level="product", title=pid,
            description=p.get("Comment") or p.get("Description"), target=target, mission=ihid, instrument=iid,
            start=_iso(p.get("UTC_start_time") or p.get("Observation_time")), end=_iso(p.get("UTC_stop_time")),
            footprint=_footprint(p), bbox=_bbox(p), thumbnail=thumb, browse=browse or thumb, assets=assets,
            landing=p.get("ProductURL") or p.get("FilesURL"), extra={"product type": pt, **extra}))
    end = q.offset + len(out)
    return page(out, total, end if end < total else None)


async def search(conn_id: str, q: Query, client: httpx.AsyncClient) -> dict:
    check_times(q)
    return await (_collections if q.level == "collection" else _products)(q, client)
