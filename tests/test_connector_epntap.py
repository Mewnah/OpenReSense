import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

from server.connectors import epntap
from server.connectors.base import Query

VOTABLE = b"""<?xml version="1.0"?>
<VOTABLE version="1.4" xmlns="http://www.ivoa.net/xml/VOTable/v1.3"><RESOURCE type="results"><TABLE>
<FIELD name="granule_uid" datatype="char" arraysize="*"/><FIELD name="c1min" datatype="double"/>
<FIELD name="c1max" datatype="double"/><FIELD name="c2min" datatype="double"/><FIELD name="c2max" datatype="double"/>
<FIELD name="spatial_frame_type" datatype="char" arraysize="*"/><FIELD name="time_min" datatype="double"/>
<FIELD name="access_url" datatype="char" arraysize="*"/><FIELD name="access_format" datatype="char" arraysize="*"/>
<FIELD name="access_estsize" datatype="int"/><FIELD name="thumbnail_url" datatype="char" arraysize="*"/>
<FIELD name="target_name" datatype="char" arraysize="*"/>
<DATA><TABLEDATA>
<TR><TD>g1</TD><TD>170</TD><TD>190</TD><TD>-10</TD><TD>10</TD><TD>body</TD><TD>2451544.5</TD>
<TD>http://x/a.fits</TD><TD>image/fits</TD><TD>100</TD><TD></TD><TD>Titan</TD></TR>
</TABLEDATA></DATA></TABLE></RESOURCE></VOTABLE>"""
SVC = {"tap": "http://t/tap", "table": "s.epn_core", "title": "T", "description": None}


def test_rows_and_product_mapping():
    (row,) = epntap._rows(VOTABLE)
    assert row["thumbnail_url"] is None and row["c1min"] == 170
    p = epntap._product(row, SVC, "http://t/tap|s.epn_core")
    assert p["start"] == "2000-01-01T00:00:00Z" and p["target"] == "titan"
    assert p["assets"][0]["size"] == 102400 and p["assets"][0]["openable"]
    assert p["footprint"]["type"] == "MultiPolygon" and p["bbox"] == [170, -10, -170, 10]  # 190 -> -170: antimeridian


def test_non_body_frame_has_no_footprint():
    row = {"spatial_frame_type": "celestial", "c1min": 1, "c1max": 2, "c2min": 0, "c2max": 1}
    assert epntap._bbox_footprint(row) == (None, None)


def test_conditions():
    q = Query(target="titan", bbox=(170, -10, -170, 10), start="2000-01-01T00:00:00Z", text="o'x")
    c = " AND ".join(epntap._conditions(q))
    assert "'Titan'" in c and "time_max >= 2451544.5" in c and "'%o''x%'" in c
    assert "c1max >= 170" in c and "c1min <= -170" in c


def test_non_image_table_pages_consistently(monkeypatch):
    async def fake_services(client):
        return [SVC]
    monkeypatch.setattr(epntap, "services", fake_services)
    queries = []

    def handler(req):
        adql = parse_qs(req.content.decode())["QUERY"][0]
        queries.append(adql)
        empty = b"<?xml version='1.0'?><VOTABLE version='1.4' xmlns='http://www.ivoa.net/xml/VOTable/v1.3'><RESOURCE type='results'><TABLE><FIELD name='granule_uid' datatype='char' arraysize='*'/><DATA><TABLEDATA></TABLEDATA></DATA></TABLE></RESOURCE></VOTABLE>"
        return httpx.Response(200, content=empty if "dataproduct_type" in adql else VOTABLE)

    async def go(offset):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await epntap.search("epntap", Query(level="product", collection="http://t/tap|s.epn_core", limit=1, offset=offset), c)
    first, second = asyncio.run(go(0)), asyncio.run(go(1))
    assert len(first["results"]) == 1 and first["notes"] and second["results"] == []  # offset 1: all-type fallback, not an image page end
    assert all("ORDER BY granule_uid" in q for q in queries)


def test_collection_requires_target():
    async def run():
        async with httpx.AsyncClient() as c:
            await epntap.search("epntap", Query(level="collection"), c)
    with pytest.raises(ValueError):
        asyncio.run(run())


@pytest.mark.network
def test_live_collections_and_products():
    async def run():
        async with httpx.AsyncClient(follow_redirects=True) as c:
            out = {}
            for t in ("titan", "mars"):
                out[t] = await epntap.search("epntap", Query(level="collection", target=t, limit=100), c)
            coll = next(r for r in out["mars"]["results"] if r["id"].endswith("hrsc3nd.epn_core"))
            prod = await epntap.search("epntap", Query(target="mars", collection=coll["collection"], limit=3), c)
            return out, prod
    out, prod = asyncio.run(run())
    assert out["titan"]["total"] >= 3 and out["mars"]["total"] >= 3
    assert prod["results"] and prod["results"][0]["assets"][0]["url"].startswith("http")
