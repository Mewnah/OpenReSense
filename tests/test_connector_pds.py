import asyncio
import json

import httpx
import pytest

from server.config import CATALOG_DIR
from server.connectors import REGISTRY, pds
from server.connectors.base import Query

CTX = "urn:nasa:pds:context:"
COLLECTION = {"id": "urn:nasa:pds:orex.ocams:data_raw::13.0", "type": "Product_Collection", "title": "OSIRIS-REx OCAMS Raw Data Collection",
              "start_date_time": "2016-09-08T00:00:00Z", "stop_date_time": "null",
              "investigations": [{"id": CTX + "investigation:mission.orex"}],
              "observing_system_components": [{"id": CTX + "instrument_host:spacecraft.orex"}, {"id": CTX + "instrument:ocams.orex"}],
              "targets": [{"id": CTX + "target:asteroid.101955_bennu"}],
              "metadata": {"label_url": "https://x/collection.xml", "update_date_time": "2018-05-21T00:00:00Z"}, "properties": {}}
PRODUCT = {**COLLECTION, "id": "urn:nasa:pds:orex.ocams:data_raw:20190531t051649s609_map_l0pan.fits::1.0", "type": "Product_Observational",
           "title": "OCAMS L0 image", "start_date_time": "2019-05-31T05:16:49.609Z", "stop_date_time": "2019-05-31T05:16:49.609Z",
           "metadata": {"label_url": "https://x/a.xml"},
           "properties": {"ops:Data_File_Info.ops:file_ref": ["https://x/a.fits"], "ops:Data_File_Info.ops:file_name": ["a.fits"],
                          "ops:Data_File_Info.ops:file_size": ["4443840"], "ops:Data_File_Info.ops:mime_type": ["application/fits"],
                          "ops:Label_File_Info.ops:file_ref": ["https://x/a.xml"], "ops:Label_File_Info.ops:file_size": ["1000"]}}


def run(q, rows, calls=None, hits=None):
    def handler(req):
        if calls is not None:
            calls.append(dict(req.url.params))
        return httpx.Response(200, json={"summary": {"hits": hits or len(rows)}, "data": rows})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await pds.search("pds", q, c)
    return asyncio.run(go())


def test_target_table_matches_vocab():
    vocab = json.loads((CATALOG_DIR / "vocab.json").read_text("utf-8"))["targets"]
    assert set(pds.TARGETS) <= set(vocab) and set(REGISTRY["pds"][1]["targets"]) == set(pds.TARGETS)
    assert pds.TARGETS["titan"] == ["satellite.saturn.titan"] and pds.TARGETS["moon"] == ["satellite.earth.moon"]


def test_collection_mapping_and_query():
    calls = []
    r = run(Query(level="collection", target="bennu", limit=5), [COLLECTION], calls)
    q = calls[0]["q"]
    assert 'ref_lid_target eq "urn:nasa:pds:context:target:asteroid.101955_bennu"' in q and 'collection_type eq "Data"' in q
    x = r["results"][0]
    assert (x["target"], x["mission"], x["instrument"], x["start"], x["end"]) == ("bennu", "OREX", "OCAMS OREX", "2016-09-08T00:00:00Z", None)
    assert x["collection"] == COLLECTION["id"] and r["total"] == 1 and r["next_offset"] is None


def test_collection_text_is_filtered_locally_on_token_starts():
    rows = [COLLECTION, {**COLLECTION, "id": "urn:nasa:pds:other:data::1.0", "title": "Something else", "investigations": [], "observing_system_components": []}]
    assert [x["title"] for x in run(Query(level="collection", target="bennu", text="ocams"), rows)["results"]] == [COLLECTION["title"]]
    assert run(Query(level="collection", target="bennu", instrument="cams"), rows)["total"] == 0  # must start a token
    with pytest.raises(ValueError):
        run(Query(level="collection", text="ocams"), rows)


def test_wildcard_target_and_product_query():
    calls = []
    r = run(Query(level="product", collection="urn:nasa:pds:orex.ocams:data_raw::13.0", target="rings", text="201905", start="2019-05-01", limit=1),
            [PRODUCT], calls, hits=794)
    q = calls[0]["q"]
    assert 'ref_lid_target like "urn:nasa:pds:context:target:ring.*"' in q
    assert 'lid like "urn:nasa:pds:orex.ocams:data_raw:201905*"' in q and 'start_date_time ge "2019-05-01T00:00:00Z"' in q
    assert calls[0]["limit"] == "1" and "start" not in calls[0]
    p = r["results"][0]
    assert p["target"] == "bennu" or p["target"] == "rings"
    assert [(a["name"], a["role"], a["openable"]) for a in p["assets"]] == [("a.fits", "data", True), ("a.xml", "label", True)]
    assert p["assets"][0]["size"] == 4443840 and r["total"] == 794 and r["next_offset"] == 1


def test_collection_without_parent_links_falls_back_to_the_lid_prefix():
    calls = []

    def handler(req):
        calls.append(req.url.params["q"])
        hits = 0 if "parent_collection_identifier" in calls[-1] else 1
        return httpx.Response(200, json={"summary": {"hits": hits}, "data": [PRODUCT] * hits})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await pds.search("pds", Query(level="product", collection="urn:nasa:pds:lro-l-lroc-2-edr:lrolrc_0001_data::1.0"), c)
    r = asyncio.run(go())
    assert len(calls) == 2 and 'lid like "urn:nasa:pds:lro-l-lroc-2-edr:lrolrc_0001_data:*"' in calls[1] and r["total"] == 1


def test_errors():
    with pytest.raises(ValueError):
        run(Query(level="product"), [])
    with pytest.raises(ValueError):
        run(Query(level="product", target="bennu", text="x"), [])
    with pytest.raises(ValueError):
        run(Query(level="collection", target="bennu", limit=200, offset=900), [])


@pytest.mark.network
def test_live_bennu_collections_and_products():
    async def go():
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            cols = await pds.search("pds", Query(level="collection", target="bennu", text="ocams"), c)
            prods = await pds.search("pds", Query(level="product", collection=cols["results"][0]["collection"], limit=3), c)
            insight = await pds.search("pds", Query(level="collection", target="mars", text="insight", limit=5), c)
            return cols, prods, insight
    cols, prods, insight = asyncio.run(go())
    assert cols["total"] > 0 and prods["results"] and insight["total"] > 0
    assert all(any(a["role"] == "label" and a["openable"] for a in p["assets"]) for p in prods["results"])


def test_target_filter_is_dropped_when_only_the_collection_declares_it():
    calls = []

    def handler(req):
        calls.append(req.url.params["q"])
        hits = 0 if "ref_lid_target" in calls[-1] else 1
        return httpx.Response(200, json={"summary": {"hits": hits}, "data": [PRODUCT] * hits})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await pds.search("pds", Query(level="product", collection="urn:jaxa:darts:hyb2_onc:data_iof::1.0", target="moon"), c)
    r = asyncio.run(go())
    assert "ref_lid_target" not in calls[-1] and r["total"] == 1 and "moon" in r["notes"][0]
