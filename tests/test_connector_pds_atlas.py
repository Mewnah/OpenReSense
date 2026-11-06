import asyncio

import httpx
import pytest

from server.connectors import REGISTRY, pds_atlas as atlas
from server.connectors.base import Query

DOC = {"uuid": "u1", "PRODUCT_ID": "ESP_031871_1985_RED", "TARGET": ["MARS", "Mars"], "ATLAS_MISSION_NAME": "Mars Reconnaissance Orbiter",
       "ATLAS_INSTRUMENT_NAME": ["HIRISE"], "ATLAS_PRODUCT_TYPE": "RDR", "START_TIME": "2013-02-01T00:00:00Z",
       "ATLAS_DATA_URL": "https://h/ESP_031871_1985_RED.JP2", "ATLAS_LABEL_URL": "https://h/ESP_031871_1985_RED.LBL",
       "ATLAS_BROWSE_URL": "https://h/b.jpg", "ATLAS_THUMBNAIL_URL": "https://h/t.jpg", "FILE_NAME_SPECIFICATION": "RDR/ESP/ESP_031871_1985_RED.JP2",
       "MINIMUM_LONGITUDE": 77.7671, "MAXIMUM_LONGITUDE": 77.8881, "MINIMUM_LATITUDE": 18.3426, "MAXIMUM_LATITUDE": 18.5896,
       "UPPER_LEFT_LATITUDE": 18.5, "UPPER_LEFT_LONGITUDE": 77.8, "UPPER_RIGHT_LATITUDE": 18.5, "UPPER_RIGHT_LONGITUDE": 77.9,
       "LOWER_RIGHT_LATITUDE": 18.3, "LOWER_RIGHT_LONGITUDE": 77.9, "LOWER_LEFT_LATITUDE": 18.3, "LOWER_LEFT_LONGITUDE": 77.8}
PIVOT = {"facet_counts": {"facet_pivot": {"ATLAS_MISSION_NAME,ATLAS_INSTRUMENT_NAME": [
    {"value": "cassini", "count": 5, "pivot": [{"value": "iss", "count": 3}, {"value": "vims", "count": 2}]},
    {"value": "voyager", "count": 1, "pivot": [{"value": "vg-iss", "count": 1}]}]}}}


def run(q, payload, calls=None):
    def handler(req):
        if calls is not None:
            calls.append(req.url.params.multi_items())
        return httpx.Response(200, json=payload)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await atlas.search("pds-atlas", q, c)
    return asyncio.run(go())


def test_registered():
    assert "pds-atlas" in REGISTRY


def test_collections_from_pivot():
    r = run(Query(level="collection", target="titan", text="iss"), PIVOT)
    assert [x["collection"] for x in r["results"]] == ["titan|cassini|iss", "titan|voyager|vg-iss"]
    assert r["results"][0]["title"] == "Cassini ISS" and r["total"] == 2


def test_product_mapping_and_filters():
    calls = []
    r = run(Query(level="product", collection="mars|mars reconnaissance orbiter|hirise", bbox=(77.2, 18.2, 77.8, 18.7), text="esp_03", end="2014-01-01", limit=1),
            {"response": {"numFound": 204, "docs": [DOC]}}, calls)
    fq = [v for k, v in calls[0] if k == "fq"]
    assert 'TARGET:"mars"' in fq and 'ATLAS_MISSION_NAME:"mars reconnaissance orbiter"' in fq and 'ATLAS_INSTRUMENT_NAME:"hirise"' in fq
    assert "PRODUCT_ID:esp_03*" in fq and "START_TIME:[* TO 2014-01-01T23:59:59Z]" in fq
    p = r["results"][0]
    assert p["target"] == "mars" and p["bbox"] == [77.7671, 18.3426, 77.8881, 18.5896] and p["footprint"]["type"] == "Polygon"
    assert [(a["name"], a["role"], a["openable"]) for a in p["assets"]] == [("ESP_031871_1985_RED.JP2", "data", True), ("ESP_031871_1985_RED.LBL", "label", True)]
    assert r["total"] == 204 and r["next_offset"] == 1


def test_longitude_conventions_and_nulls():
    assert atlas._lon_overlap(77.2, 77.8).count("MINIMUM_LONGITUDE") == 2  # +-180 and 0..360 spellings
    assert "MINIMUM_LONGITUDE:[* TO 181]" in atlas._lon_overlap(179, -179)
    d = {**DOC, "MINIMUM_LONGITUDE": 350.0, "MAXIMUM_LONGITUDE": 351.0, "UPPER_LEFT_LATITUDE": -1e32}
    fp, bbox = atlas._geometry(d)
    assert fp is None and bbox[0] == -10.0 and bbox[2] == -9.0
    assert atlas._geometry({"MINIMUM_LONGITUDE": 999.9}) == (None, None)


def test_requires_a_valid_filter():
    with pytest.raises(ValueError):
        run(Query(level="product"), {})
    with pytest.raises(ValueError):
        run(Query(level="product", target="ceres"), {})


@pytest.mark.network
def test_live_mars_hirise_jezero():
    async def go():
        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as c:
            return await atlas.search("pds-atlas", Query(level="product", target="mars", instrument="hirise", bbox=(77.2, 18.2, 77.8, 18.7), limit=3), c)
    r = asyncio.run(go())
    assert r["total"] > 20 and all(p["thumbnail"] and p["assets"] for p in r["results"])


def test_stale_archive_urls_are_repaired():
    ctx = ("https://pds-imaging.jpl.nasa.gov/pds/prod?q=OFSN+%3D+data/mro/mars_reconnaissance_orbiter/ctx//mrox_0457/data/P17.IMG"
           "+AND+RT+%3D+")
    d = {**DOC, "PRODUCT_ID": None, "identifier": ["P17"], "FILE_NAME_SPECIFICATION": None,
         "ATLAS_DATA_URL": ctx + "RAW", "ATLAS_LABEL_URL": ctx + "PDS_LABEL",
         "ATLAS_BROWSE_URL": "https://pdsimg.jpl.nasa.gov//data/mro/mars_reconnaissance_orbiter/ctx/mrox_0457/extras/browse/P17.IMG.jpeg",
         "ATLAS_THUMBNAIL_URL": "brwsnotavail.jpg"}
    p = atlas._product(d, "mars")
    assert p["title"] == "P17"
    assert [a["url"] for a in p["assets"]][0] == "https://pds-imaging.jpl.nasa.gov/data/mro/ctx/mrox_0457/data/P17.IMG"
    assert "mars_reconnaissance_orbiter" not in p["assets"][1]["url"] and p["assets"][1]["role"] == "label"
    assert p["browse"] == "https://pds-imaging.jpl.nasa.gov/data/mro/ctx/mrox_0457/extras/browse/P17.IMG.jpeg" and p["thumbnail"] is None
    themis = {**DOC, "ATLAS_DATA_URL": "https://starbrite.jpl.nasa.gov/prod?object=PDS.ASU.Product&keywordQuery=OFSN+=+/ODTSDP_v1/data/odtir1_0005/i056xxrdr//I05690011RDR.QUB+AND+RT+=+RAW",
              "ATLAS_LABEL_URL": "https://starbrite.jpl.nasa.gov/prod?object=PDS.ASU.Product&keywordQuery=OFSN+=+/x.LBL+AND+RT+=+PDS_LABEL"}
    p = atlas._product(themis, "mars")
    assert [a["url"] for a in p["assets"]] == ["https://static.mars.asu.edu/pds/ODTSDP_v1/data/odtir1_0005/i056xxrdr/I05690011RDR.QUB"]
