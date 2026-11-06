"""PDS Imaging Node Planetary Image Atlas connector.

Backend: the public Solr core the Atlas web UI itself queries (https://pds-imaging.jpl.nasa.gov/solr/pds_archives/,
~39 M image products, PDS3 and PDS4). Collection = (target, mission, instrument), id "target|mission|instrument".
"""
import re

import httpx

from .base import Query, asset, page, result
from ._util import check_times, json_of

SOLR = "https://pds-imaging.jpl.nasa.gov/solr/pds_archives/select"
TIMEOUT = httpx.Timeout(30, connect=10)
TARGETS = ("sun mercury venus earth moon mars phobos deimos gaspra ida jupiter io europa ganymede callisto amalthea "
           "himalia saturn mimas enceladus tethys dione rhea titan hyperion iapetus phoebe janus epimetheus prometheus "
           "pandora pan uranus miranda ariel umbriel titania oberon neptune triton proteus nereid pluto charon nix hydra").split()
FIELDS = ("uuid,PRODUCT_ID,TARGET,ATLAS_MISSION_NAME,ATLAS_INSTRUMENT_NAME,ATLAS_PRODUCT_TYPE,START_TIME,STOP_TIME,"
          "ATLAS_DATA_URL,ATLAS_LABEL_URL,ATLAS_BROWSE_URL,ATLAS_THUMBNAIL_URL,FILE_NAME_SPECIFICATION,DATA_SET_ID,"
          "MINIMUM_LATITUDE,MAXIMUM_LATITUDE,MINIMUM_LONGITUDE,MAXIMUM_LONGITUDE,MAP_SCALE,"
          "UPPER_LEFT_LATITUDE,UPPER_LEFT_LONGITUDE,UPPER_RIGHT_LATITUDE,UPPER_RIGHT_LONGITUDE,"
          "LOWER_RIGHT_LATITUDE,LOWER_RIGHT_LONGITUDE,LOWER_LEFT_LATITUDE,LOWER_LEFT_LONGITUDE")

CONNECTORS = [{
    "id": "pds-atlas", "name": "PDS Imaging Node Planetary Image Atlas", "provider": "NASA PDS Cartography and Imaging Sciences Node (JPL)",
    "targets": list(TARGETS), "levels": ["collection", "product"], "supports": ["text", "bbox", "time", "instrument"],
    "url": "https://pds-imaging.jpl.nasa.gov/search/",
    "description": "Tens of millions of planetary image products (Mars rovers and orbiters, LRO, Cassini, Voyager, "
                   "Galileo, MESSENGER, ...) with browse images and PDS file links. Text matches the start of the product id. "
                   "Area search only finds products that have footprints in the index (mostly map-projected/RDR products, not raw EDRs).",
}]


def _solr_time(t: str | None, end: bool = False) -> str:  # Solr rejects anything but full UTC timestamps
    if not t:
        return "*"
    if len(t) == 10:
        return t + ("T23:59:59Z" if end else "T00:00:00Z")
    return t if t.endswith("Z") else t + "Z"


def _clean(s: str) -> str:  # keeps user text from becoming Solr syntax
    return re.sub(r"[^\w.\- ]", "", s)


async def _select(client: httpx.AsyncClient, params: dict) -> dict:
    r = await client.get(SOLR, params={"wt": "json", "q": "*:*", **params}, timeout=TIMEOUT)
    r.raise_for_status()
    return json_of(r)


def _target_fq(target: str | None) -> list[str]:
    if target and target not in TARGETS:
        raise ValueError(f"Image Atlas has no data for target {target!r}")
    return [f'TARGET:"{target}"'] if target else []


def _lon_overlap(w: float, e: float) -> str:
    """Overlap with W..E (W > E crosses the antimeridian) against records stored as +-180 or as 0..360."""
    if e - w >= 360 or (w, e) == (-180, 180):
        spans = [(-180.0, 360.0)]
    else:
        w3, e3 = w % 360, e % 360
        spans = ([(w, e)] if w <= e else [(w, 180.0), (-180.0, e)]) + ([(w3, e3)] if w3 <= e3 else [(w3, 360.0), (0.0, e3)])
    return " OR ".join(f"(MINIMUM_LONGITUDE:[* TO {b}] AND MAXIMUM_LONGITUDE:[{a} TO *])" for a, b in spans)


async def _collections(q: Query, client: httpx.AsyncClient) -> dict:
    data = await _select(client, {"rows": 0, "fq": _target_fq(q.target), "facet": "true", "facet.limit": -1,
                                  "facet.mincount": 1, "facet.pivot": "ATLAS_MISSION_NAME,ATLAS_INSTRUMENT_NAME"})
    words = " ".join(x for x in (q.text, q.instrument, q.mission) if x).lower().split()
    hits = []
    for m in data["facet_counts"]["facet_pivot"]["ATLAS_MISSION_NAME,ATLAS_INSTRUMENT_NAME"]:
        for i in m.get("pivot", []):
            if all(w in f"{m['value']} {i['value']}" for w in words):
                hits.append((m["value"], i["value"], i["count"]))
    hits.sort(key=lambda h: -h[2])
    shown = hits[q.offset:q.offset + q.limit]
    out = [result(id=f"{q.target or ''}|{m}|{i}", connector="pds-atlas", level="collection",
                  title=f"{m.title()} {i.upper()}", description=f"{n:,} image products", target=q.target,
                  mission=m.title(), instrument=i.upper(), landing="https://pds-imaging.jpl.nasa.gov/search/",
                  collection=f"{q.target or ''}|{m}|{i}", extra={"products": n}) for m, i, n in shown]
    end = q.offset + len(shown)
    return page(out, len(hits), end if end < len(hits) else None)


_OFSN = re.compile(r"OFSN\+(?:%3D|=)\+(\S+?)(?:\+AND\+RT\+(?:%3D|=)\+(RAW|PDS_LABEL))?$")


def _fix(url: str | None, role: str = "data") -> str | None:
    """The Solr index still holds retired hosts and volume paths; map them onto the live archive.
    RAW 'prod?q=OFSN' links become direct file URLs (so the opened dataset keeps its real name and sibling label)."""
    if not url or "notavail" in url or not url.startswith("http"):  # placeholder images / relative names
        return None
    m = _OFSN.search(url)
    path = re.sub(r"/{2,}", "/", m[1]).replace("/mars_reconnaissance_orbiter/", "/") if m else ""
    if "starbrite.jpl.nasa.gov" in url and m:  # THEMIS: ASU's PDS mirror
        return "https://static.mars.asu.edu/pds/" + path.lstrip("/").removeprefix("pds/")
    url = re.sub(r"(?<!:)/{2,}", "/", url.replace("pdsimg.jpl.nasa.gov", "pds-imaging.jpl.nasa.gov"))
    url = url.replace("/mro/mars_reconnaissance_orbiter/", "/mro/")
    return f"https://pds-imaging.jpl.nasa.gov/{path.lstrip('/')}" if m and m[2] == "RAW" and role == "data" else url


def _num(v) -> float | None:
    return v if isinstance(v, (int, float)) and abs(v) <= 360 else None  # archive uses 999.9 / -1e32 as null


def _lon(v: float) -> float:
    return v - 360 if v > 180 else v


def _geometry(d: dict) -> tuple[dict | None, list | None]:
    corners = [(_num(d.get(f"{c}_LONGITUDE")), _num(d.get(f"{c}_LATITUDE")))
               for c in ("UPPER_LEFT", "UPPER_RIGHT", "LOWER_RIGHT", "LOWER_LEFT")]
    footprint = None
    if all(x is not None and y is not None and abs(y) <= 90 for x, y in corners):
        ring = [[_lon(x), y] for x, y in corners]
        if max(p[0] for p in ring) - min(p[0] for p in ring) < 180:  # otherwise it straddles the antimeridian
            footprint = {"type": "Polygon", "coordinates": [ring + ring[:1]]}
    w, e, s, n = (_num(d.get(k)) for k in ("MINIMUM_LONGITUDE", "MAXIMUM_LONGITUDE", "MINIMUM_LATITUDE", "MAXIMUM_LATITUDE"))
    if None in (w, e, s, n) or abs(s) > 90 or abs(n) > 90:
        return footprint, None
    return footprint, [_lon(w), s, _lon(e), n]


def _product(d: dict, collection_target: str | None) -> dict:
    targets = [t.lower() for t in d.get("TARGET", [])]
    target = collection_target if collection_target in targets else next((t for t in targets if t in TARGETS), collection_target)
    pid = d.get("PRODUCT_ID") or (d.get("identifier") or [d["uuid"]])[0]
    data, label = _fix(d.get("ATLAS_DATA_URL")), _fix(d.get("ATLAS_LABEL_URL"), "label")
    name = (d.get("FILE_NAME_SPECIFICATION") or (data or pid).split("?")[0]).rsplit("/", 1)[-1]
    assets = []
    if data:
        assets.append(asset(data, name, "data"))
    if label and "starbrite" not in (d.get("ATLAS_LABEL_URL") or ""):  # THEMIS QUB/IMG labels are embedded
        assets.append(asset(label, name.rsplit(".", 1)[0] + ".LBL", "label"))
    footprint, bbox = _geometry(d)
    instrument = (d.get("ATLAS_INSTRUMENT_NAME") or [None])[0]
    return result(
        id=d["uuid"], connector="pds-atlas", level="product", title=pid,
        description=d.get("DATA_SET_ID"), target=target, mission=d.get("ATLAS_MISSION_NAME"),
        instrument=instrument.upper() if instrument else None, start=d.get("START_TIME"), end=d.get("STOP_TIME"),
        footprint=footprint, bbox=bbox, thumbnail=_fix(d.get("ATLAS_THUMBNAIL_URL")), browse=_fix(d.get("ATLAS_BROWSE_URL")),
        assets=assets, landing=assets[-1]["url"] if assets else None,
        extra={k: d[k] for k in ("ATLAS_PRODUCT_TYPE", "MAP_SCALE") if d.get(k) is not None})


async def _products(q: Query, client: httpx.AsyncClient) -> dict:
    target, mission, instrument = q.target, q.mission, q.instrument
    if q.collection:
        target, mission, instrument = (q.collection.split("|") + ["", "", ""])[:3]
    fq = _target_fq(target or None)
    if mission:
        fq.append(f'ATLAS_MISSION_NAME:"{_clean(mission)}"')
    if instrument:
        fq.append(f'ATLAS_INSTRUMENT_NAME:"{_clean(instrument)}"')
    if q.text:
        fq.append(f"PRODUCT_ID:{_clean(q.text).replace(' ', '')}*")  # prefix only: a leading wildcard times out on 30 M rows
    if q.start or q.end:
        fq.append(f"START_TIME:[{_solr_time(q.start)} TO {_solr_time(q.end, True)}]")
    if q.bbox:
        w, s, e, n = q.bbox
        fq.append(f"MAXIMUM_LATITUDE:[{s} TO *] AND MINIMUM_LATITUDE:[* TO {n}]")
        fq.append(_lon_overlap(w, e))
    if not fq:
        raise ValueError("Image Atlas search needs at least a target, collection, text, time or area")
    data = await _select(client, {"fq": fq, "fl": FIELDS, "rows": q.limit, "start": q.offset, "sort": "START_TIME asc,uuid asc"})
    docs, total = data["response"]["docs"], data["response"]["numFound"]
    end = q.offset + len(docs)
    return page([_product(d, target or None) for d in docs], total, end if end < total else None)


async def search(conn_id: str, q: Query, client: httpx.AsyncClient) -> dict:
    check_times(q)
    return await (_collections if q.level == "collection" else _products)(q, client)
