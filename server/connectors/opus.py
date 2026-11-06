"""PDS Ring-Moon Systems Node OPUS connector.

Collection = (target, instrument) with an observation count, id "<vocab target id>|<OPUS instrument name>".
Product = one OPUS observation with raw/calibrated files, previews and a link to the OPUS detail page.
API: https://opus.pds-rings.seti.org/opus/api/  (data.json, files.json, meta/mults/<field>.json)
"""
import asyncio
import json
import re
import time

import httpx

from ..config import CATALOG_DIR
from .base import Query, asset, page, result
from ._util import check_times, json_of

API = "https://opus.pds-rings.seti.org/opus/api"
TIMEOUT = httpx.Timeout(25, connect=10)
TTL = 3600

CONNECTORS = [{
    "id": "opus", "name": "PDS Ring-Moon Systems OPUS", "provider": "NASA PDS Ring-Moon Systems Node (SETI Institute)",
    "targets": ["sun", "earth", "moon", "mars", "venus", "jupiter", "io", "europa", "ganymede", "callisto", "amalthea",
                "himalia", "saturn", "mimas", "enceladus", "tethys", "dione", "rhea", "titan", "hyperion", "iapetus",
                "phoebe", "janus", "epimetheus", "prometheus", "pandora", "pan", "uranus", "miranda", "ariel",
                "umbriel", "titania", "oberon", "neptune", "triton", "proteus", "nereid", "pluto", "charon", "nix",
                "hydra", "arrokoth", "gaspra", "ida", "asteroids", "comets", "kbos", "rings"],
    "levels": ["collection", "product"], "supports": ["text", "time", "instrument"],
    "url": "https://opus.pds-rings.seti.org/opus/",
    "description": "Cassini ISS/VIMS/UVIS/CIRS, Voyager, Galileo, New Horizons, Hubble and ground-based observations of "
                   "the outer planets, their moons and rings. Text matches the OPUS ID (e.g. 'n14547'). No footprints.",
}]

_VOCAB = json.loads((CATALOG_DIR / "vocab.json").read_text("utf-8"))["targets"]
_NAME_TO_ID = {name.lower(): tid for tid, name in _VOCAB.items()}
_KBO_NAMES = {"chariklo", "chiron", "pholus", "huya", "ixion", "orcus", "quaoar", "sedna", "makemake", "haumea", "eris", "varda"}
_cache: dict[str, tuple[float, dict]] = {}


def target_id(opus_name: str) -> str | None:
    n = opus_name.strip().lower()
    if n in _NAME_TO_ID:
        return _NAME_TO_ID[n]
    if n == "2014 mu69":  # Arrokoth's provisional designation
        return "arrokoth"
    if re.search(r"\brings?$", n):
        return "rings"
    if n == "shoemaker levy 9":
        return "comets"
    if n in _KBO_NAMES or re.fullmatch(r"\d{4} [a-z]{1,2}\d+", n):
        return "kbos"
    if n == "masursky":
        return "asteroids"
    return None


def mission_of(instrument: str) -> str:
    return instrument.rsplit(" ", 1)[0]  # "Cassini ISS" -> "Cassini"


def _iso(t: str | None) -> str | None:
    return t + "Z" if t and not t.endswith("Z") else t


async def _get(client: httpx.AsyncClient, path: str, **params) -> dict:
    r = await client.get(f"{API}/{path}", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return json_of(r)


async def _mults(client: httpx.AsyncClient, field: str, names: list[str] | None = None) -> dict[str, int]:
    key = f"{field}|{names}"
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    mults = (await _get(client, f"meta/mults/{field}.json", **({"target": ",".join(names)} if names else {})))["mults"]
    mults = {k: v for k, v in mults.items() if v}
    _cache[key] = (time.monotonic(), mults)
    return mults


async def _opus_targets(client: httpx.AsyncClient, vocab_id: str) -> list[str]:
    return [n for n in await _mults(client, "target") if target_id(n) == vocab_id]


async def _collections(q: Query, client: httpx.AsyncClient) -> dict:
    names = await _opus_targets(client, q.target) if q.target else None
    if q.target and not names:
        return page([], 0, None, [f"OPUS has no observations of {q.target}"])
    instruments = await _mults(client, "instrument", names)
    words = q.text.lower().split()
    hits = [(i, n) for i, n in instruments.items()
            if all(w in i.lower() or (names and any(w in x.lower() for x in names)) for w in words)
            and (not q.instrument or q.instrument.lower() in i.lower())
            and (not q.mission or q.mission.lower() in i.lower())]
    hits.sort(key=lambda h: -h[1])
    shown = hits[q.offset:q.offset + q.limit]
    out = [result(id=f"{q.target or ''}|{i}", connector="opus", level="collection",
                  title=f"{i} — {_VOCAB.get(q.target, 'all targets')}", description=f"{n:,} observations",
                  target=q.target, mission=mission_of(i), instrument=i, landing="https://opus.pds-rings.seti.org/opus/",
                  collection=f"{q.target or ''}|{i}", extra={"observations": n}) for i, n in shown]
    end = q.offset + len(shown)
    return page(out, len(hits), end if end < len(hits) else None)


def _assets(files: dict) -> tuple[list, str | None, str | None]:
    assets, thumb, browse = [], None, None
    for cat, urls in files.items():
        if re.search(r"documentation|index|inventory|geometry|hst_text", cat):
            continue
        for url in urls:
            name = url.rsplit("/", 1)[-1]
            ext = name.lower().rsplit(".", 1)[-1]
            if cat == "browse_thumb":
                role, thumb = "thumbnail", url
            elif cat.startswith("browse_") or re.search(r"_(thumb|medium|full|browse)$", cat):
                role = "browse"
                if cat in ("browse_medium", "browse_small") and (browse is None or cat == "browse_medium"):
                    browse = url
            elif ext == "fmt":
                role = "metadata"
            else:
                role = "label" if ext in ("lbl", "xml") else "data"
            assets.append(asset(url, name, role, type=cat if role == "data" else None))
    return assets, thumb, browse


async def _products(q: Query, client: httpx.AsyncClient) -> dict:
    target, instrument = q.target, q.instrument
    if q.collection:
        target, _, instrument = q.collection.partition("|")
    params = {}
    if target:
        names = await _opus_targets(client, target)
        if not names:
            return page([], 0, None, [f"OPUS has no observations of {target}"])
        params["target"] = ",".join(names)
    if instrument:
        params["instrument"] = instrument
    if q.mission:  # OPUS has a mission facet; instrument names are prefixed with it
        params["mission"] = q.mission
    if q.start:
        params["time1"] = q.start
    if q.end:
        params["time2"] = q.end
    if q.text:
        params["opusid"] = q.text.strip()
    params.update(limit=q.limit, startobs=q.offset + 1)
    try:
        data, files = await asyncio.gather(
            _get(client, "data.json", cols="opusid,target,instrument,time1,time2,ringobsid", **params),
            _get(client, "files.json", **params))
    except httpx.HTTPStatusError:  # OPUS answers 5xx when startobs is past the last observation
        if not q.offset:
            raise
        first = await _get(client, "data.json", cols="opusid", **{**params, "limit": 1, "startobs": 1})
        if first["available"] > q.offset:
            raise
        return page([], first["available"], None, ["offset is past the last observation"])
    out = []
    for opusid, tgt, inst, t1, t2, ringobs in data["page"]:
        assets, thumb, browse = _assets(files["data"].get(opusid, {}))
        ids = [i for i in map(target_id, tgt.split(",")) if i]
        out.append(result(
            id=opusid, connector="opus", level="product", title=opusid, description=f"{inst} observation of {tgt}",
            target=target if target in ids else (ids[0] if ids else target), mission=mission_of(inst), instrument=inst,
            start=_iso(t1), end=_iso(t2), thumbnail=thumb, browse=browse or thumb, assets=assets,
            landing=f"https://opus.pds-rings.seti.org/opus/#/view=detail&detail={opusid}",
            extra={"target name": tgt, "ring obs id": ringobs}))
    total, end = data["available"], q.offset + len(out)
    return page(out, total, end if end < total else None)


async def search(conn_id: str, q: Query, client: httpx.AsyncClient) -> dict:
    check_times(q)
    return await (_collections if q.level == "collection" else _products)(q, client)
