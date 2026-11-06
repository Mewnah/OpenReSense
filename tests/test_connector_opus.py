import asyncio

import httpx
import pytest

from server.connectors import REGISTRY, opus
from server.connectors.base import Query

H = "https://opus.pds-rings.seti.org/holdings/"
TARGETS = {"mults": {"Titan": 197325, "Saturn": 412035, "Saturn Rings": 317299, "Jupiter Rings": 622, "Albiorix": 3426, "2014 MU69": 10}}
FILES = {"data": {"co-iss-n1": {
    "coiss_raw": [H + "volumes/N1_1.IMG", H + "volumes/N1_1.LBL", H + "volumes/label/prefix2.fmt"],
    "coiss_calib": [H + "calibrated/N1_1_CALIB.IMG"],
    "coiss_thumb": [H + "t.jpeg_small"],
    "coiss_documentation": [H + "doc.pdf"], "rms_index": [H + "i.lbl"], "ring_geometry": [H + "g.tab"],
    "browse_thumb": [H + "p_thumb.jpg"], "browse_small": [H + "p_small.jpg"], "browse_medium": [H + "p_med.jpg"],
    "browse_full": [H + "p_full.jpg"]}}}
DATA = {"available": 45170, "page": [["co-iss-n1", "Titan, Saturn Rings", "Cassini ISS", "2004-02-06T02:20:06.232", "2004-02-06T02:20:06.492", "S_IMG_CO_ISS_1"]]}


def run(q, calls=None):
    def handler(req):
        if calls is not None:
            calls.append(req.url)
        path = req.url.path
        if path.endswith("mults/target.json"):
            return httpx.Response(200, json=TARGETS)
        if path.endswith("mults/instrument.json"):
            return httpx.Response(200, json={"mults": {"Cassini ISS": 45170, "Voyager ISS": 926, "Hubble ACS": 0}})
        return httpx.Response(200, json=DATA if path.endswith("data.json") else FILES)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await opus.search("opus", q, c)
    opus._cache.clear()
    return asyncio.run(go())


def test_registered_targets_are_vocab():
    import json
    from server.config import CATALOG_DIR
    vocab = json.loads((CATALOG_DIR / "vocab.json").read_text("utf-8"))["targets"]
    assert set(REGISTRY["opus"][1]["targets"]) <= set(vocab)


def test_target_names_map_to_vocab():
    assert [opus.target_id(n) for n in ("Titan", "Saturn Rings", "Chariklo Ring", "2014 MU69", "2011 HK103", "Albiorix", "Arrokoth")] == [
        "titan", "rings", "rings", "arrokoth", "kbos", None, "arrokoth"]
    assert opus.mission_of("New Horizons LORRI") == "New Horizons"


def test_collections():
    r = run(Query(level="collection", target="titan"))
    assert [(x["collection"], x["extra"]["observations"]) for x in r["results"]] == [("titan|Cassini ISS", 45170), ("titan|Voyager ISS", 926)]
    assert run(Query(level="collection", target="titan", text="voyager"))["total"] == 1
    assert run(Query(level="collection", target="ceres"))["notes"]


def test_products_mapping_and_params():
    calls = []
    r = run(Query(level="product", collection="rings|Cassini ISS", start="2004-01-01", text="n1", limit=1, offset=0), calls)
    data_call = next(c for c in calls if c.path.endswith("data.json"))
    p = dict(data_call.params)
    assert p["target"] == "Saturn Rings,Jupiter Rings" and p["instrument"] == "Cassini ISS" and p["startobs"] == "1" and p["time1"] == "2004-01-01" and p["opusid"] == "n1"
    assert r["total"] == 45170 and r["next_offset"] == 1
    x = r["results"][0]
    assert x["target"] == "rings" and x["mission"] == "Cassini" and x["start"].endswith("Z")
    assert x["thumbnail"] == H + "p_thumb.jpg" and x["browse"] == H + "p_med.jpg"
    names = {(a["name"], a["role"]) for a in x["assets"]}
    assert {("N1_1.IMG", "data"), ("N1_1.LBL", "label"), ("N1_1_CALIB.IMG", "data"), ("prefix2.fmt", "metadata")} <= names
    assert not any(n in ("doc.pdf", "i.lbl", "g.tab") for n, _ in names)
    assert x["landing"].endswith("detail=co-iss-n1")


def test_offset_past_the_end_is_an_empty_page():
    def handler(req):
        path, p = req.url.path, req.url.params
        if path.endswith("mults/target.json"):
            return httpx.Response(200, json=TARGETS)
        if path.endswith("data.json") and p["startobs"] == "1":
            return httpx.Response(200, json=DATA)
        return httpx.Response(500)  # OPUS errors when startobs is beyond the last observation

    async def go(offset):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await opus.search("opus", Query(level="product", target="titan", offset=offset), c)
    opus._cache.clear()
    r = asyncio.run(go(999999))
    assert r["results"] == [] and r["total"] == 45170 and r["next_offset"] is None
    with pytest.raises(httpx.HTTPStatusError):  # a 500 inside the result range is a real upstream error
        asyncio.run(go(10))


@pytest.mark.network
def test_live_titan_cassini_iss():
    async def go():
        async with httpx.AsyncClient(timeout=30) as c:
            cols = await opus.search("opus", Query(level="collection", target="titan"), c)
            prods = await opus.search("opus", Query(level="product", collection="titan|Cassini ISS", limit=3), c)
            return cols, prods
    cols, prods = asyncio.run(go())
    assert "titan|Cassini ISS" in [x["collection"] for x in cols["results"]]
    assert prods["total"] > 10000
    for p in prods["results"]:
        assert p["thumbnail"] and p["browse"]
        assert any(a["name"].endswith(".IMG") and a["openable"] for a in p["assets"])
        assert any(a["role"] == "label" and a["name"].endswith(".LBL") for a in p["assets"])
