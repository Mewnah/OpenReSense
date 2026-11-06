"""Format readers and fallbacks on real samples (scripts/fetch_samples.py), downloads, streaming (network)."""
import functools
import http.server
import io
import shutil
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
import rasterio

from server import config
from server.raster import sources
from test_raster import client, env, open_path, wait  # noqa: F401  (fixtures shared with test_raster.py)

DATA = Path(__file__).parent / "data"
needs_samples = pytest.mark.skipif(not (DATA / "pds3" / "LDEM_4.LBL").exists(), reason="run scripts/fetch_samples.py")


def sample(rel):
    p = DATA / rel
    if not p.exists():
        pytest.skip("run scripts/fetch_samples.py")
    return p


def native_tile(client, status, z=0, x=0, y=0):
    r = client.get(f"/api/datasets/{status['id']}/tile/{z}/{x}/{y}?bands=1")
    assert r.status_code == 200
    return np.frombuffer(r.content, "<f4").reshape(256, 256, 2)


@needs_samples
@pytest.mark.parametrize("rel,driver,body,converted", [
    ("pds3/LDEM_4.LBL", "PDS", "moon", None),
    ("pds3/LDEM_4.IMG", "PDS", "moon", None),  # bare data file: the sibling label is found
    ("pds4/byte_pds4_cart_1700.xml", "PDS4", None, None),
    ("isis3/isis3_detached.lbl", "ISIS3", "mars", None),
    ("vicar/vicar_int16.vic", "VICAR", None, None),
    ("fits/image_in_second_hdu.fits", "FITS", None, "astropy"),
    ("hdf5/deflate.h5", "HDF5Image", None, None),
    ("netcdf/byte.nc", "netCDF", None, None),
    ("cassini_titan/N1481618305_4.LBL", "PDS", None, None),
])
def test_sample_formats(client, rel, driver, body, converted):
    s = open_path(client, sample(rel))
    i = s["info"]
    assert i["driver"] == driver and i["count"] >= 1 and i["files"]
    assert body is None or i["crs"]["body"] == body
    assert (i["converted"] or {}).get("from") == converted
    if "LDEM" not in rel:  # the GDAL sample's data file is truncated
        assert native_tile(client, s)[0, 0, 1] in (0, 1)
    if driver in ("PDS", "PDS4", "ISIS3", "VICAR", "FITS"):
        assert i["label"]


@needs_samples
def test_labels(client):
    pds = open_path(client, sample("pds3/LDEM_4.LBL"))["info"]
    assert "PDS_VERSION_ID" in pds["label"] and pds["lonlat_bounds"] == pytest.approx([-180, -90, 180, 90])
    assert pds["georef"] == "transform" and pds["crs"]["a"] == 1737400
    isis = open_path(client, sample("isis3/isis3_detached.lbl"))["info"]
    assert "Object" in isis["label"] or "IsisCube" in isis["label"]
    assert open_path(client, sample("pds4/byte_pds4_cart_1700.xml"))["info"]["label"].lstrip().startswith("<?xml")


@needs_samples
def test_pdr_and_astropy_fallbacks(client, env):
    lbl = sample("cassini_titan/N1481618305_4.LBL")
    with rasterio.open(lbl) as ds:
        want = ds.read(1)
    e = {"id": "d_pdr0000001", "status": "converting"}
    info = sources._fallback(e, str(lbl), "forced")
    assert info["converted"]["from"] == "pdr" and info["driver"] == "PDS" and info["label"]
    with rasterio.open(e["_serve"]) as ds:
        np.testing.assert_array_equal(ds.read(1), want)
    fits = sample("fits/byte_merc.fits")
    from astropy.io import fits as pf
    e = {"id": "d_fits000001", "status": "converting"}
    info = sources._fallback(e, str(fits), "GDAL has no FITS driver")
    assert info["converted"]["from"] == "astropy" and "SIMPLE" in info["label"]
    with rasterio.open(e["_serve"]) as ds, pf.open(fits) as h:
        np.testing.assert_array_equal(ds.read(1), h[0].data[::-1])  # bottom-up FITS rows flipped for display


@needs_samples
def test_hdf4_subdatasets(client):
    s = open_path(client, sample("hdf4/hdifftst2.hdf"))
    i = s["info"]
    assert i["count"] == 0 and i["driver"] == "HDF4" and len(i["subdatasets"]) >= 1
    assert client.get(f"/api/datasets/{s['id']}/tile/0/0/0").status_code == 400
    sub = i["subdatasets"][0]["name"]
    assert sub.startswith("HDF4PY:")
    s2 = open_path(client, sub)
    assert s2["info"]["count"] >= 1 and s2["info"]["converted"]["from"] == "pyhdf" and s2["info"]["driver"] == "HDF4"
    assert native_tile(client, s2).shape == (256, 256, 2)


def test_hdf5_subdatasets_listed(client, tmp_path):
    import h5py
    p = tmp_path / "two.h5"
    with h5py.File(p, "w") as f:
        f["a"] = np.arange(12, dtype="float32").reshape(3, 4)
        f["b"] = np.ones((5, 6), "uint8")
    i = open_path(client, p)["info"]
    assert i["count"] == 0 and len(i["subdatasets"]) == 2
    s = open_path(client, i["subdatasets"][0]["name"])
    assert s["info"]["width"] in (4, 6)


def test_pointer_parsing():
    lbl = '''PDS_VERSION_ID = PDS3
^IMAGE_HEADER = ("N1_4.IMG",1)
^IMAGE = ("N1_4.IMG",5 <BYTES>)
^QUBE = "CUBE.QUB"
^SPECTRAL_QUBE = ( "spec.cub" , 1)
^DESCRIPTION = "readme.txt"
^STRUCTURE = "PREFIX.FMT"
^HISTOGRAM_IMAGE = 4
'''
    assert sources.pds3_pointers(lbl) == ["N1_4.IMG", "CUBE.QUB", "spec.cub"]
    xml = ('<Product xmlns="x"><pds:file_name>c.tab</pds:file_name><File_Area_Observational><File>'
           '<file_name>a.img</file_name></File><file_name>b.dat</file_name></File_Area_Observational></Product>')
    assert sources.pds4_files(xml) == ["c.tab", "a.img", "b.dat"]


# ---------------------------------------------------------------- downloads from a local server

@pytest.fixture
def server(tmp_path):
    root = tmp_path / "www"
    root.mkdir()

    class H(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):  # lower-case-only data files: the exact and UPPER candidates must 404
            if self.path.lower().endswith(".img") and self.path != self.path.lower():
                return self.send_error(404)
            super().do_GET()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@needs_samples
def test_download_pds3_pair_case_variant(client, server):
    root, base = server
    shutil.copy(DATA / "pds3" / "LDEM_4.LBL", root / "LDEM_4.LBL")
    shutil.copy(DATA / "pds3" / "LDEM_4.IMG", root / "ldem_4.img")
    s = wait(client, client.post("/api/datasets/open", json={"url": f"{base}/LDEM_4.LBL"}).json()["id"])
    assert s["status"] == "ready", s["message"]
    assert s["bytes_done"] == s["bytes_total"] == 4282 + 10000 and s["progress"] == 1.0
    assert {Path(f).name for f in s["info"]["files"]} == {"LDEM_4.LBL", "LDEM_4.IMG"}
    assert s["info"]["crs"]["body"] == "moon"
    # label whose data file is missing on the server -> error naming the file
    shutil.copy(root / "LDEM_4.LBL", root / "NOIMG.LBL")
    (root / "ldem_4.img").unlink()
    s = wait(client, client.post("/api/datasets/open", json={"url": f"{base}/NOIMG.LBL"}).json()["id"])
    assert s["status"] == "error" and "LDEM_4.IMG" in s["message"]


@needs_samples
def test_download_img_finds_sibling_label_and_companions(client, server):
    root, base = server
    shutil.copy(DATA / "pds3" / "LDEM_4.LBL", root / "LDEM_4.LBL")
    shutil.copy(DATA / "pds3" / "LDEM_4.IMG", root / "ldem_4.img")
    (root / "extra.txt").write_text("notes")
    s = wait(client, client.post("/api/datasets/open", json={
        "url": f"{base}/ldem_4.img", "companions": [f"{base}/extra.txt"]}).json()["id"])
    assert s["status"] == "ready", s["message"]
    assert s["info"]["driver"] == "PDS" and s["info"]["crs"]["body"] == "moon"
    assert (config.CACHE_DIR / "downloads" / s["id"][2:] / "extra.txt").exists()


def test_download_http_error_reports(client, server):
    _, base = server
    s = wait(client, client.post("/api/datasets/open", json={"url": f"{base}/nothing.tif", "mode": "download"}).json()["id"])
    assert s["status"] == "error" and "404" in s["message"]


def test_earthdata_token_only_to_nasa_hosts(monkeypatch):
    monkeypatch.setattr(config, "load_settings", lambda: {"earthdata_token": "TOKEN"})
    seen = []

    def handler(req):
        seen.append((req.url.host, req.headers.get("authorization")))
        nxt = {"data.asf.alaska.edu": "https://urs.earthdata.nasa.gov/oauth",
               "urs.earthdata.nasa.gov": "https://elsewhere.example.com/f.tif"}.get(req.url.host)
        return httpx.Response(302, headers={"location": nxt}) if nxt else httpx.Response(200, content=b"ok")

    c = sources._client()
    c._transport = httpx.MockTransport(handler)
    c.get("https://data.asf.alaska.edu/x.tif")
    assert seen == [("data.asf.alaska.edu", "Bearer TOKEN"), ("urs.earthdata.nasa.gov", "Bearer TOKEN"),
                    ("elsewhere.example.com", None)]
    seen.clear()
    c.get("http://data.asf.alaska.edu/x.tif")  # never over plain http
    assert seen[0][1] is None
    assert sources.sign_url("https://example.com/a.tif") == "https://example.com/a.tif"


# ---------------------------------------------------------------- network

@pytest.mark.network
def test_planetary_computer_signing(client):
    u = "https://elevationeuwest.blob.core.windows.net/copernicus-dem/COP90_hh/Copernicus_DSM_COG_30_S90_00_W180_00_DEM.tif"
    signed = sources.sign_url(u)
    assert "sig=" in signed and signed.startswith(u)
    s = wait(client, client.post("/api/datasets/open", json={"url": u}).json()["id"], 60)  # streamed with a signed URL
    assert s["status"] == "ready", s["message"]
    assert s["info"]["dtype"] == "float32" and sources.REG[s["id"]]["_stream"] == u


@pytest.mark.network
def test_real_cassini_titan_detached_label(client):
    base = "https://opus.pds-rings.seti.org/holdings/volumes/COISS_2xxx/COISS_2008/data/1481618078_1481626758"
    s = wait(client, client.post("/api/datasets/open", json={"url": f"{base}/N1481618305_4.IMG"}).json()["id"], 120)
    assert s["status"] == "ready", s["message"]
    i = s["info"]
    assert (i["width"], i["height"], i["driver"]) == (512, 512, "PDS") and "TITAN" in i["label"]
    assert {Path(f).name for f in i["files"]} == {"N1481618305_4.LBL", "N1481618305_4.IMG"}
    assert s["bytes_done"] == s["bytes_total"] > 500_000


@pytest.mark.network
def test_stream_remote_cog(client):
    f = DATA / "cog_url.txt"
    if not f.exists():
        pytest.skip("run scripts/fetch_samples.py")
    url = f.read_text().strip()
    t0 = time.time()
    s = wait(client, client.post("/api/datasets/open", json={"url": url}).json()["id"], 60)
    assert s["status"] == "ready", s["message"]
    assert time.time() - t0 < 30 and s["info"]["files"] == [] and s["bytes_done"] == 0
    assert sources.REG[s["id"]]["_stream"] == url and s["info"]["overviews"]
    mz = s["info"]["tiles"]["max_zoom"]
    r = client.get(f"/api/datasets/{s['id']}/tile/{max(mz - 3, 0)}/0/0?bands=1")
    assert r.status_code == 200 and len(r.content) == 256 * 256 * 8
    ll = s["info"]["lonlat_bounds"]
    r = client.get(f"/api/datasets/{s['id']}/warp", params={
        "crs": "lonlat", "bbox": f"{ll[0]},{ll[1]},{ll[2]},{ll[3]}", "width": 128, "height": 128, "bands": 1})
    assert r.status_code == 200 and np.frombuffer(r.content, "<f4").reshape(128, 128, 2)[..., 1].max() == 1


@needs_samples
def test_pds3_one_based_offsets_use_label_extent(client, tmp_path):
    """MOLA/MESSENGER-style labels count lines/samples from 1; GDAL alone lands the map one pixel north-west."""
    lbl = (sample("pds3/LDEM_4.LBL").read_text("latin-1")
           .replace("LINE_PROJECTION_OFFSET       = 359.5", "LINE_PROJECTION_OFFSET       = 360.5")
           .replace("SAMPLE_PROJECTION_OFFSET     = 719.5", "SAMPLE_PROJECTION_OFFSET     = 720.5"))
    assert "360.5" in lbl and "720.5" in lbl
    (tmp_path / "LDEM_4.LBL").write_text(lbl, "latin-1")
    shutil.copy(sample("pds3/LDEM_4.IMG"), tmp_path / "LDEM_4.IMG")
    i = open_path(client, tmp_path / "LDEM_4.LBL")["info"]
    assert i["driver"] == "PDS" and i["lonlat_bounds"] == pytest.approx([-180, -90, 180, 90], abs=1e-6)


def test_fits_bscale_bzero_kept_as_scale_offset(client, tmp_path):
    """astropy refuses memmap on scaled FITS; the raw ints plus BSCALE/BZERO are what we serve."""
    from astropy.io import fits
    a = np.arange(40 * 30, dtype="float32").reshape(30, 40)
    hdu = fits.PrimaryHDU(a.copy())
    hdu.scale("int16", bscale=0.5, bzero=10.0)
    hdu.writeto(tmp_path / "scaled.fits")
    s = open_path(client, tmp_path / "scaled.fits")
    i = s["info"]
    assert i["converted"]["from"] == "astropy" and i["dtype"] == "int16"
    assert i["scales"] == [0.5] and i["offsets"] == [10.0]
    p = client.get(f"/api/datasets/{s['id']}/pixel?col=3&row=0").json()  # FITS row 0 is the bottom: image row 0 = a[29]
    assert p["scaled"][0] == pytest.approx(a[29, 3], abs=0.5)


def test_envi_cube_downloads_its_header(client, server):
    """GDAL only recognises a raw .IMG cube through its ENVI .HDR (M3, CRISM...)."""
    root, base = server
    cube = np.arange(2 * 3 * 4, dtype="<i2").reshape(2, 3, 4)  # bands, lines, samples (bsq)
    cube.tofile(root / "cube.img")
    (root / "cube.HDR").write_text("ENVI\nsamples = 4\nlines = 3\nbands = 2\nheader offset = 0\nfile type = ENVI Standard\n"
                                   "data type = 2\ninterleave = bsq\nbyte order = 0\nwavelength = {500.5, 600.5}\n")
    s = wait(client, client.post("/api/datasets/open", json={"url": f"{base}/cube.img"}).json()["id"])
    assert s["status"] == "ready", s["message"]
    i = s["info"]
    assert i["driver"] == "ENVI" and i["count"] == 2 and i["wavelengths"] == [500.5, 600.5] and "wavelength" in i["label"]


def test_download_urls_with_filename_in_query():
    atlas = "https://pds-imaging.jpl.nasa.gov/pds/prod?q=OFSN+%3D+/data/messenger//MSGRMDS_5001/MDR/H02//MDIS_MDR.LBL+AND+RT+%3D+PDS_LABEL"
    assert sources._file_name(atlas) == "MDIS_MDR.LBL"
    assert sources.sibling_url(atlas, "MDIS_MDR.IMG") == atlas.replace("MDIS_MDR.LBL", "MDIS_MDR.IMG").replace("PDS_LABEL", "RAW")
    assert sources._file_name("https://mast.stsci.edu/api/v0.1/Download/file?uri=mast:HST/product/x_drz.fits") == "x_drz.fits"
    assert sources.sibling_url("https://h/a/b.LBL", "b.IMG") == "https://h/a/b.IMG"


@needs_samples
def test_label_pointer_missing_falls_back_to_file_name(client, server):
    """HiRISE labels point at an uncompressed .IMG that is only published as the .JP2 named in FILE_NAME."""
    root, base = server
    (root / "ldem_4.img").write_bytes((DATA / "pds3" / "LDEM_4.IMG").read_bytes())
    lbl = (DATA / "pds3" / "LDEM_4.LBL").read_text("latin-1").replace('^IMAGE', 'FILE_NAME = "ldem_4.img"\n^IMAGE').replace('"LDEM_4.IMG"', '"GONE.IMG"')
    (root / "HI.LBL").write_text(lbl, "latin-1")
    s = wait(client, client.post("/api/datasets/open", json={"url": f"{base}/HI.LBL"}).json()["id"])
    assert (config.CACHE_DIR / "downloads" / s["id"][2:] / "ldem_4.img").exists()


def test_truncated_download_is_an_error_and_reopen_works(client, tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "_BACKOFF", 0)
    from PIL import Image
    png = tmp_path / "ok.png"
    Image.fromarray(np.arange(64 * 64, dtype="uint8").reshape(64, 64) % 251).save(png)
    body, state = png.read_bytes(), {"cut": True}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[:30] if state["cut"] else body)
            self.close_connection = True

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/cut.png"
    try:
        s = wait(client, client.post("/api/datasets/open", json={"url": url}).json()["id"])
        assert s["status"] == "error" and s["message"] and s["progress"] < 1
        state["cut"] = False
        s = wait(client, client.post("/api/datasets/open", json={"url": url}).json()["id"])  # same id, fresh job
        assert s["status"] == "ready", s["message"]
    finally:
        srv.shutdown()


def _serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


@pytest.mark.parametrize("case", ["resume", "changed", "wrong_range", "short_range"])
def test_download_resumes_after_a_dropped_connection(client, tmp_path, monkeypatch, case):
    """A dropped transfer is retried from where it stopped (Range + If-Range). A file that changed meanwhile starts
    over, and so does a 206 that is not exactly the rest of the file (a server ignoring the offset, a proxy capping it)."""
    monkeypatch.setattr(sources, "_BACKOFF", 0)
    from PIL import Image
    bodies = []
    for seed in (1, 2):  # random pixels: a ~3 MB PNG, more than the 1 MB chunks written before the drop
        buf = io.BytesIO()
        Image.fromarray(np.random.default_rng(seed).integers(0, 256, (1800, 1800), dtype="uint8")).save(buf, "PNG")
        bodies.append(buf.getvalue())
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.headers.get("Range"), self.headers.get("If-Range")))
            tag, body = ('"v2"', bodies[1]) if case == "changed" and len(seen) > 1 else ('"v1"', bodies[0])
            rng = self.headers.get("Range")
            start = int(rng[6:-1]) if rng and self.headers.get("If-Range") == tag else 0
            end = len(body)
            if start and case == "wrong_range":
                start = 100
            if start and case == "short_range":
                end = start + 100_000
            self.send_response(206 if start else 200)
            if start:
                self.send_header("Content-Range", f"bytes {start}-{end - 1}/{len(body)}")
            self.send_header("ETag", tag)
            self.send_header("Content-Length", str(end - start))
            self.end_headers()
            self.wfile.write(body[start:end] if len(seen) > 1 else body[:2_500_000])  # the first transfer drops
            self.close_connection = True

        def log_message(self, *a):
            pass

    srv, base = _serve(H)
    try:
        s = wait(client, client.post("/api/datasets/open", json={"url": f"{base}/drop_{case}.png"}).json()["id"])
        assert s["status"] == "ready", s["message"]
        assert seen[0] == (None, None) and len(seen) == (3 if case.endswith("_range") else 2)
        assert seen[1][0].startswith("bytes=") and int(seen[1][0][6:-1]) >= 1 << 20 and seen[1][1] == '"v1"'
        if len(seen) == 3:
            assert seen[2] == (None, None)  # the bad 206 is dropped and the whole file asked for again
        got = (config.CACHE_DIR / "downloads" / s["id"][2:] / f"drop_{case}.png").read_bytes()
        assert got == bodies[case == "changed"]
        assert s["bytes_done"] == s["bytes_total"] == len(got)
    finally:
        srv.shutdown()


def _pds3_server(lbl_status):
    """Bare 10 x 10 X.IMG with a detached X.LBL; lbl_status(n) is the status of the n-th label request (200 or not)."""
    img = bytes(range(100))
    lbl = ('PDS_VERSION_ID = PDS3\nRECORD_TYPE = FIXED_LENGTH\nRECORD_BYTES = 10\nFILE_RECORDS = 10\n^IMAGE = "X.IMG"\n'
           'OBJECT = IMAGE\n  LINES = 10\n  LINE_SAMPLES = 10\n  SAMPLE_TYPE = UNSIGNED_INTEGER\n  SAMPLE_BITS = 8\n'
           'END_OBJECT = IMAGE\nEND\n').encode()
    log, live = [], {"now": 0, "max": 0}
    lock = threading.Lock()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            name = self.path.rsplit("/", 1)[-1]
            with lock:
                log.append(name)
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])
                n = log.count(name)
            try:
                if name != "X.IMG":
                    time.sleep(0.2)  # a slow 404 / label, like the PDS redirect + new connection
                status = 200 if name == "X.IMG" else lbl_status(n) if name == "X.LBL" else 404
                body = img if name == "X.IMG" else lbl
                self.send_response(status)
                self.send_header("Content-Length", str(len(body) if status == 200 else 0))
                self.end_headers()
                if status == 200:
                    self.wfile.write(body)
            finally:
                with lock:
                    live["now"] -= 1

        def log_message(self, *a):
            pass

    srv, base = _serve(H)
    return srv, base, log, live


def test_label_probes_run_in_parallel_and_retry(client, monkeypatch):
    monkeypatch.setattr(sources, "_BACKOFF", 0)
    srv, base, log, live = _pds3_server(lambda n: 503 if n == 1 else 200)  # the label fails once
    try:
        s = wait(client, client.post("/api/datasets/open", json={"url": f"{base}/X.IMG"}).json()["id"])
        assert s["status"] == "ready", s["message"]
        assert s["info"]["driver"] == "PDS" and {Path(f).name for f in s["info"]["files"]} == {"X.LBL", "X.IMG"}
        assert live["max"] >= 3 and log.count("X.LBL") == 2  # siblings fetched together; the 503 was retried
    finally:
        srv.shutdown()


def test_label_that_keeps_failing_fails_the_open(client, monkeypatch):
    """A label that exists but keeps failing must not leave a dataset cached as Ready without it."""
    monkeypatch.setattr(sources, "_BACKOFF", 0)
    srv, base, log, _ = _pds3_server(lambda n: 503)
    try:
        s = wait(client, client.post("/api/datasets/open", json={"url": f"{base}/X.IMG"}).json()["id"])
        assert s["status"] == "error" and "503" in s["message"] and log.count("X.LBL") == 4
    finally:
        srv.shutdown()


def test_attached_pds3_label_skips_sibling_probes(client, tmp_path):
    label = ("PDS_VERSION_ID = PDS3\nRECORD_TYPE = FIXED_LENGTH\nRECORD_BYTES = 200\nFILE_RECORDS = 3\nLABEL_RECORDS = 2\n"
             "^IMAGE = 3\nOBJECT = IMAGE\n  LINES = 10\n  LINE_SAMPLES = 10\n  SAMPLE_TYPE = UNSIGNED_INTEGER\n"
             "  SAMPLE_BITS = 8\nEND_OBJECT = IMAGE\nEND\n").encode().ljust(400) + bytes(range(100)).ljust(200, b"\0")
    log = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            log.append(self.path)
            ok = self.path.endswith("/A.IMG")
            self.send_response(200 if ok else 404)
            self.send_header("Content-Length", str(len(label) if ok else 0))
            self.end_headers()
            if ok:
                self.wfile.write(label)

        def log_message(self, *a):
            pass

    srv, base = _serve(H)
    try:
        s = wait(client, client.post("/api/datasets/open", json={"url": f"{base}/A.IMG"}).json()["id"])
        assert s["status"] == "ready", s["message"]
        assert s["info"]["width"] == 10 and log == ["/A.IMG"]
    finally:
        srv.shutdown()


def test_signing_only_for_blob_storage_hosts(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call the signing API")
    monkeypatch.setattr(sources.httpx, "get", boom)
    for u in ("https://planetarycomputer.microsoft.com/api/data/v1/item/preview.png?collection=x",
              "https://evil.example.com/a.blob.core.windows.net/x.tif", "https://x.blob.core.windows.net.evil.com/a.tif",
              "https://acct.blob.core.windows.net/c/a.tif?sv=1&sig=abc"):
        assert sources.sign_url(u) == u
