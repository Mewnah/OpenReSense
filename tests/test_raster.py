"""Raster backend: tiles, warp, render pipeline, stats, probe, body detection, fs, open/optimise jobs."""
import json
from pathlib import Path
import math
import time

import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin

from server import config
from server.app import app
from server.raster import render, sources

MOON = "+proj=eqc +R=1737400 +units=m +no_defs"
R = 1737400.0


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    cache = tmp_path_factory.mktemp("cache")
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "CACHE_DIR", cache)
    sources.reload()
    yield cache
    mp.undo()
    sources.reload()


@pytest.fixture(scope="module")
def client(env):
    with TestClient(app) as c:
        yield c


def wait(client, ds_id, timeout=60, optimizing=False):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = client.get(f"/api/datasets/{ds_id}").json()
        if s["status"] in ("ready", "error") and not (optimizing and s["optimizing"]):
            return s
        time.sleep(0.1)
    raise TimeoutError(s)


def open_path(client, path, **kw):
    r = client.post("/api/datasets/open", json={"path": str(path), **kw})
    assert r.status_code == 200, r.text
    s = wait(client, r.json()["id"])
    assert s["status"] == "ready", s["message"]
    return s


def write_tif(path, arr, crs=None, transform=None, nodata=None, **kw):
    arr = arr if arr.ndim == 3 else arr[None]
    with rasterio.open(path, "w", driver="GTiff", width=arr.shape[2], height=arr.shape[1], count=arr.shape[0],
                       dtype=arr.dtype.name, crs=crs, transform=transform, nodata=nodata, **kw) as o:
        o.write(arr)
    return path


@pytest.fixture(scope="module")
def u16(tmp_path_factory):
    h, w = 400, 600
    a = ((np.arange(h * w).reshape(h, w) * 7) % 60000 + 1).astype("uint16")
    a[:50, :50] = 0
    p = write_tif(tmp_path_factory.mktemp("d") / "u16.tif", a, MOON, from_origin(-300000, 200000, 1000, 1000), nodata=0)
    with rasterio.open(p, "r+") as o:
        o.scales, o.offsets = [0.5], [10.0]
    return p, a


@pytest.fixture(scope="module")
def odd(tmp_path_factory):
    a = np.random.default_rng(1).random((300, 515), dtype=np.float32) * 100
    return write_tif(tmp_path_factory.mktemp("d") / "odd.tif", a, "EPSG:4326", from_origin(0, 10, 0.01, 0.01)), a


# ---------------------------------------------------------------- info

def test_info_fields(client, u16):
    p, a = u16
    s = open_path(client, p)
    i = s["info"]
    assert (i["width"], i["height"], i["count"], i["dtype"], i["nodata"]) == (600, 400, 1, "uint16", [0])
    assert i["scales"] == [0.5] and i["offsets"] == [10.0]
    assert i["tiles"] == {"tile_size": 256, "max_zoom": 2, "overzoom": 4}
    assert i["georef"] == "transform" and i["bounds"] == [-300000, -200000, 300000, 200000]
    assert i["crs"]["body"] == "moon" and i["crs"]["a"] == R and not i["crs"]["is_geographic"]
    w, s_, e, n = i["lonlat_bounds"]
    assert w == pytest.approx(math.degrees(-300000 / R)) and n == pytest.approx(math.degrees(200000 / R))
    assert i["resolution"] == [1000, 1000]
    assert open_path(client, p)["id"] == s["id"]  # same source, same id


def test_body_detection():
    from pyproj import CRS
    d = sources.detect_body
    assert d(CRS.from_proj4("+proj=longlat +R=3396190 +no_defs")) == "mars"
    assert d(CRS.from_epsg(4326)) == "earth"
    assert d(CRS.from_proj4("+proj=longlat +R=1000 +no_defs")) is None
    titan = ('GEOGCS["GCS_Titan_2000",DATUM["D_Titan_2000",SPHEROID["Titan_2000_IAU_IAG",2575000.0,0.0]],'
             'PRIMEM["Reference_Meridian",0.0],UNIT["Degree",0.0174532925199433]]')
    assert d(CRS.from_wkt(titan)) == "titan"


# ---------------------------------------------------------------- tiles

def test_tile_exact_edge_alpha(client, u16):
    p, a = u16
    ds_id = open_path(client, p)["id"]
    r = client.get(f"/api/datasets/{ds_id}/tile/2/2/1?bands=1")
    assert r.headers["X-Bands"] == "2"
    t = np.frombuffer(r.content, "<f4").reshape(256, 256, 2)
    np.testing.assert_array_equal(t[:144, :88, 0], a[256:400, 512:600])
    assert t[:144, :88, 1].min() == 1 and t[144:, :, 1].max() == 0 and t[:, 88:, 1].max() == 0
    assert t[144:, :, 0].max() == 0
    t = np.frombuffer(client.get(f"/api/datasets/{ds_id}/tile/2/0/0?bands=1").content, "<f4").reshape(256, 256, 2)
    assert t[:50, :50, 1].max() == 0 and t[:50, :50, 0].max() == 0 and t[50, 50, 1] == 1  # nodata block
    assert client.get(f"/api/datasets/{ds_id}/tile/2/9/9").content == bytes(256 * 256 * 4 * 2)


def test_downsample_alignment(odd):
    p, a = odd
    t = render.tile_f32(str(p), 1, 0, 0, [1], "average")
    assert t.shape == (256, 256, 2)
    np.testing.assert_allclose(t[:150, :256, 0], a[:, :512].reshape(150, 2, 256, 2).mean((1, 3)), rtol=1e-5)
    e = render.tile_f32(str(p), 1, 1, 0, [1], "average")  # cols 512..514: one full output px + one 1-px remainder
    np.testing.assert_allclose(e[0, 0, 0], a[0:2, 512:514].mean(), rtol=1e-5)
    np.testing.assert_allclose(e[0, 1, 0], a[0:2, 514].mean(), rtol=1e-5)
    assert e[0, 2, 1] == 0 and e[149, 0, 1] == 1 and e[150, 0, 1] == 0


def test_overzoom_nearest(odd):
    p, a = odd
    mz = render.max_zoom(515, 300)
    t = render.tile_f32(str(p), mz + 1, 2, 0, [1], magnify="nearest")  # scale 0.5: x=2 covers cols 256..384
    np.testing.assert_array_equal(t[:, :, 0], np.repeat(np.repeat(a[0:128, 256:384], 2, 0), 2, 1))
    t = render.tile_f32(str(p), mz + 2, 0, 0, [1], magnify="nearest")
    np.testing.assert_array_equal(t[:, :, 0], np.repeat(np.repeat(a[0:64, 0:64], 4, 0), 4, 1))
    with pytest.raises(ValueError):
        render.tile_f32(str(p), mz + 5, 0, 0, [1])


def test_rgb_tile_bands(client, tmp_path):
    a = np.random.default_rng(2).integers(0, 255, (3, 100, 120), dtype=np.uint8)
    p = write_tif(tmp_path / "rgb.tif", a, "EPSG:4326", from_origin(10, 50, 0.1, 0.1))
    s = open_path(client, p)
    assert s["info"]["colorinterp"][:3] == ["red", "green", "blue"]
    r = client.get(f"/api/datasets/{s['id']}/tile/0/0/0?bands=1,2,3")
    assert r.headers["X-Bands"] == "4"
    t = np.frombuffer(r.content, "<f4").reshape(256, 256, 4)
    np.testing.assert_array_equal(t[:100, :120, :3].transpose(2, 0, 1), a)
    assert client.get(f"/api/datasets/{s['id']}/tile/0/0/0?bands=4").status_code == 400
    assert client.get(f"/api/datasets/{s['id']}/tile/0/0/0?resampling=bogus&bands=1").status_code in (200, 400)


# ---------------------------------------------------------------- warp

def test_warp_lonlat_known_pixel(client, u16):
    p, a = u16
    ds_id = open_path(client, p)["id"]
    col, row = 200, 100
    x0, y0 = -300000 + col * 1000, 200000 - row * 1000
    d = lambda m: math.degrees(m / R)
    bbox = f"{d(x0)},{d(y0 - 4000)},{d(x0 + 4000)},{d(y0)}"
    r = client.get(f"/api/datasets/{ds_id}/warp?crs=lonlat&bbox={bbox}&width=4&height=4&bands=1")
    w = np.frombuffer(r.content, "<f4").reshape(4, 4, 2)
    np.testing.assert_array_equal(w[..., 0], a[row:row + 4, col:col + 4])
    assert w[..., 1].min() == 1
    # outside the data: transparent
    r = client.get(f"/api/datasets/{ds_id}/warp?crs=lonlat&bbox=100,-10,120,10&width=8&height=8&bands=1")
    assert np.frombuffer(r.content, "<f4").reshape(8, 8, 2)[..., 1].max() == 0


def test_warp_0_360_and_seam(tmp_path):
    lon = (np.arange(720) + 0.5) * 0.5
    a = np.tile(np.floor(lon).astype("uint16"), (360, 1))
    p = write_tif(tmp_path / "g360.tif", a, "EPSG:4326", from_origin(0, 90, 0.5, 0.5))
    w = render.warp_f32(str(p), "lonlat", (-90, -10, -80, 10), 20, 4, [1], magnify="nearest")
    assert set(np.unique(w[..., 0])) == set(range(270, 280)) and w[..., 1].min() == 1
    w = render.warp_f32(str(p), "lonlat", (-10, -10, 10, 10), 40, 2, [1], magnify="nearest")  # straddles 0/360
    assert w[0, 0, 0] == 350 and w[0, 39, 0] == 9 and w[..., 1].min() == 1
    w = render.warp_f32(str(p), "lonlat", (170, -10, 180, 10), 20, 2, [1], magnify="nearest")
    assert w[0, 0, 0] == 170 and w[0, 19, 0] == 179


def test_warp_0_360_projected(tmp_path):
    """LOLA/MOLA style maps: equirectangular with x running 0..2*pi*R; lonlat requests use -180..180."""
    lon = (np.arange(720) + 0.5) * 0.5
    a = np.tile(np.floor(lon).astype("uint16"), (360, 1))
    res = 2 * math.pi * R / 720
    p = write_tif(tmp_path / "e360.tif", a, MOON, from_origin(0, res * 180, res, res))
    w = render.warp_f32(str(p), "lonlat", (-90, -10, -80, 10), 20, 4, [1], magnify="nearest")
    assert set(np.unique(w[..., 0])) == set(range(270, 280)) and w[..., 1].min() == 1
    w = render.warp_f32(str(p), "lonlat", (-10, -10, 10, 10), 40, 2, [1], magnify="nearest")
    assert w[0, 0, 0] == 350 and w[0, 39, 0] == 9 and w[..., 1].min() == 1
    r = render.pixel(str(p), x=-85.2, y=0.1, crs="lonlat")
    assert r["col"] == 549 and r["values"] == [274.0] and r["lonlat"][0] == pytest.approx(-85.25)
    from server.raster.sources import describe
    assert describe(str(p))["lonlat_bounds"] == pytest.approx([-180, -90, 180, 90], abs=1e-6)


def test_warp_seam_on_tile_edge(tmp_path):
    """Titan ISS style: lon_0=180 and x -pi*R..pi*R, so the map wraps at 0 E. Tiles that end exactly on 0 used to
    read the whole map coarsely and leave transparent columns at that edge."""
    lon = (np.arange(720) + 0.5) * 0.5  # 0..360
    a = np.tile(np.floor(lon).astype("uint16") + 1, (360, 1))
    res = 2 * math.pi * R / 720
    p = write_tif(tmp_path / "c180.tif", a, MOON.replace("+proj=eqc", "+proj=eqc +lon_0=180"),
                  from_origin(-math.pi * R, res * 180, res, res))
    for bbox, first, last in (((-45, 45, 0, 90), 316, 360), ((0, 45, 45, 90), 1, 45), ((-45, -90, 0, -45), 316, 360)):
        w = render.warp_f32(str(p), "lonlat", bbox, 90, 90, [1], magnify="nearest")
        assert w[..., 1].min() == 1 and w[0, 0, 0] == first and w[0, -1, 0] == last, bbox


def test_warp_other_crs_and_png(client, u16, tmp_path):
    p, a = u16
    ds_id = open_path(client, p)["id"]
    r = client.get(f"/api/datasets/{ds_id}/warp?crs=native&bbox=-300000,-200000,300000,200000&width=60&height=40&bands=1&resampling=average")
    w = np.frombuffer(r.content, "<f4").reshape(40, 60, 2)
    np.testing.assert_allclose(w[10:, 10:, 0][0, 0], a[100:110, 100:110].mean(), rtol=1e-4)  # 10x10 average
    assert w[0, 0, 1] == 0  # nodata block stays transparent (mask nearest on blocks)
    r = client.get(f"/api/datasets/{ds_id}/warp?crs=native&bbox=-300000,-200000,300000,200000&width=60&height=40&bands=1&format=png&min=0&max=60000&colormap=gray")
    assert r.headers["content-type"] == "image/png"
    from PIL import Image
    import io
    im = np.array(Image.open(io.BytesIO(r.content)))
    assert im.shape == (40, 60, 4) and im[0, 0, 3] == 0 and im[20, 30, 3] == 255
    assert client.get(f"/api/datasets/{ds_id}/warp?crs=lonlat&bbox=1,1,0,0").status_code == 400
    assert client.get(f"/api/datasets/{ds_id}/warp?crs=EPSG:3857&bbox=-1e6,-1e6,1e6,1e6&bands=1").status_code == 400  # Moon vs Earth
    flat = write_tif(tmp_path / "flat.tif", np.zeros((10, 10), "uint8"))
    s = open_path(client, flat)
    assert s["info"]["georef"] == "none" and s["info"]["crs"] is None
    assert client.get(f"/api/datasets/{s['id']}/warp?crs=lonlat&bbox=0,0,1,1").status_code == 400
    assert client.get(f"/api/datasets/{s['id']}/tile/0/0/0?bands=1").status_code == 200


def test_warp_reuses_the_source_read(client, u16, monkeypatch):
    """Panning back, or restyling the globe (same area, other colours), does not read and warp the source again."""
    from server import datasets
    p, a = u16
    ds_id = open_path(client, p)["id"]
    datasets._forget_warps()
    calls, real = [], render.warp_f32
    monkeypatch.setattr(render, "warp_f32", lambda *a, **k: calls.append(1) or real(*a, **k))
    q = f"/api/datasets/{ds_id}/warp?crs=native&bbox=-300000,-200000,300000,200000&width=60&height=40&bands=1"
    first = client.get(q).content
    assert client.get(q).content == first
    pngs = [client.get(q + f"&format=png&min=0&max=60000&colormap={cm}").content for cm in ("gray", "viridis")]
    assert len(calls) == 1 and pngs[0] != pngs[1]
    datasets._forget_warps()  # as after a delete
    assert client.get(q).content == first and len(calls) == 2
    monkeypatch.setattr(datasets, "WARP_CACHE_BYTES", 60 * 40 * 2 * 4 * 4 - 1)  # this warp is now "huge": not kept
    datasets._forget_warps()
    client.get(q), client.get(q)
    assert len(calls) == 4 and not datasets._warps


# ---------------------------------------------------------------- section 6

def _px(v, **kw):
    p = dict(stretch="linear", min=[0.0], max=[10.0], invert=False, colormap="gray", gamma=1, brightness=0,
             contrast=0, exposure=0, saturation=0, nodata="transparent")
    p.update(kw)
    d = np.array(v, np.float32).reshape(-1, 1, 1)
    return render.render_rgba(d, np.ones(d.shape[1:], np.float32), p)[0, 0]


def test_render_steps():
    assert tuple(_px([5])) == (128, 128, 128, 255)
    assert _px([-5])[0] == 0 and _px([50])[0] == 255
    assert _px([5], min=[3.0], max=[3.0])[0] == 0  # max == min
    assert _px([2], invert=True)[0] == 204
    assert _px([5], stretch="log")[0] == round(255 * math.log(1 + 127.5) / math.log(256))
    q = list(range(0, 33))
    assert _px([8], stretch="equalize", quantiles=[q])[0] == 64 and _px([16], stretch="equalize", quantiles=[q])[0] == 128
    assert _px([2], contrast=0.5)[0] == 13  # .2 -> 1.5*.2-.25 = .05
    assert _px([3], exposure=1)[0] == 153
    assert _px([2.5], gamma=2)[0] == 128  # sqrt(.25)
    assert _px([5], brightness=0.1)[0] == 153
    assert tuple(_px([10, 0, 0], min=[0, 0, 0], max=[10, 10, 10], saturation=-1)[:3]) == (54, 54, 54)
    assert tuple(_px([10, 0, 0], min=[0] * 3, max=[10] * 3)[:3]) == (255, 0, 0)
    assert tuple(_px([10, 5, 0], min=[0] * 3, max=[10] * 3, saturation=1)[:3]) == (255, 110, 0)


def test_render_order_matches_reference():
    t, c, e, s, g, b = 0.37, 0.4, 0.3, 0.5, 1.8, -0.05
    rgb = np.array([t, t * 0.5, 1 - t])
    rgb = np.clip((c + 1) * rgb - c / 2, 0, 1)
    rgb = np.clip((e + 1) * rgb, 0, 1)
    S = s + 1
    L = np.array([0.2126, 0.7152, 0.0722])
    rgb = np.clip(((1 - S) * L)[:, None] * 0 + np.array([[(1 - S) * L[0] + S, (1 - S) * L[1], (1 - S) * L[2]],
                                                         [(1 - S) * L[0], (1 - S) * L[1] + S, (1 - S) * L[2]],
                                                         [(1 - S) * L[0], (1 - S) * L[1], (1 - S) * L[2] + S]]) @ rgb, 0, 1)
    rgb = np.clip(rgb ** (1 / g), 0, 1)
    expect = np.floor(np.clip(rgb + b, 0, 1) * 255 + 0.5)
    got = _px([t * 10, t * 5, (1 - t) * 10], min=[0] * 3, max=[10] * 3, contrast=c, exposure=e, saturation=s,
              gamma=g, brightness=b)[:3]
    assert np.abs(got.astype(int) - expect).max() <= 1


def test_render_colormap_and_nodata():
    cm = json.load(open(config.WEB_DIR / "colormaps.json"))["maps"]
    name = next(k for k in cm if k != "gray")
    stops = cm[name]
    assert list(_px([5], colormap=name)[:3]) == [round(v) for v in stops[16][1:]]  # t=.5 is stop 16
    mid = np.array(stops[16][1:]) / 2 + np.array(stops[17][1:]) / 2
    t = 0.5 + 1 / 64
    assert np.abs(_px([t * 10], colormap=name)[:3].astype(int) - np.floor(mid + 0.5)).max() <= 1
    d, a = np.array([[[5.0, 5.0]]], np.float32), np.array([[1.0, 0.0]], np.float32)
    p = dict(stretch="linear", min=[0.0], max=[10.0], invert=False, colormap="gray", gamma=1, brightness=0,
             contrast=0, exposure=0, saturation=0)
    assert list(render.render_rgba(d, a, {**p, "nodata": "transparent"})[0, 1]) == [0, 0, 0, 0]
    assert list(render.render_rgba(d, a, {**p, "nodata": "black"})[0, 1]) == [0, 0, 0, 255]


# ---------------------------------------------------------------- stats and probe

def test_stats(client, u16):
    p, a = u16
    ds_id = open_path(client, p)["id"]
    valid = a[a != 0].astype("float64")
    for exact in (0, 1):
        b = client.get(f"/api/datasets/{ds_id}/stats?exact={exact}").json()["bands"][0]
        assert (b["min"], b["max"], b["valid"], b["total"]) == (valid.min(), valid.max(), valid.size, a.size)
        assert b["mean"] == pytest.approx(valid.mean(), rel=1e-6) and b["std"] == pytest.approx(valid.std(), rel=1e-6)
        assert len(b["quantiles"]) == 33 and b["quantiles"] == sorted(b["quantiles"])
        assert sum(b["histogram"]["counts"]) == valid.size and len(b["histogram"]["counts"]) == 256
        assert b["percentiles"]["99.9"] == pytest.approx(np.percentile(valid, 99.9), rel=2e-3 if exact else 1e-6)
    assert client.get(f"/api/datasets/{ds_id}/stats?bands=2").status_code == 400


def test_pixel_probe(client, u16):
    p, a = u16
    ds_id = open_path(client, p)["id"]
    r = client.get(f"/api/datasets/{ds_id}/pixel?col=200&row=100").json()
    assert r["values"] == [float(a[100, 200])] and r["scaled"] == [a[100, 200] * 0.5 + 10] and r["nodata"] == [False]
    assert r["native"] == [-300000 + 200500, 200000 - 100500] and r["inside"]
    assert r["lonlat"][0] == pytest.approx(math.degrees(r["native"][0] / R))
    r2 = client.get(f"/api/datasets/{ds_id}/pixel", params={"x": r["lonlat"][0], "y": r["lonlat"][1], "crs": "lonlat"}).json()
    assert (r2["col"], r2["row"]) == (200, 100)
    r3 = client.get(f"/api/datasets/{ds_id}/pixel", params={"x": -299500, "y": 199500, "crs": "native"}).json()
    assert (r3["col"], r3["row"], r3["values"], r3["nodata"]) == (0, 0, [0.0], [True])
    out = client.get(f"/api/datasets/{ds_id}/pixel?col=-1&row=5").json()
    assert out["inside"] is False and out["values"] is None


def test_probe_0_360(tmp_path):
    a = np.tile(np.arange(720, dtype="uint16"), (360, 1))
    p = write_tif(tmp_path / "g360.tif", a, "EPSG:4326", from_origin(0, 90, 0.5, 0.5))
    r = render.pixel(str(p), x=-85.2, y=0.1, crs="lonlat")
    assert r["col"] == 549 and r["values"] == [549.0] and r["lonlat"][0] == pytest.approx(-85.25)


# ---------------------------------------------------------------- fs, lifecycle, upload

def test_fs(client, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / ".hidden").write_text("x")
    (tmp_path / "b.tif").write_bytes(b"0")
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "lab.xml").write_text("x")
    r = client.get("/api/fs", params={"path": str(tmp_path)}).json()
    assert [e["name"] for e in r["entries"]] == ["sub", "a.txt", "b.tif", "lab.xml"]
    assert [e["openable"] for e in r["entries"]] == [False, False, True, True]
    assert r["parent"] and r["entries"][2]["size"] == 1
    top = client.get("/api/fs").json()
    assert top["path"] == "" and top["drives"] and top["entries"][0]["name"] == "Home"
    assert client.get("/api/fs", params={"path": str(tmp_path / "nope")}).status_code == 404


def test_lifecycle_and_safety(client, u16, tmp_path, env):
    p, _ = u16
    ds_id = open_path(client, p)["id"]
    assert any(d["id"] == ds_id for d in client.get("/api/datasets").json())
    sources.reload()  # persisted registry survives a restart
    assert client.get(f"/api/datasets/{ds_id}").json()["status"] == "ready"
    assert client.delete(f"/api/datasets/{ds_id}?purge=1").status_code == 200
    assert p.exists()  # user files are never deleted
    assert client.get(f"/api/datasets/{ds_id}").status_code == 404
    assert client.get("/api/datasets/not-an-id").status_code == 404
    assert client.post("/api/datasets/open", json={"path": str(tmp_path / "missing.tif")}).status_code == 404
    assert client.post("/api/datasets/open", json={}).status_code == 400
    assert client.post("/api/datasets/open", json={"url": "file:///etc/passwd"}).status_code == 400
    bad = tmp_path / "bad.tif"
    bad.write_bytes(b"not a raster at all")
    s = client.post("/api/datasets/open", json={"path": str(bad)}).json()
    s = wait(client, s["id"])
    assert s["status"] == "error" and s["message"]
    assert client.get(f"/api/datasets/{s['id']}/tile/0/0/0").status_code == 409


def test_interrupted_jobs_become_errors(env):
    sources.REG["d_0000000000"] = {"id": "d_0000000000", "status": "downloading", "optimizing": False, "_created": 1}
    sources.save()
    sources.reload()
    assert sources.REG["d_0000000000"]["status"] == "error"
    sources.REG.pop("d_0000000000")


def test_upload_pair(client, tmp_path):
    from pathlib import Path
    d = Path(__file__).parent / "data" / "pds3"
    if not (d / "LDEM_4.LBL").exists():
        pytest.skip("run scripts/fetch_samples.py")
    files = [("files", (n, (d / n).read_bytes())) for n in ("LDEM_4.IMG", "LDEM_4.LBL")]
    s = client.post("/api/datasets/upload", files=files).json()
    s = wait(client, s["id"])
    assert s["status"] == "ready" and s["name"].lower() == "ldem_4.lbl" and s["info"]["crs"]["body"] == "moon"


# ---------------------------------------------------------------- optimisation (striped 30 MP)

def test_optimize_large_striped(client, tmp_path):
    h, w = 5000, 6000
    a = (np.arange(w, dtype="uint32")[None, :] + np.arange(h, dtype="uint32")[:, None]).astype("uint16")
    p = write_tif(tmp_path / "big.tif", a, "EPSG:4326", from_origin(0, 50, 0.001, 0.001), nodata=0)
    s = client.post("/api/datasets/open", json={"path": str(p)}).json()
    s = wait(client, s["id"], optimizing=False)
    assert s["optimizing"] is True and s["status"] == "ready"
    mz = s["info"]["tiles"]["max_zoom"]
    t = render.tile_f32(str(p), mz, 5, 6, [1])
    np.testing.assert_array_equal(t[:, :, 0], a[6 * 256:7 * 256, 5 * 256:6 * 256])  # served meanwhile
    s = wait(client, s["id"], timeout=180, optimizing=True)
    assert s["optimizing"] is False and "optimization failed" not in s["message"]
    i = s["info"]
    assert i["overviews"] and i["georef"] == "transform" and i["crs"]
    cog = sources.serve_path(sources.REG[s["id"]])
    assert cog.endswith("optimized.tif")
    with rasterio.open(cog) as ds:
        assert ds.profile["tiled"] and ds.nodata == 0
    t2 = render.tile_f32(cog, mz, 5, 6, [1])
    np.testing.assert_array_equal(t, t2)  # lossless
    t0 = time.perf_counter()
    for k in range(5):
        render.tile_f32(cog, mz - 2, k % 2, 1, [1])
    assert (time.perf_counter() - t0) / 5 < 0.5
    st = client.get(f"/api/datasets/{s['id']}/stats").json()
    assert max(st["sample_shape"]) <= 1024


# ---------------------------------------------------------------- export, crs name, lonlat overshoot

def test_crs_name_readable(client, u16):
    assert open_path(client, u16[0])["info"]["crs"]["name"] == "Equirectangular · Moon"  # PROJ-string CRS is unnamed


def test_export_raw_and_rendered(client, u16, env):
    p, a = u16
    ds_id = open_path(client, p)["id"]
    base = f"/api/datasets/{ds_id}/export"
    r = client.get(f"{base}?kind=raw&format=tif&window=100,50,300,150")
    assert r.status_code == 200 and 'u16_raw.tif"' in r.headers["content-disposition"]
    out = env / "raw.tif"
    out.write_bytes(r.content)
    with rasterio.open(out) as o:
        assert o.dtypes == ("uint16",) and o.nodata == 0 and o.scales == (0.5,) and o.offsets == (10.0,)
        np.testing.assert_array_equal(o.read(1), a[50:150, 100:300])
        with rasterio.open(p) as src:
            assert o.transform == src.transform @ rasterio.Affine.translation(100, 50) and o.crs == src.crs
    r = client.get(f"{base}?kind=raw&format=tif&scale=0.5&window=0,0,600,400")
    out.write_bytes(r.content)
    with rasterio.open(out) as o:
        assert (o.width, o.height) == (300, 200) and o.transform.a == 2000
    assert client.get(f"{base}?kind=raw&format=png").status_code == 400
    from PIL import Image
    import io
    for fmt, mode in (("png", "RGBA"), ("jpg", "RGB"), ("tif", "RGBA")):
        r = client.get(f"{base}?kind=rendered&format={fmt}&scale=0.25&min=0&max=60000")
        assert r.status_code == 200, r.text
        im = Image.open(io.BytesIO(r.content))
        assert im.mode == mode and im.size == (150, 100)
    r = client.get(f"{base}?kind=rendered&format=png&window=100,100,110,110&min=0&max=60000")
    im = np.array(Image.open(io.BytesIO(r.content)))
    assert im.shape == (10, 10, 4) and im[0, 0, 0] == round(255 * int(a[100, 100]) / 60000) and im[0, 0, 3] == 255
    assert client.get(f"{base}?window=0,0,601,400").status_code == 400
    assert client.get(f"{base}?scale=2").status_code == 400
    assert not list((env / "exports").glob("*"))  # temp files removed after sending


def test_export_413(client, tmp_path):
    huge = tmp_path / "huge.tif"  # sparse 30000 x 20000 = 600 MP
    with rasterio.open(huge, "w", driver="GTiff", width=30000, height=20000, count=1, dtype="uint8", sparse_ok=True,
                       tiled=True):
        pass
    ds_id = open_path(client, huge)["id"]
    r = client.get(f"/api/datasets/{ds_id}/export?kind=raw&format=tif")
    assert r.status_code == 413 and "scale" in r.json()["detail"]


def test_warp_lonlat_overshoot(client, tmp_path):
    a = np.arange(180 * 360, dtype="float32").reshape(180, 360)
    p = write_tif(tmp_path / "glob.tif", a, "EPSG:4326", from_origin(-180, 90, 1, 1))
    pm = write_tif(tmp_path / "globm.tif", a, MOON, from_origin(-R * math.pi, R * math.pi / 2, R * 2 * math.pi / 360, R * math.pi / 180))
    for src in (p, pm):
        ds_id = open_path(client, src)["id"]
        r = client.get(f"/api/datasets/{ds_id}/warp?crs=lonlat&bbox=-181,-91,181,91&width=364&height=184&bands=1")
        assert r.status_code == 200, r.text
        w = np.frombuffer(r.content, "<f4").reshape(184, 364, 2)
        assert w[0, 0, 1] == 0 and w[92, 182, 1] == 1  # outside transparent, inside opaque
        assert w[3:-3, 3:-3, 1].min() == 1  # whole interior covered


def test_purge_deletes_files_held_by_other_threads(client, u16):
    from concurrent.futures import ThreadPoolExecutor
    s = client.post("/api/datasets/upload", files=[("files", ("up.tif", u16[0].read_bytes()))]).json()
    ds_id = wait(client, s["id"])["id"]
    folder = sources.REG[ds_id]["_dir"]
    with ThreadPoolExecutor(8) as ex:  # each worker thread caches its own handle on the file
        assert all(r.status_code == 200 for r in ex.map(lambda _: client.get(f"/api/datasets/{ds_id}/tile/0/0/0?bands=1"), range(32)))
    assert client.delete(f"/api/datasets/{ds_id}?purge=1").status_code == 200
    assert not Path(folder).exists()  # Windows keeps open files undeletable


def test_world_warp_of_small_polar_raster_has_no_phantom_data(client, tmp_path):
    """GDAL's approximate transformer paints edge pixels over distant regions; only the real footprint may be opaque."""
    a = np.full((157, 313), 7, "float32")
    p = write_tif(tmp_path / "polar.tif", a, "EPSG:3031", rasterio.transform.from_origin(599904, -2399904, 320, 320))
    ds_id = open_path(client, p)["id"]
    r = client.get(f"/api/datasets/{ds_id}/warp?crs=lonlat&bbox=-180,-90,180,90&width=720&height=360&bands=1&resampling=nearest")
    w = np.frombuffer(r.content, "<f4").reshape(360, 720, 2)
    rows, cols = np.nonzero(w[..., 1] > 0)
    assert (90 - (rows + .5) * .5 < -60).all() and ((cols + .5) * .5 - 180 > 100).all()


# ---------------------------------------------------------------- hardening (security review)

def test_remote_names_cannot_escape_the_folder():
    for bad in ("https://h/a/x%5C..%5C..%5Cevil.txt", "https://h/a/..%2F..%2Fevil.txt", "https://h/a/..",
                r"..\..\x.bat", "C:/x.img", "../../x.txt"):
        n = sources._file_name(bad)
        assert "/" not in n and "\\" not in n and n not in ("", ".", ".."), (bad, n)
    assert sources._safe_name("../../F.IMG") == "F.IMG" and sources._safe_name(r"C:\a\b.IMG") == "b.IMG"
    assert sources._file_name("https://h/a/F.IMG") == "F.IMG"


def test_download_refuses_web_pages(tmp_path):
    import httpx
    page = httpx.MockTransport(lambda req: httpx.Response(
        200, headers={"content-type": "text/html"}, content=b"\n<!DOCTYPE html><title>Sign in</title>"))
    e = {"bytes_total": 0, "bytes_done": 0}
    with httpx.Client(transport=page) as c:
        with pytest.raises(httpx.DecodingError, match="web page"):
            sources._download(c, "https://h/F.IMG", tmp_path / "F.IMG", e, True)
        assert not sources._download(c, "https://h/F.LBL", tmp_path / "F.LBL", e, False)
    assert not (tmp_path / "F.IMG").exists()
    xml = httpx.MockTransport(lambda req: httpx.Response(  # PDS4 label served with the wrong type is still saved
        200, headers={"content-type": "text/html"}, content=b'<?xml version="1.0"?><Product_Observational>'
                                                            b'<Identification_Area><title>MRO CTX</title>'))
    with httpx.Client(transport=xml) as c:
        assert sources._download(c, "https://h/F.xml", tmp_path / "F.xml", e, True)


def test_nan_values_are_json_null(client, tmp_path):
    a = np.full((20, 30), np.nan, dtype="float32")
    a[5:, 5:] = 1.5
    s = open_path(client, write_tif(tmp_path / "nan.tif", a, "EPSG:4326", from_origin(0, 10, 0.1, 0.1), nodata=float("nan")))
    assert s["info"]["nodata"] == ["nan"]
    assert client.get("/api/datasets").status_code == 200
    r = client.get(f"/api/datasets/{s['id']}/pixel?col=0&row=0")
    assert r.status_code == 200 and r.json()["values"] == [None]


@pytest.mark.parametrize("ours", [True, False])
def test_second_start_on_a_busy_port(ours):
    """Starting the app again opens the running one; a port held by another program says so (no bind traceback)."""
    import http.server
    import subprocess
    import sys
    import threading
    page = b"<html><head><title>OpenReSense</title>" if ours else b"<html><head><title>Something else</title>"

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = subprocess.run([sys.executable, "-m", "server", "--port", str(srv.server_port), "--no-browser"],
                           cwd=Path(__file__).parent.parent, capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()
    assert r.returncode == (0 if ours else 1)
    assert ("already running" in r.stdout) if ours else ("used by another program" in r.stderr)


def test_cross_site_writes_refused(client):
    assert client.post("/api/datasets/open", json={"path": "x"}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/datasets/open", json={"path": "missing"}, headers={"Origin": "http://testserver"}).status_code == 404
    assert client.get("/api/datasets", headers={"Origin": "http://evil.example"}).status_code == 200
    assert client.get("/api/datasets", headers={"Host": "evil.example"}).status_code == 400


def test_export_413_counts_raw_bands(client, tmp_path):
    cube = tmp_path / "cube.tif"  # 3 bands x 12000 x 12000 = 432 M samples: too big raw, fine as 1 rendered plane
    with rasterio.open(cube, "w", driver="GTiff", width=12000, height=12000, count=3, dtype="uint8", sparse_ok=True,
                       tiled=True):
        pass
    ds_id = open_path(client, cube)["id"]
    r = client.get(f"/api/datasets/{ds_id}/export?kind=raw&format=tif")
    assert r.status_code == 413 and "3 bands" in r.json()["detail"]
    assert client.get(f"/api/datasets/{ds_id}/export?kind=raw&format=tif&bands=1&window=0,0,256,256").status_code == 200


# ---------------------------------------------------------------- label no-data on big JPEG 2000 products (HiRISE)

HIRISE_LIKE = """PDS_VERSION_ID = PDS3
TARGET_NAME = "MARS"
OBJECT = IMAGE_MAP_PROJECTION
    MAP_PROJECTION_TYPE          = "EQUIRECTANGULAR"
    A_AXIS_RADIUS                = 3386.150747 <KM>
    B_AXIS_RADIUS                = 3386.150747 <KM>
    C_AXIS_RADIUS                = 3386.150747 <KM>
    POSITIVE_LONGITUDE_DIRECTION = EAST
    CENTER_LATITUDE              = 0.0 <DEG>
    CENTER_LONGITUDE             = 0.0 <DEG>
    MAP_SCALE                    = 100.0 <METERS/PIXEL>
    LINE_PROJECTION_OFFSET       = 240.0 <PIXEL>
    SAMPLE_PROJECTION_OFFSET     = 320.0 <PIXEL>
END_OBJECT = IMAGE_MAP_PROJECTION
OBJECT = COMPRESSED_FILE
    FILE_NAME              = "SWATH.JP2"
    ENCODING_TYPE          = "JP2"
    UNCOMPRESSED_FILE_NAME = "SWATH.IMG"
END_OBJECT = COMPRESSED_FILE
OBJECT = UNCOMPRESSED_FILE
    FILE_NAME    = "SWATH.IMG"
    RECORD_TYPE  = FIXED_LENGTH
    RECORD_BYTES = 1280 <BYTES>
    FILE_RECORDS = 480
    ^IMAGE       = "SWATH.IMG"
    OBJECT = IMAGE
        LINES          = 480
        LINE_SAMPLES   = 640
        BANDS          = 1
        SAMPLE_TYPE    = MSB_UNSIGNED_INTEGER
        SAMPLE_BITS    = 16
        SCALING_FACTOR = 2.5e-05
        OFFSET         = 0.005
        CORE_NULL      = 0
    END_OBJECT = IMAGE
END_OBJECT = UNCOMPRESSED_FILE
END
"""


@pytest.fixture
def swath(tmp_path):
    """HiRISE-style RDR: a rotated swath of smooth data (300-500 DN) in a NULL (0) frame whose corners run off the
    image edges, as a lossless JPEG 2000 named by a PDS3 label (^IMAGE points at an .IMG that does not exist)."""
    yy, xx = np.mgrid[:480, :640]
    c, s = math.cos(math.radians(20)), math.sin(math.radians(20))
    u, v = (xx - 320) * c + (yy - 240) * s, -(xx - 320) * s + (yy - 240) * c
    inside = (abs(u) < 330) & (abs(v) < 150)
    a = np.where(inside, 400 + 50 * np.sin(xx / 40) + 50 * np.cos(yy / 50), 0).round().astype("uint16")
    write_tif(tmp_path / "src.tif", a)
    rasterio.shutil.copy(tmp_path / "src.tif", tmp_path / "SWATH.JP2", driver="JP2OpenJPEG", QUALITY="100",
                         REVERSIBLE="YES", RESOLUTIONS="4")
    (tmp_path / "SWATH.LBL").write_text(HIRISE_LIKE, "latin-1")
    return tmp_path / "SWATH.LBL", a


def _true_average(a, sc):
    """Mean of the valid (non-zero) source pixels under each sc x sc output pixel, NaN where none; and their fraction."""
    h, w = a.shape[0] // sc, a.shape[1] // sc
    blk = a[:h * sc, :w * sc].astype(float).reshape(h, sc, w, sc)
    ok = blk > 0
    n = ok.sum(axis=(1, 3))
    return np.where(n > 0, (blk * ok).sum(axis=(1, 3)) / np.maximum(n, 1), np.nan), n / sc / sc


def test_label_nodata_on_big_jpeg2000(client, swath, monkeypatch):
    """Big tiled/JP2 products get the label's CORE_NULL in our own reads (a VRT NoData sends GDAL's averaged reads to
    full resolution), its SCALING_FACTOR/OFFSET in a VRT; blended edge values of the JPEG 2000 levels stay hidden."""
    import rasterio.shutil  # noqa: F401
    monkeypatch.setattr(sources, "OPTIMIZE_MIN_PIXELS", 10_000)  # the 640 x 480 swath counts as "big"
    lbl, a = swath
    s = open_path(client, lbl)
    ds_id, i, e = s["id"], s["info"], sources.REG[s["id"]]
    assert e["_nodata"] == 0 and i["nodata"] == [0] and i["driver"] == "PDS"
    assert i["scales"] == [pytest.approx(2.5e-05)] and i["offsets"] == [pytest.approx(0.005)]
    st = client.get(f"/api/datasets/{ds_id}/stats?bands=1").json()["bands"][0]
    assert st["min"] >= 300 and st["valid"] < 0.85 * st["total"]  # the frame and its blends are left out

    def tile(z, x=0, y=0):
        return np.frombuffer(client.get(f"/api/datasets/{ds_id}/tile/{z}/{x}/{y}?bands=1").content, "<f4").reshape(256, 256, 2)
    mz = i["tiles"]["max_zoom"]
    for x in range(3):  # full resolution: no-data exactly where the data is 0, values untouched
        for y in range(2):
            t, src = tile(mz, x, y), a[y * 256:(y + 1) * 256, x * 256:(x + 1) * 256]
            h, w = src.shape
            assert ((t[:h, :w, 1] == 0) == (src == 0)).all() and (t[:h, :w, 0] == src).all()
    for dz in (1, 2):  # reduced JPEG 2000 levels (stitched across tile seams): shown pixels match their valid average
        sc = 2 ** dz
        nx, ny = math.ceil(640 / sc / 256), math.ceil(480 / sc / 256)
        t = np.zeros((ny * 256, nx * 256, 2), np.float32)
        for x in range(nx):
            for y in range(ny):
                t[y * 256:(y + 1) * 256, x * 256:(x + 1) * 256] = tile(mz - dz, x, y)
        true, frac = _true_average(a, sc)
        h, w = true.shape
        shown = t[:h, :w, 1] > 0
        assert shown.sum() > 0.5 * (frac == 1).sum()
        assert not (shown & (frac == 0)).any()
        err = np.abs(t[:h, :w, 0] - true) / true
        assert np.nanmax(np.where(shown, err, np.nan)) < 0.06, dz
    assert sources.REG[ds_id]["_serve"] in render._serial  # the VRT over label over JPEG 2000 still reads serially
    p = client.get(f"/api/datasets/{ds_id}/pixel?col=2&row=2").json()
    q = client.get(f"/api/datasets/{ds_id}/pixel?col=320&row=240").json()
    assert p["nodata"] == [True] and q["nodata"] == [False]
    assert q["scaled"][0] == pytest.approx(a[240, 320] * 2.5e-05 + 0.005)
    ll = i["lonlat_bounds"]
    r = client.get(f"/api/datasets/{ds_id}/warp", params={"crs": "lonlat", "bbox": ",".join(map(str, ll)),
                                                          "width": 160, "height": 120, "bands": 1})
    wv = np.frombuffer(r.content, "<f4").reshape(120, 160, 2)
    assert 0.3 < (wv[..., 1] > 0).mean() < 0.95 and wv[..., 0][wv[..., 1] > 0].min() >= 270
    r = client.get(f"/api/datasets/{ds_id}/export?kind=raw&format=tif&scale=0.25")
    with rasterio.MemoryFile(r.content) as mf, mf.open() as out:
        ex = out.read(1)
        assert out.nodata == 0 and out.scales == (pytest.approx(2.5e-05),) and out.offsets == (pytest.approx(0.005),)
        assert ex[ex > 0].min() >= 270


def test_label_nodata_small_product_uses_gdal(client, swath):
    """Below the size limit the label's no-data goes into the VRT and GDAL masks it itself."""
    lbl, a = swath
    s = open_path(client, lbl)
    assert sources.REG[s["id"]].get("_nodata") is None and s["info"]["nodata"] == [0]
    st = client.get(f"/api/datasets/{s['id']}/stats?bands=1").json()["bands"][0]
    assert st["min"] >= 300


def test_raw_export_keeps_each_band(tmp_path):
    """A band's valid values survive where another band is no-data: exact copy at 100 %, own mask per band where
    reduced reads of label no-data (or JPEG 2000) take the display's masks."""
    import rasterio.shutil  # noqa: F401
    a = np.stack([np.full((96, 128), v, "uint16") for v in (300, 400, 500)])
    a[0, 40:60, 40:60] = 0
    p = write_tif(tmp_path / "rgb.tif", a)
    write_tif(tmp_path / "rgb_nd.tif", a, nodata=0)
    render.export_file(str(tmp_path / "rgb_nd.tif"), tmp_path / "full.tif", "raw", "tif", [1, 2, 3], None, 1.0, "average", None)
    render.export_file(str(p), tmp_path / "half.tif", "raw", "tif", [1, 2, 3], None, 0.5, "average", None, 0)
    with rasterio.open(tmp_path / "full.tif") as o:
        assert (o.read() == a).all()
    with rasterio.open(tmp_path / "half.tif") as o:
        b = o.read()
        assert o.nodata == 0 and (b[0] == 0).sum() >= 100 and (b[1:] > 0).all()


def test_block_seam_sees_trailing_partial_row(tmp_path):
    """A read ending one source row short of the image hides what a whole-image read hides there."""
    a = np.full((65, 80), 400, "uint16")
    a[64, 30:40] = 0  # the image's last, partial output row at scale 2
    with rasterio.open(write_tif(tmp_path / "t.tif", a)) as ds:
        avg = rasterio.enums.Resampling.average
        whole = render.read_block(ds, [1], 0, 0, 80, 65, 2, avg, 0)
        parts = [render.read_block(ds, [1], 0, r, 80, h, 2, avg, 0) for r, h in ((0, 64), (64, 1))]
    assert (whole[1][29:32, 15:20] == 0).all()  # the hole's 3-px ring reaches into the first part
    assert (np.concatenate([p[1] for p in parts]) == whole[1]).all()
    assert (np.concatenate([p[0] for p in parts], axis=1) == whole[0]).all()


def test_stats_label_nodata_rounded_to_float32(tmp_path):
    a = np.full((40, 50), 7.5, "float32")
    a[:10, :10] = -9999.9  # stored as float32(-9999.9) != -9999.9
    p = str(write_tif(tmp_path / "f.tif", a))
    for exact in (False, True):
        st = render.stats(p, [1], exact, -9999.9)["bands"][0]
        assert st["min"] == 7.5 and st["valid"] == 40 * 50 - 100


@pytest.mark.parametrize("tile, reads", [(1024, 3 * 2), (256, 1)])
def test_exact_stats_lock_jpeg2000_per_block(tmp_path, tile, reads):
    """A full scan locks a shared JPEG 2000 handle per read, not for minutes, so tiles get turns: one read per big
    codec block, ~4 MP strips for small blocks (GDAL decodes those in parallel)."""
    import rasterio.shutil  # noqa: F401
    a = (np.arange(1100 * 2100).reshape(1100, 2100) % 997 + 1).astype("uint16")
    p = str(tmp_path / "s.jp2")
    rasterio.shutil.copy(write_tif(tmp_path / "s.tif", a), p, driver="JP2OpenJPEG", QUALITY="100", REVERSIBLE="YES",
                         tiled=True, blockxsize=tile, blockysize=tile)
    ds = render.open_ds(p)
    lock = render._shared[p][1]
    holds = []

    class Spy:
        def __enter__(self):
            lock.acquire()
            holds.append(1)

        def __exit__(self, *exc):
            lock.release()
    render._shared[p] = (ds, Spy())
    try:
        st = render.stats(p, [1], True)["bands"][0]
    finally:
        render._shared[p] = (ds, lock)
    assert len(holds) == reads
    assert (st["min"], st["max"], st["valid"]) == (1, 997, a.size)
    assert st["mean"] == pytest.approx(a.mean()) and st["std"] == pytest.approx(a.std())


def test_exact_std_far_from_zero(tmp_path):
    a = 1e12 + np.random.default_rng(2).normal(0, 1, (300, 400))  # sum(v^2) - n mean^2 loses everything here
    st = render.stats(str(write_tif(tmp_path / "d.tif", a)), [1], True)["bands"][0]
    assert st["std"] == pytest.approx(a.std(), rel=1e-6) and st["mean"] == pytest.approx(a.mean(), rel=1e-15)


def test_jp2_check_skips_mosaic_sources(tmp_path, monkeypatch):
    """Only labels and VRTs inside a VRT can wrap a JPEG 2000; a mosaic's TIFFs are not opened to find out."""
    import rasterio.shutil  # noqa: F401
    rasterio.shutil.copy(write_tif(tmp_path / "t.tif", np.ones((4, 4), "uint8")), tmp_path / "m.vrt", driver="VRT")
    with rasterio.open(tmp_path / "m.vrt") as ds:
        monkeypatch.setattr(render, "open_raw", lambda p: pytest.fail(f"opened {p}"))
        assert not render._jp2(ds)


def test_dilate_matches_brute_force():
    rng = np.random.default_rng(3)
    m = rng.random((37, 53)) > 0.97
    for r in (1, 3, 12):
        want = np.zeros_like(m)
        for y, x in np.argwhere(m):
            want[max(0, y - r):y + r + 1, max(0, x - r):x + r + 1] = True
        assert (render._dilate(m, r) == want).all()
