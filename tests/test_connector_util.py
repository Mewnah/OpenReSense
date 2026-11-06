import asyncio

import httpx
import pytest

from server.connectors import REGISTRY, ode
from server.connectors._util import check_times, json_of
from server.connectors.base import Query


def test_unparsable_dates_are_rejected_before_any_request():
    check_times(Query(start="2020-01-31", end="2020-02-01T12:00:00Z"))
    with pytest.raises(ValueError, match="start must be an ISO 8601"):
        check_times(Query(start="notadate"))
    for cid, (mod, _) in REGISTRY.items():  # every connector validates up front (no client is touched)
        with pytest.raises(ValueError, match="end must be"):
            asyncio.run(mod.search(cid, Query(end="31/01/2020"), None))


def test_html_body_is_an_upstream_error_not_a_bad_query():
    r = httpx.Response(200, text="<html>maintenance</html>", request=httpx.Request("GET", "https://h.example/x"))
    with pytest.raises(httpx.HTTPError):  # -> HTTP 502 in the router, not 400
        json_of(r)
    assert json_of(httpx.Response(200, json={"a": 1})) == {"a": 1}


def test_ode_collections_with_several_dataset_versions_are_merged():
    def s(n):
        return {"DataSetId": f"D{n}", "IHID": "MRO", "IHName": "MRO", "IID": "CRISM", "IName": "CRISM", "PT": "TRDR", "PTName": "TRDR",
                "NumberProducts": str(n), "ValidFootprints": "T", "ValidObservationTimes": "T", "ValidTargets": {"ValidTarget": "MARS"}}
    payload = {"ODEResults": {"Status": "Success", "IIPTSets": {"IIPTSet": [s(5), s(7)]}}}

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload))) as c:
            return await ode.search("ode", Query(level="collection", target="mars"), c)
    ode._iipt_cache.clear()
    r = asyncio.run(go())
    assert r["total"] == 1 and r["results"][0]["extra"]["products"] == 12
    ode._iipt_cache.clear()
