"""ASF DAAC Search API: SAR (and a few optical) datasets - Sentinel-1, ALOS, UAVSAR, SEASAT, NISAR, OPERA ..."""
import asyncio

from .base import Query, asset, page, result
from .base import bounds as _bounds, query_bbox
from ._util import check_times, json_of

API = "https://api.daac.asf.alaska.edu/services/search/param"
MAX_WINDOW = 2000  # the API has no offset: offset+limit results are fetched and sliced

CONNECTORS = [{
    "id": "asf", "name": "ASF DAAC (SAR archive)", "provider": "NASA Alaska Satellite Facility DAAC",
    "targets": ["earth"], "levels": ["collection", "product"], "needs_collection": True, "supports": ["text", "bbox", "time"],
    "url": "https://search.asf.alaska.edu/",
    "description": "Synthetic-aperture-radar archive: Sentinel-1, ALOS PALSAR, UAVSAR, SEASAT, SIR-C, NISAR, "
                   "OPERA and more, with browse images. Downloads need a free Earthdata login (token in settings).",
}]

# (dataset name accepted by the API, mission, instrument, description). Verified 2026-11-05; the legacy
# RADARSAT-1, ERS, JERS-1 and AIRSAR datasets are named in the API but return no products, so they are left out.
DATASETS = [
    ("SENTINEL-1", "Sentinel-1", "C-SAR", "ESA C-band SAR constellation (GRD, SLC, raw, OCN), global since 2014."),
    ("SLC-BURST", "Sentinel-1", "C-SAR", "Sentinel-1 single-look-complex bursts, individually downloadable."),
    ("OPERA-S1", "Sentinel-1", "C-SAR", "NASA OPERA analysis-ready Sentinel-1 products: RTC backscatter, CSLC, displacement."),
    ("ALOS PALSAR", "ALOS", "PALSAR", "JAXA L-band SAR, 2006-2011, global (L1.0, L1.1, RTC)."),
    ("ALOS AVNIR-2", "ALOS", "AVNIR-2", "JAXA ALOS optical multispectral 10 m imagery, 2006-2011."),
    ("UAVSAR", "UAVSAR", "UAVSAR", "NASA/JPL airborne L-band polarimetric and interferometric SAR campaigns."),
    ("SIR-C", "Space Shuttle STS-59/68", "SIR-C", "Spaceborne Imaging Radar-C multi-frequency, multi-polarization SAR, 1994."),
    ("SEASAT", "SEASAT 1", "SAR", "1978 SEASAT L-band SAR, the first civilian spaceborne SAR (reprocessed GeoTIFFs)."),
    ("NISAR", "NISAR", "L- and S-band SAR", "NASA-ISRO NISAR L/S-band SAR products (launched 2025)."),
    ("SMAP", "SMAP", "SAR", "SMAP L-band radar level-1 data (soil moisture mission)."),
    ("ARIA S1 GUNW", "Sentinel-1", "C-SAR", "ARIA Sentinel-1 geocoded unwrapped interferograms."),
]


def _size(value):
    return value if isinstance(value, (int, float)) else None


def _file_size(sizes: dict, name: str):
    info = sizes.get(name) or {}
    return None if info.get("format") in ("MB", "GB") else _size(info.get("bytes"))  # those are not byte counts


def _product(feature: dict) -> dict:
    p = feature["properties"]
    sizes = p["bytes"] if isinstance(p.get("bytes"), dict) else {}
    assets = [asset(p["url"], p.get("fileName"), "data", size=_size(p.get("bytes")))]
    assets += [asset(u, role="browse") for u in p.get("browse") or []]
    for url in p.get("additionalUrls") or []:
        if url not in {a["url"] for a in assets}:
            name = url.rsplit("/", 1)[-1]
            assets.append(asset(url, name, "browse" if "/BROWSE/" in url else "data", size=_file_size(sizes, name)))
    previews = [a["url"] for a in assets if a["role"] == "browse"]
    geometry = feature.get("geometry")
    return result(
        id=p["sceneName"], connector="asf", level="product", title=p["sceneName"],
        description=" ".join(str(v) for v in (p.get("platform"), p.get("sensor"), p.get("processingLevel"),
                                              p.get("beamModeType"), p.get("flightDirection")) if v) or None,
        target="earth", mission=p.get("platform"), instrument=p.get("sensor"),
        start=p.get("startTime"), end=p.get("stopTime"), footprint=geometry,
        bbox=_bounds(geometry) if geometry else None, thumbnail=previews[0] if previews else None,
        browse=previews[0] if previews else None, assets=assets,
        landing=f"https://search.asf.alaska.edu/#/?granule={p['sceneName']}", collection=None,
        extra={k: v for k, v in {
            "processing_level": p.get("processingLevel"), "beam_mode": p.get("beamModeType"),
            "polarization": p.get("polarization"), "flight_direction": p.get("flightDirection"),
            "path": p.get("pathNumber"), "frame": p.get("frameNumber"), "orbit": p.get("orbit")}.items()
               if v is not None})


def _wkt(box) -> str:
    w, s, e, n = box
    ring = lambda a, b: f"(({a} {s},{b} {s},{b} {n},{a} {n},{a} {s}))"  # noqa: E731
    if w > e:  # crosses the antimeridian
        return f"MULTIPOLYGON({ring(w, 180)},{ring(-180, e)})"
    return f"POLYGON{ring(w, e)}"


async def _collections(q: Query) -> dict:
    words = q.text.lower().split()
    hits = [d for d in DATASETS if all(w in " ".join(d).lower() for w in words)
            and (not q.mission or q.mission.lower() in d[1].lower())]
    rows = [result(id=n, connector="asf", level="collection", title=n, description=desc, target="earth",
                   mission=mission, instrument=instr, collection=n,
                   landing=f"https://search.asf.alaska.edu/#/?dataset={n.replace(' ', '%20')}",
                   extra={"dataset": n}) for n, mission, instr, desc in hits]
    return page(rows[q.offset:q.offset + q.limit], total=len(rows),
                next_offset=q.offset + q.limit if q.offset + q.limit < len(rows) else None,
                notes=["Downloads need an Earthdata login; the backend adds the token set in Settings."])


async def search(conn_id: str, q: Query, client) -> dict:
    check_times(q)
    if q.target and q.target != "earth":
        return page([], total=0, notes=["ASF only holds Earth data."])
    if q.level == "collection":
        return await _collections(q)
    if q.collection not in {d[0] for d in DATASETS}:
        raise ValueError("product search needs an ASF dataset name as collection, e.g. SENTINEL-1")
    window = q.offset + q.limit
    if window > MAX_WINDOW:
        raise ValueError(f"ASF paging is limited to the newest {MAX_WINDOW} results; narrow the area or dates")
    params = {"dataset": q.collection}
    if q.text:  # the API has no free text; exact scene names are accepted
        params["granule_list"] = ",".join(q.text.replace(",", " ").split())
    if box := query_bbox(q):
        params["intersectsWith"] = _wkt(box)
    if q.start:
        params["start"] = q.start
    if q.end:
        params["end"] = q.end
    notes = ["Downloads need an Earthdata login; the backend adds the token set in Settings."]
    if q.collection == "SENTINEL-1" and not q.text:
        params["processingLevel"] = "GRD_HD"  # default listing is dominated by raw metadata products
        notes.append("Sentinel-1 is limited to GRD_HD (ground-range detected, high-res) products.")
    if q.text:
        notes.append("Text is matched as exact scene names (space separated).")

    async def get(**extra):
        r = await client.get(API, params={**params, **extra})
        if r.status_code == 400:
            raise ValueError("ASF rejected the query: " + r.text[:200])
        r.raise_for_status()
        return r

    r, count = await asyncio.gather(get(output="geojson", maxResults=window), get(output="count"))
    total = int(count.text.strip())
    rows = [_product(f) for f in json_of(r)["features"][q.offset:window]]
    return page(rows, total=total, next_offset=window if total > window and window < MAX_WINDOW else None, notes=notes)
