"""Europlanet VESPA EPN-TAP federation: services discovered via IVOA RegTAP, probed per target."""
import asyncio
import io
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
from astropy.io.votable import parse_single_table

from ..config import CACHE_DIR
from .base import Query, asset, is_openable, known_target, page, result, target_id, target_variants
from ._util import check_times

CONNECTORS = [{
    "id": "epntap", "name": "VESPA EPN-TAP (Europlanet)", "provider": "Europlanet VESPA / IVOA registry (incl. ESA PSA)",
    "targets": ["*"], "levels": ["collection", "product"], "needs_collection": True,
    "supports": ["text", "bbox", "time", "instrument"],
    "url": "https://vespa.obspm.fr/",
    "description": "Federated search over every EPN-TAP planetary data service in the IVOA registry; "
                   "collection level probes each service for the target, product level queries one table.",
}]

REGISTRIES = ["https://registry.euro-vo.org/regtap/tap/sync",
              "http://reg.g-vo.org/tap/sync"]  # reg.g-vo.org has a broken https certificate
REGISTRY_QUERY = """SELECT DISTINCT t.table_name, i.access_url, r.res_title, t.table_description
FROM rr.res_table AS t JOIN rr.resource AS r USING (ivoid) JOIN rr.interface AS i USING (ivoid)
WHERE t.table_name LIKE '%epn_core' AND i.intf_type='vs:paramhttp' AND i.intf_role='std'
AND r.res_type='vs:catalogservice' AND i.access_url LIKE 'http%/tap' AND i.access_url NOT LIKE '%/__system__/%'"""
SERVICES_FILE = CACHE_DIR / "epntap_services.json"
SERVICES_TTL = 6 * 3600
PROBE_TIMEOUT = 8
PROBE_DEADLINE = 25  # whole collection-level fan-out must finish inside the 30 s connector budget
PROBE_TTL = 3600
IMAGE_TYPES = ("im", "ci", "ma", "sc")  # EPN-TAP dataproduct_type codes: image, image cube, map, spectral cube
OPENABLE_FORMATS = {"image/fits", "application/fits", "image/tiff", "image/jp2"}

_services: tuple[float, list[dict]] | None = None
_probes: dict[tuple, tuple[float, dict | None]] = {}
def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _rows(content: bytes) -> list[dict]:
    """VOTable bytes -> list of dicts, masked/empty cells None. Raises on ADQL error documents."""
    table = parse_single_table(io.BytesIO(content)).to_table()
    out = []
    for row in table:
        d = {}
        for k in table.colnames:
            v = row[k]
            if getattr(v, "mask", False) is True:
                v = None
            elif hasattr(v, "item"):
                v = v.item()
            if isinstance(v, bytes):
                v = v.decode("utf-8", "replace")
            d[k] = None if v == "" else v
        out.append(d)
    return out


async def _tap(client: httpx.AsyncClient, url: str, adql: str, timeout: float) -> list[dict]:
    r = await client.post(url + "/sync", data={"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": adql},
                          timeout=timeout)
    return _rows(r.content)  # DaCHS reports ADQL errors as HTTP 500 + VOTable; parsing raises then


async def services(client: httpx.AsyncClient) -> list[dict]:
    global _services
    if _services and time.time() - _services[0] < SERVICES_TTL:
        return _services[1]
    if SERVICES_FILE.exists() and time.time() - SERVICES_FILE.stat().st_mtime < SERVICES_TTL:
        try:
            _services = (time.time(), json.loads(SERVICES_FILE.read_text("utf-8")))
            return _services[1]
        except ValueError:
            pass
    last = None
    for reg in REGISTRIES:
        try:
            r = await client.post(reg, data={"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": REGISTRY_QUERY},
                                  timeout=40)
            rows = _rows(r.content)
            break
        except Exception as e:  # try the next registry mirror
            last = e
    else:
        if _services:
            return _services[1]  # stale beats nothing
        raise httpx.HTTPError(f"IVOA registry unreachable: {last}")
    seen, out = set(), []
    for r in rows:
        key = (r["access_url"], r["table_name"])
        if key not in seen:
            seen.add(key)
            out.append({"tap": r["access_url"], "table": r["table_name"], "title": r["res_title"],
                        "description": r["table_description"]})
    SERVICES_FILE.write_text(json.dumps(out), "utf-8")
    _services = (time.time(), out)
    return out


def _target_cond(target: str) -> str:
    return "target_name IN (" + ", ".join(_lit(n) for n in target_variants(target)) + ")"


async def _probe(client, svc, target, sem, dead):
    key = (svc["tap"], svc["table"], target)
    hit = _probes.get(key)
    if hit and time.time() - hit[0] < PROBE_TTL:
        return hit[1]
    async with sem:
        if svc["tap"] in dead:  # host already failed during this fan-out
            raise httpx.ConnectTimeout("skipped: host unreachable")
        try:
            rows = await _tap(client, svc["tap"],
                              f"SELECT TOP 1 granule_uid, dataproduct_type, instrument_host_name, instrument_name "
                              f"FROM {svc['table']} WHERE {_target_cond(target)}", PROBE_TIMEOUT)
        except httpx.TransportError:
            dead.add(svc["tap"])
            raise  # reported in notes; not cached
        except Exception:  # service down / table lacks columns: treated as "no data"
            rows = []
    _probes[key] = (time.time(), rows[0] if rows else None)
    return rows[0] if rows else None


def _jd(iso: str) -> float:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return 2440587.5 + dt.timestamp() / 86400


def _iso(jd) -> str | None:
    try:
        return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(days=float(jd) - 2440587.5)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError):
        return None


def _lon_intervals(w: float, e: float) -> list[tuple[float, float]]:
    """bbox longitudes -> intervals in both the -180..180 and 0..360 conventions (services mix them)."""
    segs = [(w, e)] if w <= e else [(w, 180.0), (-180.0, e)]
    out = []
    for lo, hi in segs:
        out.append((lo, hi))
        if hi <= 0:
            out.append((lo + 360, hi + 360))
        elif lo < 0:
            out += [(0.0, hi), (lo + 360, 360.0)]
    return out


def _norm_lon(x: float, upper: bool) -> float:
    x = (x + 180) % 360 - 180
    return 180.0 if upper and x == -180 else x


def _bbox_footprint(r: dict):
    keys = ("c1min", "c1max", "c2min", "c2max")
    if r.get("spatial_frame_type") != "body" or any(r.get(k) is None for k in keys):
        return None, None
    c1min, c1max, s, n = (float(r[k]) for k in keys)
    if not -90 <= s <= n <= 90:
        return None, None
    if c1max - c1min >= 359.9:
        w, e = -180.0, 180.0
    else:
        w, e = _norm_lon(c1min, False), _norm_lon(c1max, True)

    def ring(a, b):
        return [[a, s], [b, s], [b, n], [a, n], [a, s]]
    geom = ({"type": "Polygon", "coordinates": [ring(w, e)]} if w <= e else
            {"type": "MultiPolygon", "coordinates": [[ring(w, 180.0)], [ring(-180.0, e)]]})
    return geom, [w, s, e, n]


def _data_asset(r: dict):
    url, fmt = r.get("access_url"), r.get("access_format") or ""
    if not url:
        return None
    size = int(float(r["access_estsize"]) * 1024) if r.get("access_estsize") is not None else None
    openable = is_openable(url) or fmt.split("#")[0].lower() in OPENABLE_FORMATS
    name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or r["granule_uid"]
    return asset(url, name, "data", fmt or None, size, openable)


def _product(r: dict, svc: dict, collection: str) -> dict:
    footprint, bbox = _bbox_footprint(r)
    data = _data_asset(r)
    assets = [data] if data else []
    if r.get("label_url"):
        assets.append(asset(r["label_url"], role="label"))
    thumb = r.get("thumbnail_url")
    if thumb:
        assets.append(asset(thumb, role="thumbnail"))
    extra = {k: r[k] for k in ("dataproduct_type", "processing_level", "target_name", "target_class",
                               "spatial_frame_type", "access_format", "granule_gid") if r.get(k) is not None}
    return result(
        id=f"{collection}|{r['granule_uid']}", connector="epntap", level="product",
        title=r.get("obs_id") or r["granule_uid"], description=svc.get("title"),
        target=known_target(r.get("target_name")), mission=r.get("instrument_host_name"),
        instrument=r.get("instrument_name"), start=_iso(r.get("time_min")), end=_iso(r.get("time_max")),
        footprint=footprint, bbox=bbox, thumbnail=thumb, browse=thumb, assets=assets,
        landing=svc["tap"].removesuffix("/tap"), extra=extra)


async def _collections(q: Query, client):
    name = q.target or q.text
    if not name:
        raise ValueError("collection level needs a target (or text naming a body)")
    target = target_id(name)
    svcs = [s for s in await services(client) if "/__system__/" not in s["tap"]]  # registry LIKE filter lets these through
    sem, dead = asyncio.Semaphore(10), set()
    tasks = {asyncio.ensure_future(_probe(client, s, target, sem, dead)): s for s in svcs}
    done, pending = await asyncio.wait(tasks, timeout=PROBE_DEADLINE)
    for t in pending:
        t.cancel()
    results, timed_out = [], [tasks[t] for t in pending]
    for t in done:
        s = tasks[t]
        if t.exception():
            timed_out.append(s)  # only transport errors propagate out of _probe
            continue
        hit = t.result()
        if not hit:
            continue
        coll = f"{s['tap']}|{s['table']}"
        results.append(result(
            id=coll, connector="epntap", level="collection", title=s["table"].removesuffix(".epn_core"),
            description=(s["description"] or s["title"] or "").strip()[:300] or None, target=target,
            mission=hit.get("instrument_host_name"), instrument=hit.get("instrument_name"),
            landing=s["tap"].removesuffix("/tap"), collection=coll,
            extra={"service": s["title"], "sample_dataproduct_type": hit.get("dataproduct_type")}))
    # image-like collections first
    results.sort(key=lambda r: (not any(t in (r["extra"]["sample_dataproduct_type"] or "") for t in IMAGE_TYPES), r["id"]))
    notes = [f"{len(svcs)} EPN-TAP tables probed, {len(results)} have data for '{target}'"]
    if timed_out:
        notes.append("timed out: " + ", ".join(sorted({f"{s['table']} @ {s['tap']}" for s in timed_out})))
    end = q.offset + q.limit
    return page(results[q.offset:end], len(results), end if len(results) > end else None, notes)


def _conditions(q: Query) -> list[str]:
    c = []
    if q.target:
        c.append(_target_cond(target_id(q.target)))
    if q.start:
        c.append(f"time_max >= {_jd(q.start)}")
    if q.end:
        c.append(f"time_min <= {_jd(q.end)}")
    if q.bbox:
        w, s, e, n = q.bbox
        lon = " OR ".join(f"(c1max >= {lo} AND c1min <= {hi})" for lo, hi in _lon_intervals(w, e))
        c.append(f"spatial_frame_type = 'body' AND c2max >= {s} AND c2min <= {n} AND ({lon})")
    if q.text:
        like = _lit(f"%{q.text}%")
        c.append(f"(obs_id ILIKE {like} OR granule_uid ILIKE {like} OR instrument_name ILIKE {like})")
    if q.instrument:
        c.append(f"instrument_name ILIKE {_lit('%' + q.instrument + '%')}")
    if q.mission:
        c.append(f"instrument_host_name ILIKE {_lit('%' + q.mission + '%')}")
    return c


async def _products(q: Query, client):
    svc_url, _, table = (q.collection or "").partition("|")
    svc = next((s for s in await services(client) if s["tap"] == svc_url and s["table"] == table), None)
    if not svc:
        raise ValueError("collection must be 'tap_url|table' taken from a collection-level result")
    top = min(q.offset + q.limit + 1, 2000)  # ADQL 2.0 has no OFFSET: fetch one past the page and slice
    base = _conditions(q)
    images = "(" + " OR ".join(f"dataproduct_type LIKE '%{t}%'" for t in IMAGE_TYPES) + ")"

    async def run(conds, n=top):
        where = " WHERE " + " AND ".join(conds) if conds else ""
        return await _tap(client, svc_url, f"SELECT TOP {n} * FROM {table}{where} ORDER BY granule_uid", 28)  # stable paging

    notes = []
    rows = await run(base + [images])
    # table holds only non-image products (spectra, time series, ...): decide the same way on every page
    if not rows and not (q.offset and await run(base + [images], 1)):
        rows = await run(base)
        if rows:
            notes.append("no image/cube/map products matched; showing all product types")
    end = q.offset + q.limit
    return page([_product(r, svc, q.collection) for r in rows[q.offset:end]], None,
                end if len(rows) > end else None, notes)


async def search(conn_id: str, q: Query, client: httpx.AsyncClient) -> dict:
    check_times(q)
    return await (_collections(q, client) if q.level == "collection" else _products(q, client))
