import asyncio

import httpx
import pytest

from server.connectors import asf
from server.connectors.base import Query

FEATURE = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[0, 50], [2, 50], [2, 52], [0, 52], [0, 50]]]},
           "properties": {"sceneName": "S1X", "fileName": "S1X.zip", "url": "https://d/S1X.zip", "bytes": 100,
                          "browse": ["https://d/B/S1X.jpg"], "platform": "Sentinel-1D", "sensor": "C-SAR",
                          "startTime": "2026-01-01T00:00:00Z", "stopTime": "2026-01-01T00:00:25Z",
                          "processingLevel": "GRD_HD", "pathNumber": 30, "polarization": "VV+VH",
                          "additionalUrls": ["https://d/BROWSE/x.gif"], "flightDirection": None}}


def run(handler, q):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await asf.search("asf", q, c)
    return asyncio.run(go())


def test_collection_list_filters_by_text():
    p = run(lambda r: httpx.Response(500), Query(level="collection", text="optical"))
    assert [r["title"] for r in p["results"]] == ["ALOS AVNIR-2"]
    assert len(run(lambda r: httpx.Response(500), Query(level="collection"))["results"]) == len(asf.DATASETS)


def test_product_mapping_and_params():
    calls = []

    def handler(req):
        calls.append(dict(req.url.params))
        if req.url.params["output"] == "count":
            return httpx.Response(200, text="40\n")
        return httpx.Response(200, json={"features": [FEATURE] * 3})

    p = run(handler, Query(collection="SENTINEL-1", bbox=(179, -20, -179, -10), limit=2, offset=1))
    assert "MULTIPOLYGON" in calls[0]["intersectsWith"] and calls[0]["maxResults"] == "3"
    assert calls[0]["processingLevel"] == "GRD_HD"
    r = p["results"][0]
    assert len(p["results"]) == 2 and p["total"] == 40 and p["next_offset"] == 3
    assert r["bbox"] == [0, 50, 2, 52] and r["thumbnail"] == "https://d/B/S1X.jpg" and r["assets"][0]["size"] == 100
    assert [a["role"] for a in r["assets"]] == ["data", "browse", "browse"]


def test_text_is_an_exact_scene_list_and_lifts_the_default_level_filter():
    calls = []

    def handler(req):
        calls.append(dict(req.url.params))
        return httpx.Response(200, text="1") if req.url.params["output"] == "count" else httpx.Response(200, json={"features": [FEATURE]})

    p = run(handler, Query(collection="SENTINEL-1", text="S1X  S1Y"))
    assert calls[0]["granule_list"] == "S1X,S1Y" and "processingLevel" not in calls[0] and p["total"] == 1


def test_bad_collection_and_deep_paging():
    with pytest.raises(ValueError):
        run(lambda r: httpx.Response(200), Query(collection="NOPE"))
    with pytest.raises(ValueError):
        run(lambda r: httpx.Response(200), Query(collection="UAVSAR", offset=2000))


@pytest.mark.network
def test_live_sentinel1_small_bbox():
    async def go():
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            p = await asf.search("asf", Query(collection="SENTINEL-1", bbox=(-1, 50, 1, 52), limit=3), c)
            assert p["total"] > 0 and p["results"][0]["footprint"] and p["results"][0]["thumbnail"]
    asyncio.run(go())
