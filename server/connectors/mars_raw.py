"""NASA rover/lander raw image feeds: Perseverance + Ingenuity, Curiosity, InSight (mars.nasa.gov JSON)."""
import asyncio
import html
from datetime import datetime, timezone

import httpx

from .base import Query, asset, page, result
from ._util import check_times, json_of

CONNECTORS = [{
    "id": "mars-raw", "name": "Mars rover raw images", "provider": "NASA JPL (Perseverance, Ingenuity, Curiosity, InSight)",
    "targets": ["mars"], "levels": ["collection", "product"], "supports": ["text", "time", "instrument"],
    "url": "https://mars.nasa.gov/raw-images/",
    "description": "Latest unprocessed camera images from the Mars surface missions, newest first. Filter by camera "
                   "(collection), date range (converted to sols, +-1) or camera-name text.",
}]

RSS = "https://mars.nasa.gov/rss/api/"          # Perseverance + Ingenuity
V1 = "https://mars.nasa.gov/api/v1/raw_image_items/"  # Curiosity + InSight
SOL_SECONDS = 88775.244
# (landing UTC used as sol 0, display name, cameras)
MISSIONS = {
    "mars2020": (datetime(2021, 2, 18, 20, 55, tzinfo=timezone.utc), "Mars 2020 Perseverance",
                 ["MCZ_LEFT", "MCZ_RIGHT", "NAVCAM_LEFT", "NAVCAM_RIGHT", "FRONT_HAZCAM_LEFT_A", "FRONT_HAZCAM_RIGHT_A",
                  "REAR_HAZCAM_LEFT", "REAR_HAZCAM_RIGHT", "SUPERCAM_RMI", "SKYCAM", "SHERLOC_WATSON", "PIXL_MCC",
                  "CACHECAM", "EDL_RUCAM", "EDL_DDCAM", "EDL_PUCAM1", "EDL_PUCAM2", "EDL_RDCAM", "LCAM"]),
    "ingenuity": (datetime(2021, 2, 18, 20, 55, tzinfo=timezone.utc), "Mars 2020 Ingenuity", ["HELI_NAV", "HELI_RTE"]),
    "msl": (datetime(2012, 8, 6, 5, 17, tzinfo=timezone.utc), "Mars Science Laboratory Curiosity",
            ["MAST_LEFT", "MAST_RIGHT", "MAHLI", "MARDI", "CHEMCAM_RMI", "NAV_LEFT_A", "NAV_RIGHT_A", "NAV_LEFT_B",
             "NAV_RIGHT_B", "FHAZ_LEFT_A", "FHAZ_RIGHT_A", "FHAZ_LEFT_B", "FHAZ_RIGHT_B", "RHAZ_LEFT_A",
             "RHAZ_RIGHT_A", "RHAZ_LEFT_B", "RHAZ_RIGHT_B"]),
    "insight": (datetime(2018, 11, 26, 19, 54, tzinfo=timezone.utc), "InSight", ["icc", "idc"]),
}


def _sol(iso: str, mission: str) -> int:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((dt - MISSIONS[mission][0]).total_seconds() // SOL_SECONDS)


def _conditions(q: Query, mission: str, camera: str | None) -> list[str]:
    """condition_N values; sol:gte must precede sol:lte or the feed ignores the upper bound."""
    sols = []
    if q.start or q.end:
        sols = [f"{max(0, _sol(q.start, mission) - 1) if q.start else 0}:sol:gte"]
        if q.end:
            sols.append(f"{_sol(q.end, mission) + 1}:sol:lte")
    cond = sols
    if mission in ("msl", "insight") and camera:
        cond.append(f"{camera}:instrument")
    return cond


async def _get(client, mission: str, per_page: int, page_no: int, conds: list[str], search: str | None, camera=None):
    if mission in ("mars2020", "ingenuity"):
        params = {"feed": "raw_images", "feedtype": "json", "category": mission, "num": per_page, "page": page_no,
                  "order": "sol desc"}
        if camera or search:
            params["search"] = camera or search
        url = RSS
    else:
        params = {"order": "sol desc", "per_page": per_page, "page": page_no, "condition_1": f"{mission}:mission"}
        if search and not camera:
            params["search"] = search
        url = V1
    for i, c in enumerate(conds):
        params[f"condition_{2 + i}"] = c
    r = await client.get(url, params=params, timeout=25)
    r.raise_for_status()
    d = json_of(r)
    items = d.get("images", d.get("items", []))
    return items, d.get("total_results", d.get("total"))


async def _collections(q: Query, client):
    sem = asyncio.Semaphore(8)

    async def count(mission, cam):
        async with sem:
            try:
                _, total = await _get(client, mission, 1, 0, _conditions(q, mission, cam), q.text or None, cam)
            except (httpx.HTTPError, ValueError):
                return mission, cam, None
            return mission, cam, total

    counts = await asyncio.gather(*(count(m, c) for m, (_, _, cams) in MISSIONS.items() for c in cams))
    rows = [result(
        id=f"{m}|{c}", connector="mars-raw", level="collection", title=f"{MISSIONS[m][1]} - {c}",
        description=f"{n:,} raw images" + (" (note: counts include thumbnails for Curiosity/InSight)" if m in ("msl", "insight") else ""),
        target="mars", mission=MISSIONS[m][1], instrument=c, landing=V1.rsplit("/api", 1)[0] + "/raw-images/",
        collection=f"{m}|{c}", extra={"images": n}) for m, c, n in counts if n]
    notes = [f"{sum(1 for *_, n in counts if n is None)} camera counts unavailable"] if any(n is None for *_, n in counts) else []
    end = q.offset + q.limit
    return page(rows[q.offset:end], len(rows), end if len(rows) > end else None, notes)


def _iso(s: str | None) -> str | None:
    if not s:
        return None
    return s.split(".")[0].rstrip("Z") + "Z"


def _rss_item(i: dict, mission: str) -> dict:
    f, cam = i["image_files"], i["camera"]
    ex = i.get("extended", {})
    assets = [asset(f["full_res"], role="data", type="image/png" if f["full_res"].endswith(".png") else "image/jpeg")]
    if f.get("large"):
        assets.append(asset(f["large"], role="browse", type="image/jpeg"))
    return result(
        id=i["imageid"], connector="mars-raw", title=i["title"], target="mars", mission=MISSIONS[mission][1],
        instrument=cam["instrument"], description=html.unescape(i.get("caption") or "").strip()[:400] or None,
        start=_iso(i.get("date_taken_utc")), thumbnail=f.get("small"), browse=f.get("large") or f.get("medium"),
        assets=assets, landing=i.get("link"), collection=None,
        extra={k: v for k, v in {
            "sol": i.get("sol"), "site": i.get("site"), "drive": i.get("drive"), "sample_type": i.get("sample_type"),
            "filter": cam.get("filter_name"), "credit": i.get("credit"), "dimension": ex.get("dimension"),
            "mast_az": ex.get("mastAz"), "mast_el": ex.get("mastEl"), "received": i.get("date_received")}.items()
            if v not in (None, "")})


def _v1_item(i: dict, mission: str) -> dict:
    url = i.get("https_url") or i["url"]
    ex = i.get("extended") or {}
    return result(
        id=i["imageid"], connector="mars-raw", title=i["title"], target="mars", mission=MISSIONS[mission][1],
        instrument=i["instrument"], description=html.unescape(i.get("description") or "").replace("<br />", " ").strip()[:400] or None,
        start=_iso(i.get("date_taken")), thumbnail=url, browse=url, assets=[asset(url, role="data", type="image/jpeg")],
        landing="https://mars.nasa.gov" + i["link"] if i.get("link") else None,
        extra={k: v for k, v in {
            "sol": i.get("sol"), "sample_type": ex.get("sample_type"), "credit": i.get("image_credit"),
            "filter": ex.get("filter_name"), "received": i.get("date_received")}.items() if v not in (None, "")})


async def _products(q: Query, client):
    mission, _, camera = (q.collection or "mars2020|").partition("|")
    if mission not in MISSIONS:
        raise ValueError(f"collection must be '<{'|'.join(MISSIONS)}>|<camera>' from a collection-level result")
    per_page = min(q.limit, 100)  # the feeds cap pages at 100
    if q.offset % per_page:
        raise ValueError("offset must be a multiple of limit for the rover feeds")
    camera = camera or q.instrument
    items, total = await _get(client, mission, per_page, q.offset // per_page,
                              _conditions(q, mission, camera), q.text or None, camera)
    notes = []
    if mission in ("msl", "insight"):
        kept = [i for i in items if not i.get("is_thumbnail")]
        if len(kept) < len(items):
            notes.append(f"{len(items) - len(kept)} thumbnail-only items hidden")
        items = kept
    if q.start or q.end:
        notes.append("date range converted to sols (+-1 sol)")
    if q.text and camera:
        notes.append("text is ignored while a camera is selected (the feeds take one or the other)")
    make = _rss_item if mission in ("mars2020", "ingenuity") else _v1_item
    end = q.offset + per_page
    return page([make(i, mission) for i in items], total, end if total and total > end else None, notes)


async def search(conn_id: str, q: Query, client: httpx.AsyncClient) -> dict:
    check_times(q)
    return await (_collections(q, client) if q.level == "collection" else _products(q, client))
