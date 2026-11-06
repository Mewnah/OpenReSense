import asyncio

import httpx
import pytest

from server.connectors import cmr
from server.connectors.base import Query

COLLECTION = {"meta": {"concept-id": "C1-TEST", "provider-id": "TEST"}, "umm": {
    "ShortName": "SN", "Version": "2", "EntryTitle": "Title", "Abstract": "Abs", "DOI": {"DOI": "10.1/x"},
    "ProcessingLevel": {"Id": "3"}, "CloudHosted": True,
    "Platforms": [{"ShortName": "Terra", "Instruments": [{"ShortName": "MODIS"}]}],
    "TemporalExtents": [{"RangeDateTimes": [{"BeginningDateTime": "2000-01-01T00:00:00Z"}], "EndsAtPresentFlag": True}],
    "SpatialExtent": {"HorizontalSpatialDomain": {"Geometry": {"BoundingRectangles": [
        {"WestBoundingCoordinate": -180, "SouthBoundingCoordinate": -90, "EastBoundingCoordinate": 180,
         "NorthBoundingCoordinate": -60}]}}},
    "RelatedUrls": [{"Type": "GET RELATED VISUALIZATION", "URL": "https://x/pic.png"}]}}
R = "http://esipfed.org/ns/fedsearch/1.1/"
GRANULE = {"id": "G1-TEST", "title": "t", "producer_granule_id": "granule.v1", "time_start": "2020-01-01T00:00:00Z",
           "collection_concept_id": "C1-TEST", "dataset_id": "DS", "cloud_cover": "10",
           "polygons": [["10 179 10 -179 12 -179 12 179 10 179"]],
           "links": [{"rel": R + "data#", "href": "https://x/a.tif"}, {"rel": R + "browse#", "href": "https://x/b.jpg"},
                     {"rel": R + "s3#", "href": "s3://bucket/a.tif"}, {"rel": R + "data#", "href": "s3://bucket/b.tif"},
                     {"rel": R + "metadata#", "href": "https://x/a.xml"},
                     # collection-level links CMR appends to every granule: must not show up as assets
                     {"rel": R + "data#", "href": "https://search.example/search/granules?p=C1"},
                     {"rel": R + "documentation#", "href": "https://other/guide.pdf"},
                     {"rel": R + "metadata#", "href": "https://doi.org/10.5067/X"}]}


def run(handler, q):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await cmr.search("cmr", q, c)
    return asyncio.run(go())


def test_collection_mapping_and_params():
    seen = {}

    def handler(req):
        seen.update(req.url.params)
        return httpx.Response(200, json={"items": [COLLECTION]}, headers={"CMR-Hits": "5"})

    p = run(handler, Query(level="collection", text="sea ice", region="antarctica", limit=1))
    r = p["results"][0]
    assert seen["keyword"] == "sea* ice*" and seen["bounding_box"] == "-180,-90,180,-60" and seen["has_granules"] == "true"
    assert (p["total"], p["next_offset"]) == (5, 1)
    assert r["collection"] == "C1-TEST" and r["landing"] == "https://doi.org/10.1/x" and r["end"] is None
    assert r["bbox"] == [-180, -90, 180, -60] and r["thumbnail"] == "https://x/pic.png"
    assert r["extra"]["instruments"] == ["MODIS"] and r["extra"]["cloud_hosted"] is True


def test_granule_mapping_antimeridian_and_assets():
    seen = {}

    def handler(req):
        seen.update(req.url.params)
        return httpx.Response(200, json={"feed": {"entry": [GRANULE]}}, headers={"CMR-Hits": "1"})

    p = run(handler, Query(collection="C1-TEST", bbox=(170, -20, -170, -10), text="v1"))
    r = p["results"][0]
    assert seen["bounding_box"] == "170,-20,-170,-10" and seen["readable_granule_name[]"] == "*v1*"
    assert r["bbox"] == [179, 10, -179, 12]  # W>E: crosses the antimeridian
    assert [(a["role"], a["name"]) for a in r["assets"]] == [("data", "a.tif"), ("browse", "b.jpg"), ("metadata", "a.xml")]
    assert r["assets"][0]["openable"] and r["thumbnail"] == "https://x/b.jpg" and p["next_offset"] is None


def test_errors_and_non_earth():
    with pytest.raises(ValueError):
        run(lambda req: httpx.Response(200), Query(level="product"))
    assert run(lambda req: httpx.Response(500), Query(target="moon"))["results"] == []


@pytest.mark.network
def test_live_collections_and_granules():
    async def go():
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            cols = await cmr.search("cmr", Query(level="collection", text="antarctic", limit=5), c)
            assert cols["total"] > 50 and cols["results"]
            gran = await cmr.search("cmr", Query(collection="C2021957295-LPCLOUD", bbox=(-1, 50, 1, 52), limit=3), c)
            assert gran["total"] > 0 and gran["results"][0]["footprint"]
    asyncio.run(go())
