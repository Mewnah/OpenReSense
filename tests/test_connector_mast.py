import asyncio

import httpx
import pytest

from server.connectors import mast
from server.connectors.base import Query

OBS = {"obsid": 7, "obs_id": "idh002o3q", "target_name": "JUPITER", "obs_collection": "HST", "instrument_name": "WFC3/UVIS",
       "filters": "F502N", "t_min": 58156.5, "t_max": 58156.6, "jpegURL": "mast:HST/product/idh002o3q_flt.jpg",
       "dataURL": "mast:HST/product/idh002o3q_drz.fits", "obs_title": "t", "calib_level": 3}
PRODUCTS = [
    {"productType": "SCIENCE", "productFilename": "a_raw.fits", "dataURI": "mast:HST/product/a_raw.fits", "size": 5, "calib_level": 1},
    {"productType": "SCIENCE", "productFilename": "a_drz.fits", "dataURI": "mast:HST/product/a_drz.fits", "size": 9, "calib_level": 3},
    {"productType": "PREVIEW", "productFilename": "a.jpg", "dataURI": "mast:HST/product/a.jpg", "size": 1, "calib_level": 3},
]


def test_product_mapping_orders_science_by_calibration():
    p = mast._product(OBS, PRODUCTS)
    assert [a["name"] for a in p["assets"][:2]] == ["a_drz.fits", "a_raw.fits"]
    assert p["assets"][0]["url"].endswith("uri=mast:HST/product/a_drz.fits") and p["assets"][0]["openable"]
    assert p["assets"][-1]["role"] == "browse" and p["thumbnail"] == p["browse"]
    assert p["start"] == "2018-02-07T12:00:00Z" and p["target"] == "jupiter"


def test_product_falls_back_to_dataurl():
    assert mast._product(OBS, [])["assets"][0]["name"] == "idh002o3q_drz.fits"


def test_plain_urls_bypass_the_download_service():
    assert mast._download("https://hla.stsci.edu/cgi-bin/preview.cgi?dataset=x") == "https://hla.stsci.edu/cgi-bin/preview.cgi?dataset=x"
    assert mast._download("mast:HST/product/a.fits") == mast.DOWNLOAD + "mast:HST/product/a.fits"
    f = {x["paramName"]: x["values"] for x in mast._filters(Query(target="67p"), False)}
    assert "67P/CHURYUMOV-GERASIMENKO" in f["target_name"]


def test_filters():
    f = {x["paramName"]: x["values"] for x in mast._filters(
        Query(target="titan", collection="HST|WFPC2/PC", start="2000-01-01T00:00:00Z"), True)}
    assert "TITAN" in f["target_name"] and f["obs_collection"] == ["HST"] and f["instrument_name"] == ["WFPC2/PC"]
    assert f["t_max"][0]["min"] == 51544.0
    with pytest.raises(ValueError):
        mast._filters(Query(), False)


@pytest.mark.network
def test_live_jupiter_images_with_previews_and_fits():
    async def run():
        async with httpx.AsyncClient(follow_redirects=True) as c:
            return await mast.search("mast", Query(target="jupiter", collection="HST|WFC3/UVIS", limit=5), c)
    p = asyncio.run(run())
    assert p["results"] and all(r["browse"] for r in p["results"])
    assert any(a["name"].endswith(".fits") for r in p["results"] for a in r["assets"])
