import asyncio

import httpx
import pytest

from server.connectors import REGISTRY, ode
from server.connectors.base import Query

IIPT = {"ODEResults": {"Status": "Success", "IIPTSets": {"IIPTSet": [
    {"DataSetId": "MRO-M-HIRISE-3-RDR-V1.1", "IHID": "MRO", "IHName": "Mars Reconnaissance Orbiter", "IID": "HIRISE",
     "IName": "High-Resolution Imaging Science Experiment", "PT": "RDRV11", "PTName": "PDS3 RDR", "NumberProducts": "189476",
     "MinObservationTime": "2006-03-24T04:41:49.651", "MaxObservationTime": "2025-07-06T14:16:58.729",
     "ValidFootprints": "T", "ValidObservationTimes": "T", "ODEMetaDB": "Mars", "ValidTargets": {"ValidTarget": "MARS"}},
    {"DataSetId": "MRO-M-HIRISE-2-EDR-V1.0", "IHID": "MRO", "IHName": "Mars Reconnaissance Orbiter", "IID": "HIRISE",
     "IName": "High-Resolution Imaging Science Experiment", "PT": "EDR", "PTName": "PDS3 EDR", "NumberProducts": "2526363",
     "ValidFootprints": "F", "ValidObservationTimes": "F", "ODEMetaDB": "Mars", "ValidTargets": {"ValidTarget": ["MARS", "PHOBOS"]}},
    {"DataSetId": "HRSC-PHOBOS", "IHID": "MEX", "IHName": "Mars Express", "IID": "HRSC", "IName": "HRSC", "PT": "PHOBOS",
     "PTName": "Phobos maps", "ValidFootprints": "F", "ValidObservationTimes": "F", "ODEMetaDB": "Mars",
     "ValidTargets": {"ValidTarget": "PHOBOS"}}]}}}

PRODUCT = {"pdsid": "PSP_007925_1990_COLOR", "Comment": "Channel into Jezero Crater delta", "UTC_start_time": "2008-04-05T00:01:48.000",
           "Footprint_C0_geometry": "POLYGON ((77.5533 18.6682, 77.5328 18.6659, 77.4836 19.0448, 77.5042 19.0471, 77.5533 18.6682))",
           "Westernmost_longitude": "77.4836", "Easternmost_longitude": "77.5533", "Minimum_latitude": "18.6659",
           "Maximum_latitude": "19.0471", "ProductURL": "https://ode.rsl.wustl.edu/mars/x", "Map_scale": "0.25",
           "Product_files": {"Product_file": [
               {"FileName": "X.JP2", "Type": "Product", "KBytes": "686027", "URL": "https://h/X.JP2"},
               {"FileName": "X.LBL", "Type": "Product", "KBytes": "8", "URL": "https://h/X.LBL"},
               {"FileName": "X.BROWSE.JPG", "Type": "Browse", "KBytes": "1019", "Description": "BROWSE", "URL": "https://h/b.jpg"},
               {"FileName": "X.THUMB.JPG", "Type": "Browse", "KBytes": "292", "Description": "THUMBNAIL", "URL": "https://h/t.jpg"},
               {"FileName": "DSMAP.CAT", "Type": "Referenced", "KBytes": "7", "URL": "https://h/cat"}]}}


def fake_client(calls):
    def handler(req):
        p = dict(req.url.params)
        calls.append(p)
        if p["query"] == "iipt":
            return httpx.Response(200, json=IIPT)
        if p["results"] == "c":
            return httpx.Response(200, json={"ODEResults": {"Status": "Success", "Count": "202"}})
        return httpx.Response(200, json={"ODEResults": {"Status": "Success", "Count": "1", "Products": {"Product": PRODUCT}}})  # single product = bare dict
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(q, calls=None):
    async def go():
        async with fake_client([] if calls is None else calls) as c:
            return await ode.search("ode", q, c)
    ode._iipt_cache.clear()
    return asyncio.run(go())


def test_registered():
    assert REGISTRY["ode"][1]["id"] == "ode"


def test_footprint_wkt():
    assert ode._footprint({"Footprint_C0_geometry": "MULTIPOLYGON EMPTY", "Footprint_geometry": "POLYGON ((181 1, 182 1, 182 2, 181 1))"}) == {
        "type": "Polygon", "coordinates": [[[-179, 1], [-178, 1], [-178, 2], [-179, 1]]]}
    gc = ode._footprint({"Footprint_C0_geometry": "GEOMETRYCOLLECTION (POLYGON ((180 1, 179 2, 180 2, 180 1)), POLYGON ((-180 1, -179 2, -180 2, -180 1)))"})
    assert gc["type"] == "MultiPolygon" and len(gc["coordinates"]) == 2
    assert ode._footprint({"Footprint_C0_geometry": "MULTIPOLYGON EMPTY"}) is None


def test_bbox_and_longitude_conversion():
    assert ode._bbox({"Westernmost_longitude": "179.83", "Easternmost_longitude": "181", "Minimum_latitude": "-3", "Maximum_latitude": "2"}) == [179.83, -3.0, -179.0, 2.0]
    assert ode._bbox({"Westernmost_longitude_text": "0", "Easternmost_longitude_text": "360", "Minimum_latitude_text": "-90", "Maximum_latitude_text": "90"}) == [-180, -90, 180, 90]
    assert ode._lon360_box((77.2, 18.2, 77.8, 18.7)) == {"minlat": 18.2, "maxlat": 18.7, "westernlon": 77.2, "easternlon": 77.8}
    assert ode._lon360_box((179, 0, -179, 1))["westernlon"] == 179 and ode._lon360_box((179, 0, -179, 1))["easternlon"] == 181
    assert ode._lon360_box((-10, 0, 0, 1))["easternlon"] == 360
    assert ode._lon360_box((-180, -90, 180, 90))["easternlon"] == 360


def test_collections_filter_and_target():
    r = run(Query(level="collection", target="mars", text="hirise"))
    assert [x["collection"] for x in r["results"]] == ["mars|MRO|HIRISE|EDR", "mars|MRO|HIRISE|RDRV11"] and r["total"] == 2
    assert [x["collection"] for x in run(Query(level="collection", target="phobos"))["results"]] == ["phobos|MRO|HIRISE|EDR", "phobos|MEX|HRSC|PHOBOS"]
    with pytest.raises(ValueError):
        run(Query(level="collection", target="titan"))


def test_products_mapping_and_request():
    calls = []
    r = run(Query(level="product", collection="mars|MRO|HIRISE|RDRV11", bbox=(77.2, 18.2, 77.8, 18.7), text="PSP_0079", start="2008-01-01", limit=5, offset=0), calls)
    prod = calls[-1]
    assert prod["offset"] == "1" and prod["westernlon"] == "77.2" and prod["pdsid"] == "*PSP_0079*" and prod["minobtime"] == "2008-01-01" and "order" not in prod
    assert r["total"] == 202 and r["next_offset"] == 1
    p = r["results"][0]
    assert p["start"] == "2008-04-05T00:01:48.000Z" and p["thumbnail"] == "https://h/t.jpg" and p["browse"] == "https://h/b.jpg"
    assert [(a["name"], a["role"], a["openable"]) for a in p["assets"]] == [
        ("X.JP2", "data", True), ("X.LBL", "label", True), ("X.BROWSE.JPG", "browse", True), ("X.THUMB.JPG", "thumbnail", False)]
    assert p["footprint"]["type"] == "Polygon" and p["bbox"] == [77.4836, 18.6659, 77.5533, 19.0471]


def test_unsupported_queries():
    with pytest.raises(ValueError):
        run(Query(level="product"))
    with pytest.raises(ValueError, match="footprints"):
        run(Query(level="product", collection="mars|MRO|HIRISE|EDR", bbox=(0, 0, 1, 1)))


@pytest.mark.network
def test_live_hirise_jezero():
    async def go():
        async with httpx.AsyncClient(timeout=30) as c:
            return await ode.search("ode", Query(level="product", collection="mars|MRO|HIRISE|RDRV11", bbox=(77.2, 18.2, 77.8, 18.7), limit=5), c)
    r = asyncio.run(go())
    assert r["total"] > 20 and r["results"]
    urls = [a["url"].lower() for p in r["results"] for a in p["assets"]]
    assert any(u.endswith((".jp2", ".img")) for u in urls)
    assert all(p["footprint"] for p in r["results"])


@pytest.mark.network
def test_live_moon_lroc_collections():
    async def go():
        async with httpx.AsyncClient(timeout=30) as c:
            return await ode.search("ode", Query(level="collection", target="moon", text="lroc", limit=50), c)
    r = asyncio.run(go())
    assert any(x["collection"] == "moon|LRO|LROC|EDRNAC4" for x in r["results"])
