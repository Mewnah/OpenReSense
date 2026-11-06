import asyncio
import json

import httpx
import pytest

from server.connectors import stac
from server.connectors.base import Query

COLLECTIONS = {"collections": [
    {"id": "mars-dtm", "title": "Mars DTMs", "description": "Digital terrain", "keywords": ["mars"],
     "summaries": {"ssys:targets": ["Mars"]},
     "extent": {"spatial": {"bbox": [[-10669445.0, -1778200, 10669445.0, 1778410]]},
                "temporal": {"interval": [["2000-01-01T00:00:00Z", None]]}}},
    {"id": "moon-tc", "title": "Moon TC", "description": "Kaguya", "summaries": {"ssys:targets": ["moon"]},
     "extent": {"spatial": {"bbox": [[0, -90, 360, 90]]}, "temporal": {"interval": [[None, None]]}}}], "links": []}
ITEM = {"id": "it1", "collection": "mars-dtm", "bbox": [1, 2, 3, 4],
        "geometry": {"type": "Polygon", "coordinates": [[[190, 0], [200, 0], [200, 5], [190, 0]]]},
        "properties": {"datetime": "2020-01-01T00:00:00Z", "ssys:targets": ["Mars"], "eo:cloud_cover": 5},
        "assets": {"thumbnail": {"href": "https://x/t.jpg", "roles": ["thumbnail"], "type": "image/jpeg"},
                   "image": {"href": "https://x/i.tif", "roles": ["data", "overview"], "type": "image/tiff", "file:size": 7},
                   "band": {"href": "s3://bkt/dir/b.tif", "type": "image/tiff"},
                   "cdse": {"href": "s3://eodata/x.jp2", "roles": ["data"], "alternate": {"https": {"href": "https://dl/x"}}},
                   "meta": {"href": "https://x/m.xml", "roles": ["metadata"], "type": "application/xml"}}}


@pytest.fixture(autouse=True)
def clean():
    stac._collections_cache.clear()
    stac._next_links.clear()


def run(handler, conn_id, q):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await stac.search(conn_id, q, c)
    return asyncio.run(go())


def test_collections_planetary_targets_bbox_and_filters():
    def h(req):
        return httpx.Response(200, json=COLLECTIONS)

    by_id = {r["id"]: r for r in run(h, "stac:usgs-astro", Query(level="collection"))["results"]}
    assert by_id["mars-dtm"]["target"] == "mars" and by_id["mars-dtm"]["bbox"] is None  # metre extent is not degrees
    assert by_id["moon-tc"]["bbox"] == [-180, -90, 180, 90]  # 0..360 global
    assert [r["id"] for r in run(h, "stac:usgs-astro", Query(level="collection", target="moon"))["results"]] == ["moon-tc"]
    assert [r["id"] for r in run(h, "stac:usgs-astro", Query(level="collection", text="terrain"))["results"]] == ["mars-dtm"]
    assert run(h, "stac:cdse", Query(level="collection", target="mars"))["results"] == []


def test_deafrica_collection_fallback_to_root_catalog():
    def h(req):
        if req.url.path.endswith("/collections"):
            return httpx.Response(200, text="<html>Server error</html>")
        return httpx.Response(200, json={"links": [{"rel": "child", "href": "https://e/stac/collections/s2_l2a"},
                                                   {"rel": "child", "href": "https://e/stac/catalogs/arrivals"}]})

    assert [r["id"] for r in run(h, "stac:deafrica", Query(level="collection"))["results"]] == ["s2_l2a"]


def test_item_mapping():
    def h(req):
        return httpx.Response(200, json={"features": [ITEM], "numberMatched": 1, "links": []})

    p = run(h, "stac:usgs-astro", Query(collection="mars-dtm", limit=5))
    r = p["results"][0]
    assert r["bbox"] == [1, 2, 3, 4] and r["target"] == "mars" and r["footprint"]["coordinates"][0][0] == [-170, 0]
    assert r["thumbnail"] == "https://x/t.jpg"
    by_name = {a["name"]: a for a in r["assets"]}
    assert by_name["i.tif"]["role"] == "data" and by_name["i.tif"]["openable"] and by_name["i.tif"]["size"] == 7
    assert by_name["m.xml"]["role"] == "metadata"
    urls = {a["url"] for a in r["assets"]}
    assert "https://bkt.s3.amazonaws.com/dir/b.tif" in urls and "https://dl/x" in urls
    assert r["extra"] == {"eo:cloud_cover": 5} and p["total"] == 1 and p["next_offset"] is None


def test_paging_follows_next_links_and_antimeridian_intersects():
    bodies = []

    def h(req):
        body = json.loads(req.content)
        bodies.append(body)
        n = int(body.get("token", 0))
        feats = [{**ITEM, "id": f"i{n + k}"} for k in range(2)]
        links = [{"rel": "next", "method": "POST", "href": str(req.url), "body": {"token": n + 2}, "merge": True}] \
            if n < 4 else []
        return httpx.Response(200, json={"features": feats, "links": links})

    q = Query(collection="c", bbox=(170, -10, -170, 10), limit=2)
    p0 = run(h, "stac:earth-search", q)
    assert [r["id"] for r in p0["results"]] == ["i0", "i1"] and p0["next_offset"] == 2
    assert bodies[0]["intersects"]["type"] == "MultiPolygon" and "bbox" not in bodies[0]
    q.offset = 2  # next_offset resumes from the cached next link: exactly one more request
    bodies.clear()
    p1 = run(h, "stac:earth-search", q)
    assert [r["id"] for r in p1["results"]] == ["i2", "i3"] and len(bodies) == 1 and bodies[0]["token"] == 2
    q.offset = 4
    p2 = run(h, "stac:earth-search", q)
    assert [r["id"] for r in p2["results"]] == ["i4", "i5"] and p2["next_offset"] is None


@pytest.mark.network
@pytest.mark.parametrize("conn_id,coll,box", [
    ("stac:earth-search", "sentinel-2-l2a", (-1, 50, 1, 52)),
    ("stac:planetary-computer", "landsat-c2-l2", (-1, 50, 1, 52)),
    ("stac:cdse", "sentinel-2-l2a", (-1, 50, 1, 52)),
    ("stac:usgs-astro", "mo_themis_controlled_mosaics", None),
    ("stac:dea", "ga_ls8c_ard_3", (140, -35, 141, -34)),
    ("stac:deafrica", "s2_l2a", (30, -2, 31, -1)),
    ("stac:usgs-landsat", "landsat-c2l2-sr", (-1, 50, 1, 52))])
def test_live_collections_and_items(conn_id, coll, box):
    async def go():
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            cols = await stac.search(conn_id, Query(level="collection", text=coll, limit=200), c)
            assert coll in {r["id"] for r in cols["results"]}
            items = await stac.search(conn_id, Query(collection=coll, bbox=box, limit=2), c)
            assert items["results"] and items["results"][0]["assets"]
            if conn_id == "stac:usgs-astro":
                every = await stac.search(conn_id, Query(level="collection", limit=200), c)
                assert {"mars", "moon"} <= {r["target"] for r in every["results"]}
    asyncio.run(go())


def test_unwrapped_longitudes_are_folded_and_split():
    ring = [[-180.58, -17.7], [-178.8, -17.8], [-179.2, -19.8], [-180.99, -19.5], [-180.58, -17.7]]
    g = stac._fold_lons({"type": "Polygon", "coordinates": [ring]})
    assert g["type"] == "MultiPolygon" and len(g["coordinates"]) == 2
    assert all(-180 <= x <= 180 for poly in g["coordinates"] for r in poly for x, _ in r)
    assert stac._valid_bbox([-180.99, -19.8, -178.85, -17.7]) == [179.01, -19.8, -178.85, -17.7]
    assert stac._valid_bbox([-180, 85, 180, -56]) == [-180, -56, 180, 85]  # swapped S/N in some PC extents
    inside = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    assert stac._fold_lons(inside) is inside
