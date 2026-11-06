import json
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import catalog


def E(id, name, **kw):
    return {"id": id, "name": name, "provider": "P", "agency": ["NASA"], "kind": "archive", "url": "https://x",
            "targets": ["moon"], "description": "", **kw}


SOURCES = [
    E("lroc", "LROC browse", tags=["lro"], description="Lunar imagery", formats=["PDS3"], access="open"),
    E("hirise", "HiRISE", targets=["mars"], agency=["NASA", "JPL"], instruments=["HiRISE"],
      description="Mars orbiter camera, also the moon is mentioned", formats=["JP2"], access="open"),
    E("polar", "Polar SAR", targets=["earth"], regions=["antarctica"], agency=["ESA"], kind="portal",
      access="free-registration", connector="stac"),
    E("glob", "Global optical", targets=["earth"], regions=["global"], agency=["ESA"], kind="portal", access="open"),
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "sources").mkdir()
    (tmp_path / "user").mkdir()
    (tmp_path / "vocab.json").write_text(json.dumps({
        "targets": {"moon": "Moon", "mars": "Mars", "earth": "Earth"},
        "regions": {"antarctica": {"name": "Antarctica", "aliases": ["south pole"]}, "global": {"name": "Global"}}}))
    (tmp_path / "bodies.json").write_text(json.dumps({"bodies": [{"id": "moon", "name": "Moon", "aliases": ["Luna"]}]}))
    (tmp_path / "sources" / "a.json").write_text(json.dumps({"domain": "a", "sources": SOURCES}))
    (tmp_path / "sources" / "bad.json").write_text("{not json")
    monkeypatch.setattr(catalog.config, "CATALOG_DIR", tmp_path)
    catalog._state.update(sig=None)
    app = FastAPI()
    app.include_router(catalog.router, prefix="/api")
    c = TestClient(app)
    c.dir = tmp_path
    return c


def ids(r):
    return [e["id"] for e in r["results"]]


def get(c, **p):
    r = c.get("/api/catalog", params=p)
    assert r.status_code == 200
    return r.json()


def test_all_and_entry(client):
    r = get(client)
    assert r["total"] == 4
    assert client.get("/api/catalog/hirise").json()["name"] == "HiRISE"
    assert client.get("/api/catalog/nope").status_code == 404


def test_scoring_and_alias(client):
    assert ids(get(client, q="moon"))[0] == "lroc"        # target match outranks a description mention
    assert "hirise" in ids(get(client, q="moon"))
    assert ids(get(client, q="luna")) == ["lroc"]          # body alias
    assert ids(get(client, q="south pole")) == ["polar"]   # region alias, phrase as two tokens
    assert ids(get(client, q='"south pole"')) == ["polar"]
    assert ids(get(client, q="moon mars")) == ["hirise"]   # all tokens must match
    assert get(client, q="zzz")["total"] == 0
    assert ids(get(client, q="antarc")) == ["polar"]       # word prefix


def test_filters_and_facets(client):
    r = get(client, target="earth", agency="ESA", access="open")
    assert ids(r) == ["glob"]
    # self-filter exclusion: access facet ignores the access filter, other facets honour it
    assert r["facets"]["access"] == {"open": 1, "free-registration": 1}
    assert r["facets"]["targets"] == {"earth": 1}
    assert get(client, target="moon,mars")["total"] == 2
    assert get(client, region="antarctica")["facets"]["regions"] == {"antarctica": 1, "global": 1}
    assert get(client, connector="stac")["total"] == 1


def test_pagination(client):
    r = get(client, limit=2, offset=3)
    assert r["total"] == 4 and len(r["results"]) == 1
    assert get(client, limit=0)["results"] == []
    assert client.get("/api/catalog", params={"limit": "x"}).status_code == 400


def test_reload_on_mtime_and_user_dir(client):
    assert get(client)["total"] == 4
    f = client.dir / "user" / "mine.json"
    f.write_text(json.dumps({"sources": [E("mine", "My thing"), E("lroc", "dup")]}))
    assert get(client)["total"] == 5  # dup id ignored
    f.write_text(json.dumps({"sources": []}))
    os.utime(f, ns=(time.time_ns() + 10**9,) * 2)
    assert get(client)["total"] == 4


def test_relevance_whole_word_and_focus(client):
    f = client.dir / "user" / "rel.json"
    f.write_text(json.dumps({"sources": [
        E("terrain", "Terrain maps", targets=["mars"]),
        E("terra", "Terra imagery", targets=["mars"]),
        E("moon-tool", "Moon toolbox", kind="tool"),
        E("moon-data", "Moon data", kind="dataset"),
        E("moon-multi", "Moon multi", kind="dataset", targets=["moon", "mars", "earth"]),
        E("lunar-only", "Lunar archive", kind="dataset"),
    ]}))
    assert ids(get(client, q="terra"))[:2] == ["terra", "terrain"]            # whole word before prefix
    r = ids(get(client, target="moon"))
    assert r.index("moon-data") < r.index("moon-tool")                          # data sources before tools
    assert max(map(r.index, ["moon-data", "moon-tool", "moon-multi"])) < r.index("lroc")  # named after the target first


def test_name_starting_with_term_ranks_first(client):
    (client.dir / "user" / "lead.json").write_text(json.dumps({"sources": [
        E("a", "Mission webmaps (Zorgcam, CTX)"), E("b", "Zorgcam archive")]}))
    assert ids(get(client, q="zorgcam")) == ["b", "a"]
