"""STScI MAST (HST, JWST, HLA, IUE, ...) observations of solar-system targets."""
import collections
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from .base import Query, asset, page, result
from .base import target_id, target_variants
from ._util import check_times, json_of

CONNECTORS = [{
    "id": "mast", "name": "MAST (HST / JWST / HLA / IUE ...)", "provider": "Space Telescope Science Institute",
    "targets": ["mercury", "venus", "moon", "mars", "phobos", "deimos", "ceres", "vesta", "jupiter", "io",
                "europa", "ganymede", "callisto", "saturn", "mimas", "enceladus", "tethys", "dione", "rhea",
                "titan", "iapetus", "uranus", "titania", "oberon", "neptune", "triton", "pluto", "charon",
                "psyche", "lutetia", "steins", "donaldjohanson", "eros", "itokawa", "bennu",
                "didymos", "dimorphos", "toutatis", "borrelly", "wild-2", "tempel-1", "hartley-2", "67p", "3i-atlas",
                "amalthea", "himalia", "hyperion", "phoebe", "epimetheus", "pandora", "miranda", "ariel", "umbriel",
                "proteus", "nereid", "hydra"],
    "levels": ["collection", "product"], "supports": ["text", "time", "instrument"],
    "url": "https://mast.stsci.edu/",
    "description": "Space-telescope images and cubes of planets, moons and small bodies. Targets are matched by "
                   "MAST target name; science FITS and JPEG previews are downloadable. Text matches obs_id exactly.",
}]

API = "https://mast.stsci.edu/api/v0/invoke"
DOWNLOAD = "https://mast.stsci.edu/api/v0.1/Download/file?uri="
IMAGE_TYPES = ["image", "IMAGE", "cube"]
PRODUCTS_TIMEOUT = 20
MAX_SCIENCE_FILES = 8
ALIASES = {"67p": {"67P/CHURYUMOV-GERASIMENKO"}}  # MAST stores the full designation; filters are exact (case-insensitive)


def _download(uri: str) -> str:
    if uri.startswith("http"):  # HLA previews, Spitzer ...: the MAST download service answers 400 for these
        return uri
    return DOWNLOAD + quote(uri, safe=":/")


def _mjd(iso: str) -> float:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return 40587 + dt.timestamp() / 86400


def _iso(mjd) -> str | None:
    try:
        return (datetime(1858, 11, 17, tzinfo=timezone.utc) + timedelta(days=float(mjd))).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError):
        return None


async def _invoke(client: httpx.AsyncClient, service: str, params: dict, timeout: float, **top) -> dict:
    r = await client.post(API, data={"request": json.dumps({"service": service, "format": "json", "params": params,
                                                            **top})}, timeout=timeout)
    r.raise_for_status()
    d = json_of(r)
    if d.get("status") not in ("COMPLETE", "EXECUTING"):
        raise ValueError(f"MAST error: {d.get('msg') or d.get('status')}")
    return d


def _filters(q: Query, with_collection: bool) -> list[dict]:
    name = q.target or q.text
    if not name:
        raise ValueError("MAST search needs a target (or text naming the target)")
    target = target_id(name)
    names = {n.upper() for n in target_variants(target)} | ALIASES.get(target, set())
    f = [{"paramName": "target_name", "values": sorted(names)},
         {"paramName": "dataproduct_type", "values": IMAGE_TYPES}]
    if q.target and q.text:
        f.append({"paramName": "obs_id", "values": [q.text]})  # MAST filters take no wildcards
    if q.start:
        f.append({"paramName": "t_max", "values": [{"min": _mjd(q.start), "max": 99999}]})
    if q.end:
        f.append({"paramName": "t_min", "values": [{"min": 0, "max": _mjd(q.end)}]})
    if q.instrument:
        f.append({"paramName": "instrument_name", "values": [q.instrument]})
    if q.mission:
        f.append({"paramName": "obs_collection", "values": [q.mission]})
    if with_collection and q.collection:
        coll, _, inst = q.collection.partition("|")
        f.append({"paramName": "obs_collection", "values": [coll]})
        if inst:
            f.append({"paramName": "instrument_name", "values": [inst]})
    return f


async def _collections(q: Query, client):
    d = await _invoke(client, "Mast.Caom.Filtered", {"columns": "obs_collection,instrument_name",
                                                      "filters": _filters(q, False)}, 30)
    counts = collections.Counter((r["obs_collection"], r["instrument_name"]) for r in d["data"])
    target = target_id(q.target or q.text)
    rows = [result(
        id=f"{c}|{i or ''}", connector="mast", level="collection", title=f"{c} {i}".strip(),
        description=f"{n} image/cube observations of {target}", target=target, mission=c, instrument=i,
        landing="https://mast.stsci.edu/", collection=f"{c}|{i or ''}", extra={"observations": n})
        for (c, i), n in counts.most_common()]
    end = q.offset + q.limit
    notes = [] if len(d["data"]) == d["paging"]["rowsFiltered"] else ["counts truncated by MAST"]
    return page(rows[q.offset:end], len(rows), end if len(rows) > end else None, notes)


def _science_assets(products: list[dict]) -> list[dict]:
    sci = [p for p in products if p["productType"] == "SCIENCE" and p["productFilename"].lower().endswith(".fits")]
    sci.sort(key=lambda p: -(p["calib_level"] or 0))
    return [asset(_download(p["dataURI"]), p["productFilename"], "data", "image/fits", p["size"])
            for p in sci[:MAX_SCIENCE_FILES]]


def _product(o: dict, products: list[dict]) -> dict:
    assets = _science_assets(products)
    if not assets and o.get("dataURL") and o["dataURL"].lower().endswith(".fits"):  # Products call failed/empty
        assets = [asset(_download(o["dataURL"]), o["dataURL"].rsplit("/", 1)[-1], "data", "image/fits")]
    preview = _download(o["jpegURL"]) if o.get("jpegURL") else None
    if preview:
        assets.append(asset(preview, o["jpegURL"].rsplit("/", 1)[-1], "browse", "image/jpeg", openable=True))
    return result(
        id=str(o["obsid"]), connector="mast", level="product",
        title=f"{o['obs_id']} - {o['target_name']}" + (f" ({o['filters']})" if o.get("filters") else ""),
        description=o.get("obs_title"), target=target_id(o["target_name"]) if o.get("target_name") else None,
        mission=o["obs_collection"], instrument=o.get("instrument_name"), start=_iso(o.get("t_min")),
        end=_iso(o.get("t_max")), thumbnail=preview, browse=preview, assets=assets, landing=None,
        extra={k: o[k] for k in ("proposal_id", "proposal_pi", "filters", "calib_level", "t_exptime",
                                 "dataproduct_type", "target_classification", "dataRights") if o.get(k) is not None})


async def _products(q: Query, client):
    if q.offset % q.limit:
        raise ValueError("offset must be a multiple of limit for MAST")
    d = await _invoke(client, "Mast.Caom.Filtered", {"columns": "*", "filters": _filters(q, True)}, 30,
                      pagesize=q.limit, page=q.offset // q.limit + 1)
    obs, notes = d["data"], []
    by_obs = collections.defaultdict(list)
    if obs:
        try:
            p = await _invoke(client, "Mast.Caom.Products", {"obsid": ",".join(str(o["obsid"]) for o in obs)},
                              PRODUCTS_TIMEOUT)
            for row in p["data"]:
                by_obs[int(row["obsID"])].append(row)
        except httpx.TimeoutException:
            notes.append("MAST product list timed out; showing primary data URLs only")
    total = d["paging"]["rowsFiltered"]
    end = q.offset + q.limit
    return page([_product(o, by_obs.get(o["obsid"], [])) for o in obs], total, end if total > end else None, notes)


async def search(conn_id: str, q: Query, client: httpx.AsyncClient) -> dict:
    check_times(q)
    return await (_collections(q, client) if q.level == "collection" else _products(q, client))
