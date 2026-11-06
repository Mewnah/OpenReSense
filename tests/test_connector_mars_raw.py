import asyncio

import httpx
import pytest

from server.connectors import mars_raw
from server.connectors.base import Query

RSS = {"imageid": "ZL0_X", "title": "Sol 2024", "sol": 2024, "caption": "cap &amp; more", "date_taken_utc": "2026-10-30T09:21:53.576",
       "image_files": {"full_res": "https://m/x.png", "large": "https://m/x_1200.jpg", "medium": "https://m/x_800.jpg", "small": "https://m/x_320.jpg"},
       "camera": {"instrument": "MCZ_LEFT", "filter_name": "F"}, "link": "https://m/page", "extended": {}}
V1 = {"imageid": "5065MH", "title": "Sol 5065", "sol": 5065, "instrument": "MAHLI", "url": "https://m/a.jpg", "https_url": "https://m/a.jpg",
      "date_taken": "2026-11-05T17:17:08.000Z", "description": "d<br />e", "link": "/raw_images/1", "extended": {"sample_type": "subframe"}}


def test_mapping():
    r = mars_raw._rss_item(RSS, "mars2020")
    assert r["start"] == "2026-10-30T09:21:53Z" and r["assets"][0]["openable"] and r["thumbnail"].endswith("_320.jpg")
    assert r["description"] == "cap & more" and r["extra"]["sol"] == 2024
    r = mars_raw._v1_item(V1, "msl")
    assert r["start"] == "2026-11-05T17:17:08Z" and r["landing"] == "https://mars.nasa.gov/raw_images/1"


def test_sol_range_conditions_order():
    q = Query(start="2023-01-01T00:00:00Z", end="2023-01-03T00:00:00Z")
    c = mars_raw._conditions(q, "mars2020", None)
    assert c[0].endswith(":sol:gte") and c[1].endswith(":sol:lte")
    assert 660 <= int(c[0].split(":")[0]) <= 665
    assert mars_raw._conditions(Query(), "msl", "MAHLI") == ["MAHLI:instrument"]


def test_unknown_collection():
    async def run():
        async with httpx.AsyncClient() as c:
            await mars_raw.search("mars-raw", Query(collection="nope|x"), c)
    with pytest.raises(ValueError):
        asyncio.run(run())


@pytest.mark.network
def test_live_latest_perseverance_mastcamz():
    async def run():
        async with httpx.AsyncClient(follow_redirects=True) as c:
            return await mars_raw.search("mars-raw", Query(collection="mars2020|MCZ_LEFT", limit=5), c)
    p = asyncio.run(run())
    assert len(p["results"]) == 5 and p["results"][0]["instrument"] == "MCZ_LEFT"
    assert p["results"][0]["assets"][0]["url"].startswith("https://mars.nasa.gov/")
