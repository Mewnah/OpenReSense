"""Dataset registry, open/download jobs, reader fallbacks, COG conversion, body detection and filesystem listing."""
import hashlib
import json
import math
import os
import re
import shutil
import string
import threading
import time
import uuid
import warnings
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx
import numpy as np
import rasterio
import rasterio.shutil
import rasterio.windows
from rasterio.errors import RasterioIOError

from .. import config
from ..connectors.base import is_openable
from . import render

OPTIMIZE_MIN_PIXELS = 16_000_000  # un-tiled, overview-less rasters above this get a background COG copy
COG_OPTS = dict(BLOCKSIZE=512, COMPRESS="DEFLATE", PREDICTOR="YES", OVERVIEW_RESAMPLING="AVERAGE",
                BIGTIFF="IF_SAFER", NUM_THREADS="ALL_CPUS")
EDL_HOSTS = ("earthdata.nasa.gov", "earthdatacloud.nasa.gov", "eosdis.nasa.gov", "asf.alaska.edu",
             "podaac.jpl.nasa.gov", "nsidc.org", "lpdaac.usgs.gov", "daac.ornl.gov")
PDS3_DATA_POINTER = re.compile(r"IMAGE|QUBE|HEADER|HISTOGRAM|TABLE|ARRAY")

LOCK = threading.RLock()
REG: dict[str, dict] = {}
_jobs = ThreadPoolExecutor(4, "open")
_optimizers = ThreadPoolExecutor(2, "optimize")


# ---------------------------------------------------------------- registry

def _reg_file() -> Path:
    return config.CACHE_DIR / "datasets.json"


def reload() -> None:
    """(Re)load the persisted registry; jobs that were running when the app stopped become errors."""
    with LOCK:
        REG.clear()
        try:
            saved = json.loads(_reg_file().read_text("utf-8"))
        except (FileNotFoundError, ValueError):
            saved = []
        for e in saved:
            if e["status"] in ("queued", "downloading", "converting"):
                e.update(status="error", message="Interrupted by a restart; open it again.")
            e["optimizing"] = False
            REG[e["id"]] = e


def save() -> None:
    with LOCK:
        tmp = _reg_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(list(REG.values())), "utf-8")
        tmp.replace(_reg_file())


def public(e: dict) -> dict:
    return {k: v for k, v in dict(e).items() if not k.startswith("_")}  # dict(): jobs mutate entries concurrently


def listing() -> list[dict]:
    with LOCK:
        return [public(e) for e in sorted(REG.values(), key=lambda e: -e["_created"])]


def get(ds_id: str) -> dict:
    e = REG.get(ds_id)
    if e is None:
        raise KeyError(ds_id)
    return e


def serve_path(e: dict) -> str:
    """What GDAL should open for this dataset (re-signs Planetary Computer URLs, which expire)."""
    if e.get("_stream"):
        return "/vsicurl/" + sign_url(e["_stream"])
    return e["_serve"]


def delete(ds_id: str, purge: bool) -> None:
    with LOCK:
        e = REG.pop(ds_id)
        save()
    if purge:
        render.close_all()
        for d in (e.get("_dir"), config.CACHE_DIR / "downloads" / ds_id[2:]):
            d = Path(d).resolve() if d else None
            if d and config.CACHE_DIR.resolve() in d.parents:  # never delete anything outside the cache
                shutil.rmtree(d, ignore_errors=True)


reload()


# ---------------------------------------------------------------- open requests

_SUBDATASET = re.compile(r'^[A-Za-z0-9_]+:"([^"]+)"')


def _dataset_id(key: str) -> str:
    return "d_" + hashlib.sha1(key.encode()).hexdigest()[:10]


_OFSN = re.compile(r"(OFSN\s*=\s*)(\S+?)(\s+AND\s+RT\s*=\s*)(\w+)", re.I)


def _atlas_query(url: str) -> re.Match | None:
    """PDS Imaging Atlas / Starbrite download URLs are `prod?q=OFSN = /path/FILE.IMG AND RT = RAW|PDS_LABEL`."""
    u = urlsplit(url)
    return _OFSN.search(unquote(u.query).replace("+", " ")) if u.path.endswith("/prod") else None


def sibling_url(url: str, name: str) -> str:
    """URL of file `name` in the same directory as `url`'s file."""
    m = _atlas_query(url)
    if not m:
        return urljoin(url, name)
    u = urlsplit(url)
    ofsn = m.group(2).rsplit("/", 1)[0] + "/" + name
    q = _OFSN.sub(lambda _: f"{m.group(1)}{ofsn}{m.group(3)}RAW", unquote(u.query).replace("+", " "))
    return u._replace(query=q.replace(" = ", "+%3D+").replace(" ", "+")).geturl()


def _safe_name(name: str) -> str:
    """Bare file name from a URL, label or upload: it can never point outside the folder it is joined to."""
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", re.split(r"[\\/]", name)[-1]).strip()
    return "data" if name in ("", ".", "..") else name


def _file_name(url_or_path: str) -> str:
    m = _atlas_query(url_or_path) if "://" in url_or_path else None
    if m:
        return _file_name(m.group(2))
    if "://" in url_or_path and "." not in urlsplit(url_or_path).path.rsplit("/", 1)[-1]:
        for v in parse_qs(urlsplit(url_or_path).query).get("uri", []):  # MAST: /Download/file?uri=mast:HST/product/x.fits
            return _file_name(v.replace(":", "/"))
    return _safe_name(unquote(urlsplit(url_or_path).path.rstrip("/").rsplit("/", 1)[-1]) if "://" in url_or_path
                      else url_or_path.rstrip("\\/"))


def open_dataset(url=None, path=None, name=None, companions=None, mode="auto", meta=None) -> dict:
    if bool(url) == bool(path):
        raise ValueError("give exactly one of url or path")
    if mode not in ("auto", "stream", "download"):
        raise ValueError(f"bad mode {mode!r}")
    if url:
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("url must be http(s)")
        key = url
    else:
        m = _SUBDATASET.match(path)
        real = m.group(1) if m else path
        if not os.path.exists(real):
            raise FileNotFoundError(real)
        key = path if m else os.path.normcase(os.path.abspath(path))
    ds_id = _dataset_id(key)
    with LOCK:
        e = REG.get(ds_id)
        if e and e["status"] != "error" and not (e["status"] == "ready" and not _alive(e)):
            return public(e)
        e = REG[ds_id] = {
            "id": ds_id, "name": name or _file_name(url or path), "status": "queued", "optimizing": False,
            "progress": 0.0, "message": "Queued", "bytes_done": 0, "bytes_total": 0,
            "source": {"url": url, "path": None if url else path}, "meta": meta or {}, "info": None,
            "_created": time.time(), "_mode": mode, "_companions": list(companions or []),
        }
        save()
    _jobs.submit(_open_job, ds_id)
    return public(e)


def _alive(e: dict) -> bool:
    s = e.get("_serve")
    return bool(e.get("_stream")) or not s or not os.path.isabs(s) or os.path.exists(s)


def _open_job(ds_id: str) -> None:
    e = REG[ds_id]
    try:
        if e["source"]["url"]:
            serve, stream = _fetch(e)
        else:
            path = e["source"]["path"]
            serve, stream = str(_detached_label(Path(path)) or path), None
        e.update(status="converting", message="Reading")
        info = _describe_any(e, serve, stream)
        e.update(info=info, progress=1.0, optimizing=_wants_optimizing(e, info))
        e.update(status="ready", message="Ready (building a faster copy in the background)" if e["optimizing"] else "Ready")
        if e["optimizing"]:
            _optimizers.submit(_optimize_job, e["id"])
    except Exception as ex:  # job boundary: report any failure to the UI
        e.update(status="error", message=f"{type(ex).__name__}: {ex}"[:500])
    save()


# ---------------------------------------------------------------- network

def _is_edl(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in EDL_HOSTS)


def _client() -> httpx.Client:
    token = config.load_settings().get("earthdata_token")

    def auth(req: httpx.Request) -> None:  # runs on every redirect hop too
        if token and req.url.scheme == "https" and _is_edl(req.url.host):
            req.headers["Authorization"] = f"Bearer {token}"
        else:
            req.headers.pop("Authorization", None)
    return httpx.Client(follow_redirects=True, headers={"User-Agent": config.USER_AGENT},
                        timeout=httpx.Timeout(30.0, read=60.0), event_hooks={"request": [auth]})


_sas: dict[str, tuple[str, float]] = {}


def sign_url(url: str) -> str:
    u = urlsplit(url)
    if not u.hostname or not u.hostname.endswith(".blob.core.windows.net") or "sig=" in u.query:
        return url
    hit = _sas.get(url)
    if hit and hit[1] > time.time():
        return hit[0]
    r = httpx.get("https://planetarycomputer.microsoft.com/api/sas/v1/sign", params={"href": url},
                  headers={"User-Agent": config.USER_AGENT}, timeout=20)
    r.raise_for_status()
    signed = r.json()["href"]
    _sas[url] = (signed, time.time() + 25 * 60)
    return signed


_ABSENT = {403, 404, 410}  # an optional file that is not there (S3 answers 403 for a missing key)
_RETRY = {408, 425, 429, 500, 502, 503, 504}
_BACKOFF = 1.0  # seconds before the first retry, doubling


def _download(client: httpx.Client, url: str, dest: Path, e: dict, required: bool, tries: int = 4) -> bool:
    """Download url to dest; False when an optional file is not there. Dropped connections and server errors are
    retried, resuming from the .part file (If-Range: a file that changed meanwhile starts over); a transient failure
    that persists raises even for optional files, so a dataset is never cached without a label that does exist."""
    if dest.exists() and dest.stat().st_size:
        return True
    part, ref = dest.with_name(dest.name + ".part"), dest.with_name(dest.name + ".part.ref")
    total0, done0 = e["bytes_total"], e["bytes_done"]
    for attempt in range(tries):
        have = part.stat().st_size if part.exists() and ref.exists() else 0
        headers = {"Range": f"bytes={have}-", "If-Range": ref.read_text("utf-8"), "Accept-Encoding": "identity"} if have else {}
        try:
            with client.stream("GET", url, headers=headers) as r:
                if r.status_code in _ABSENT and not required:
                    return False
                if r.status_code == 416:  # the .part does not fit the file any more
                    part.unlink(missing_ok=True)
                    continue
                r.raise_for_status()
                m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", r.headers.get("content-range", "").strip())
                if r.status_code == 206 and not (m and int(m[1]) == have and int(m[2]) + 1 == int(m[3])):
                    part.unlink(missing_ok=True)  # not the rest of this file up to its end: ask for all of it
                    ref.unlink(missing_ok=True)
                    continue
                if r.status_code != 206:
                    have = 0  # a whole file: the server ignored the range, or the file changed
                    tag = r.headers.get("etag", "")
                    validator = tag if tag and not tag.startswith("W/") else r.headers.get("last-modified", "")
                    ref.write_text(validator, "utf-8") if validator else ref.unlink(missing_ok=True)
                html = "text/html" in r.headers.get("content-type", "")
                e["bytes_total"] = total0 + have + int(r.headers.get("content-length") or 0)
                with part.open("ab" if have else "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        # a login or error page instead of the file (a PDS4 label mislabelled text/html, with its
                        # <title>, still passes)
                        if html and re.search(rb"<(!doctype\s+html|html|head|body)\b", chunk[:2048].lower()):
                            raise httpx.DecodingError(f"{url.split('?')[0]} returned a web page (login, error or landing "
                                                      "page) instead of the file. NASA Earthdata downloads need a token in Settings.")
                        html = False
                        f.write(chunk)
                        e["bytes_done"] = done0 + have + r.num_bytes_downloaded  # wire bytes, like Content-Length
                        e["progress"] = min(0.99, e["bytes_done"] / e["bytes_total"]) if e["bytes_total"] else 0.0
            part.replace(dest)
            ref.unlink(missing_ok=True)
            return True
        except httpx.HTTPError as ex:
            e["bytes_total"], e["bytes_done"] = total0, done0  # a retry counts again; a skipped file not at all
            status = ex.response.status_code if isinstance(ex, httpx.HTTPStatusError) else None
            transient = isinstance(ex, httpx.TransportError) or status in _RETRY
            if transient and attempt < tries - 1:
                time.sleep(_BACKOFF * 2 ** attempt)
                continue
            if not transient:  # nothing to resume
                part.unlink(missing_ok=True)
                ref.unlink(missing_ok=True)
            if required or transient:
                raise
            return False
    raise httpx.HTTPError(f"{url.split('?')[0]}: the server keeps rejecting the download range")


def _siblings(client: httpx.Client, url: str, ddir: Path, cands: list[tuple[str, str]]) -> None:
    """Optional files next to url, fetched in parallel (on PDS hosts every miss costs a redirect and a new
    connection): cands = [(remote name, local name)] in order of preference; the first found per local name is kept."""
    cands = [(r, n) for r, n in cands if not (ddir / n).exists()]
    tmp = [ddir / f".sibling{k}" for k in range(len(cands))]  # X.LBL and X.lbl are one file on Windows
    junk = [f for t in tmp for f in (t, t.with_name(t.name + ".part"), t.with_name(t.name + ".part.ref"))]
    for f in junk:
        f.unlink(missing_ok=True)
    if not cands:
        return
    try:
        with ThreadPoolExecutor(len(cands)) as ex:  # httpx.Client is thread-safe
            got = list(ex.map(lambda k: _download(client, sibling_url(url, cands[k][0]), tmp[k],
                                                  {"bytes_total": 0, "bytes_done": 0}, False), range(len(cands))))
        for (_, name), t, ok in zip(cands, tmp, got):
            if ok and not (ddir / name).exists():
                t.replace(ddir / name)
    finally:
        for f in junk:
            f.unlink(missing_ok=True)


def _has_pds3_label(path: Path) -> bool:
    """A PDS3 label at the start of the file (CTX and many EDRs): no sibling label or ENVI header to look for. VICAR
    files do not count: archives often put a detached PDS label next to them (Cassini ISS)."""
    with path.open("rb") as f:
        return f.read(64).lstrip().startswith((b"PDS_VERSION_ID", b"ODL_VERSION_ID", b"CCSD3ZF", b"NJPL1I00PDS"))


def pds3_pointers(label: str) -> list[str]:
    """Data file names referenced by ^POINTER lines: ^IMAGE = "F.IMG", ^QUBE = ("F.QUB", 3), ..."""
    names = []
    for m in re.finditer(r'^\s*\^([A-Z_0-9]+)\s*=\s*(.*)$', label, re.M):
        q = re.search(r'"([^"]+)"', m.group(2))
        if q and PDS3_DATA_POINTER.search(m.group(1)) and q.group(1) not in names:
            names.append(q.group(1))
    return names


def pds4_files(xml: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"<(?:\w+:)?file_name>\s*([^<\s][^<]*?)\s*</", xml)))


def _fetch(e: dict) -> tuple[str, str | None]:
    """Returns (path GDAL opens, stream url or None)."""
    url, mode = e["source"]["url"], e["_mode"]
    name = _file_name(url)
    m = _atlas_query(url)
    if m and m.group(4).upper() != "RAW" and not name.lower().endswith((".lbl", ".xml")):
        url = sibling_url(url, name)  # the "label" of an attached-label product is the product itself
    ext = Path(name).suffix.lower()
    if mode != "download" and (ext in (".tif", ".tiff") or mode == "stream"):
        try:
            with render.open_raw("/vsicurl/" + sign_url(url)) as ds:
                if mode == "stream" or _is_tiled(ds):
                    return "", url
        except RasterioIOError:
            if mode == "stream":
                raise
    ddir = config.CACHE_DIR / "downloads" / e["id"][2:]
    ddir.mkdir(parents=True, exist_ok=True)
    e["_dir"] = str(ddir)
    e.update(status="downloading", message="Downloading")
    with _client() as c:
        main = ddir / name
        _download(c, sign_url(url), main, e, True)
        opens = main
        if ext == ".lbl":
            text = main.read_text("latin-1")
            for ptr in map(_safe_name, pds3_pointers(text)):  # names from a remote label: never a path
                # HiRISE labels point at an uncompressed .IMG that only exists as the .JP2 named in FILE_NAME
                alts = [(v, ptr) for v in (ptr, ptr.upper(), ptr.lower())] \
                    + [(f, f) for f in map(_safe_name, re.findall(r'FILE_NAME\s*=\s*"([^"]+)"', text))]
                if not any(_download(c, sibling_url(url, v), ddir / dest, e, False) for v, dest in alts):
                    raise FileNotFoundError(f"label points to {ptr}, which could not be downloaded")
        elif ext == ".xml":
            for f in map(_safe_name, pds4_files(main.read_text("utf-8", "replace"))):
                if not _download(c, sibling_url(url, f), ddir / f, e, False):
                    raise FileNotFoundError(f"PDS4 label lists {f}, which could not be downloaded")
        bare = ext in (".img", ".qub", ".imq", ".vic", ".dat", ".raw", ".bil", ".bip", ".bsq") and not _has_pds3_label(main)
        cands = []
        if bare and ext in (".img", ".qub", ".imq", ".vic"):  # a detached PDS3/PDS4 label
            cands += [(n, n) for n in dict.fromkeys(Path(name).with_suffix(s).name for s in (".LBL", ".lbl", ".xml"))]
        if bare and ext in (".img", ".dat", ".raw", ".bil", ".bip", ".bsq"):  # ENVI header (M3, CRISM, ...): GDAL cannot
            # read the raw cube without it; saved lower-case: with directory listing disabled GDAL only probes name.hdr
            cands += [(n, Path(name).with_suffix(".hdr").name) for n in dict.fromkeys(Path(name).with_suffix(s).name for s in (".HDR", ".hdr"))]
        _siblings(c, url, ddir, cands)
        if bare and ext in (".img", ".qub", ".imq", ".vic"):
            opens = _detached_label(main) or main
        for extra in e["_companions"]:
            _download(c, sign_url(extra), ddir / _file_name(extra), e, False)
    return str(opens), None


def _detached_label(data: Path) -> Path | None:
    """The PDS3 .LBL next to a bare data file, if it points at that file."""
    if data.suffix.lower() not in (".img", ".qub", ".imq", ".vic", ".dat"):
        return None
    for ext in (".LBL", ".lbl"):
        lbl = data.with_suffix(ext)
        if lbl.is_file() and any(n.lower() == data.name.lower() for n in pds3_pointers(lbl.read_text("latin-1"))):
            return lbl
    for ext in (".xml", ".XML"):  # PDS4 label naming this file
        xml = data.with_suffix(ext)
        if xml.is_file() and any(n.lower() == data.name.lower() for n in pds4_files(xml.read_text("utf-8", "replace"))):
            return xml
    return None


# ---------------------------------------------------------------- describing (GDAL + fallbacks)

def _is_tiled(ds) -> bool:
    return bool(ds.profile.get("tiled")) or bool(ds.count and ds.overviews(1))


_BODIES: tuple[float, list] = (0.0, [])


def _body_list() -> list[dict]:
    global _BODIES
    f = config.CATALOG_DIR / "bodies.json"
    try:
        mt = f.stat().st_mtime
        if mt != _BODIES[0]:
            _BODIES = (mt, json.loads(f.read_text("utf-8")).get("bodies", []))
    except (OSError, ValueError):
        pass
    return _BODIES[1]


def detect_body(pc) -> str | None:
    """Body id from datum/ellipsoid names, else from the semi-major axis (+-0.5 %)."""
    bodies = _body_list()
    names = " ".join(str(n).lower() for n in (pc.name, getattr(pc.geodetic_crs, "name", ""),
                                               getattr(pc.datum, "name", ""), getattr(pc.ellipsoid, "name", "")))
    for b in sorted(bodies, key=lambda b: -len(b.get("name", ""))):
        for token in {b.get("id", ""), b.get("name", "").lower()} - {""}:
            if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", names):
                return b["id"]
    a = pc.ellipsoid.semi_major_metre if pc.ellipsoid else None
    best = None
    for b in bodies:
        refs = [r for r in (max(b.get("radii_m") or [0]), b.get("mean_radius_m")) if r]
        if a and refs:
            d = min(abs(a - r) / r for r in refs)
            if d <= 0.005 and (best is None or d < best[0]):
                best = (d, b["id"])
    return best[1] if best else None


def _crs_name(pc, body: str | None) -> str:
    """The CRS's own name when it has one, else "<projection> · <body>" (custom planetary CRSs are unnamed)."""
    if pc.name and not re.match(r"(?i)(unknown|unnamed|undefined|custom)", pc.name):
        return pc.name
    method = re.sub(r"\s*\(.*", "", pc.coordinate_operation.method_name) if pc.coordinate_operation else ""
    what = "Geographic" if pc.is_geographic else method.replace("Equidistant Cylindrical", "Equirectangular") or "Projected"
    b = next((x["name"] for x in _body_list() if x["id"] == body), None)
    return f"{what} · {b}" if b else what


def _crs_info(crs) -> dict:
    from pyproj import CRS as PCRS
    pc = PCRS.from_wkt(crs.to_wkt())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        proj4 = pc.to_proj4() or crs.to_proj4()
    unit = pc.axis_info[0].unit_name if pc.axis_info else ""
    e = pc.ellipsoid
    body = detect_body(pc)
    return {"wkt": crs.to_wkt(), "proj4": proj4, "name": _crs_name(pc, body), "is_geographic": bool(pc.is_geographic),
            "units": "degree" if pc.is_geographic else ("m" if unit in ("metre", "meter") else unit),
            "body": body, "a": e.semi_major_metre if e else None, "b": e.semi_minor_metre if e else None}


def _wavelength(tags: dict) -> float | None:
    for k, v in tags.items():
        if k.lower() in ("wavelength", "band_wavelength", "center_wavelength", "wavelength_um", "central_wavelength"):
            m = re.match(r"\s*([0-9.]+)\s*(nm|um|µm|micron)?", str(v))
            if m:
                x = float(m.group(1))
                return x * 1000 if (m.group(2) or "").startswith(("um", "µ", "micron")) or (not m.group(2) and x < 50) else x
    return None


def read_label(driver: str, files: list[str]) -> str | None:
    for f in files:
        if f.lower().endswith((".lbl", ".xml", ".hdr")):  # .hdr: the ENVI header
            return Path(f).read_text("utf-8", "replace")[:2_000_000]
    if not files or driver not in ("PDS", "ISIS2", "ISIS3", "VICAR"):
        return None
    with open(files[0], "rb") as fh:
        text = fh.read(1 << 20).decode("latin-1")
    m = re.match(r"LBLSIZE=(\d+)", text)
    if m:
        return text[: int(m.group(1))]
    end = re.search(r"^\s*END\s*$", text, re.M | re.I)
    return text[: end.end()] if end else None


def describe(path: str, extra_files: list[str] | None = None) -> dict:
    """DatasetInfo (CONTRACTS.md section 5) of any GDAL-openable path."""
    ds = render.open_ds(path)
    mode = render.georef(ds) if ds.count else "none"
    crs = _crs_info(ds.crs) if ds.crs and mode == "transform" else None
    t = ds.transform
    tags = {"": ds.tags()}
    for ns in ds.tag_namespaces():
        if ns not in ("IMAGE_STRUCTURE", "DERIVED_SUBDATASETS", "json:ISIS3", "SUBDATASETS"):
            tags[ns] = ds.tags(ns=ns)
    tags = {ns: {k: str(v)[:2000] for k, v in d.items()} for ns, d in tags.items() if d}
    files = [f for f in (ds.files or []) if "://" not in f and not f.startswith("/vsi")] or (extra_files or [])
    label = read_label(ds.driver, files)
    if not label and "json:ISIS3" in ds.tag_namespaces():
        label = "\n".join(ds.tags(ns="json:ISIS3").values())
    sub_desc = ds.tags(ns="SUBDATASETS")
    subs = [{"name": n, "description": sub_desc.get(f"SUBDATASET_{i + 1}_DESC", "")}
            for i, n in enumerate(ds.subdatasets)]
    tiles = None
    if ds.count:
        tiles = {"tile_size": render.TILE, "max_zoom": render.max_zoom(ds.width, ds.height),
                 "overzoom": render.OVERZOOM}
    bands = range(1, ds.count + 1)
    return {
        "driver": ds.driver, "width": ds.width, "height": ds.height, "count": ds.count,
        "dtype": ds.dtypes[0] if ds.count else None, "dtypes": list(ds.dtypes),
        # NaN/inf are not JSON: shown as "nan"/"inf"/"-inf"
        "nodata": [v if v is None or math.isfinite(v) else str(v) for v in ds.nodatavals],
        "scales": list(ds.scales), "offsets": list(ds.offsets),
        "units": [u or "" for u in ds.units], "band_names": [ds.descriptions[b - 1] or f"Band {b}" for b in bands],
        "colorinterp": [c.name.lower() for c in ds.colorinterp] if ds.count else [],
        "wavelengths": [_wavelength(ds.tags(b)) for b in bands],
        "crs": crs, "transform": list(t)[:6] if mode == "transform" else None,
        "bounds": list(ds.bounds) if mode == "transform" else None,
        "lonlat_bounds": render.lonlat_bounds(ds) if ds.count else None,
        "georef": mode, "resolution": [abs(t.a), abs(t.e)] if mode == "transform" else None,
        "tiles": tiles, "overviews": list(ds.overviews(1)) if ds.count else [],
        "subdatasets": subs, "metadata": tags, "label": label, "files": files, "converted": None,
    }


def _describe_any(e: dict, serve: str, stream: str | None) -> dict:
    """GDAL first; on failure pdr -> astropy -> pyhdf. Sets e['_serve'] / e['_stream']."""
    if stream:
        e["_stream"] = stream
        info = describe(serve_path(e))
        info["files"] = []
        return info
    e["_serve"] = serve
    base = serve
    if serve.startswith("HDF4PY:"):
        return _convert_hdf4_sds(e, serve)
    try:
        info = describe(serve)
        if info["count"] or info["subdatasets"]:
            return _fix_pds_label(e, serve, info)
        err = "no raster bands"
    except RasterioIOError as ex:
        err = str(ex)
    return _fallback(e, base, err)


def _label_number(block: str, *keys: str) -> float | None:
    m = re.search(rf"^\s*(?:{{}})\s*=\s*(-?[0-9][0-9.eE+-]*)".format("|".join(keys)), block, re.M)
    return float(m.group(1)) if m else None


def _fix_pds_label(e: dict, serve: str, info: dict) -> dict:
    """What GDAL's PDS driver misses, served through a VRT when the label says so:
    * georeferencing: it reads LINE/SAMPLE_PROJECTION_OFFSET as 0-based and uses the rounded MAP_SCALE, but MOLA,
      MESSENGER, ... labels are 1-based, so maps land ~1 px north-west. The label's edge coordinates
      (WESTERNMOST_LONGITUDE ...) are exact: used when they imply a transform within 1.5 px of GDAL's.
    * NoData: CORE_NULL / MISSING_CONSTANT; scale/offset: SCALING_FACTOR / OFFSET of the IMAGE object (HiRISE)."""
    label = info["label"] or ""
    if info["driver"] != "PDS" or not info["count"]:
        return info
    gt = None
    crs = info["crs"]
    ext = [_label_number(label, k) for k in ("WESTERNMOST_LONGITUDE", "EASTERNMOST_LONGITUDE", "MAXIMUM_LATITUDE", "MINIMUM_LATITUDE")]
    if crs and "+proj=eqc" in crs["proj4"] and None not in ext and re.search(r'POSITIVE_LONGITUDE_DIRECTION\s*=\s*"?EAST', label):
        w, e_, n, s = ext
        tr = render.transformer(render.geodetic_wkt_of(rasterio.crs.CRS.from_wkt(crs["wkt"])), crs["wkt"])
        lon0 = float(re.search(r"\+lon_0=(\S+)", crs["proj4"]).group(1))
        x0 = tr.transform(lon0, 0)[0]
        k = tr.transform(lon0 + 1, 0)[0] - x0  # metres per degree of longitude
        left = x0 + (w - lon0) * k
        top, bottom = tr.transform(lon0, n)[1], tr.transform(lon0, s)[1]
        a, ee = (e_ - w) * k / info["width"], (top - bottom) / info["height"]
        t = info["transform"]
        shift = max(abs(left - info["bounds"][0]) / a, abs(top - info["bounds"][3]) / ee)
        if 0.01 < shift <= 1.5 and abs(a / t[0] - 1) < 2e-3 and abs(ee / abs(t[4]) - 1) < 2e-3:
            gt = (left, a, 0, top, 0, -ee)
    img = re.search(r"OBJECT\s*=\s*IMAGE\s*$(.*?)^\s*END_OBJECT\s*=\s*IMAGE\s*$", label, re.M | re.S | re.I)
    block = img.group(1) if img else label
    nodata = _label_number(block, "CORE_NULL", "MISSING_CONSTANT", "NULL_CONSTANT") if all(v is None for v in info["nodata"]) else None
    so = None
    if all(v == 1 for v in info["scales"]) and not any(info["offsets"]):
        sc, off = _label_number(block, "SCALING_FACTOR"), _label_number(block, "OFFSET")
        if sc is not None and off is not None and (sc, off) != (1, 0):
            so = (sc, off)
    if nodata is not None and info["width"] * info["height"] > OPTIMIZE_MIN_PIXELS and _is_tiled(render.open_ds(serve)):
        # A VRT NoData sends GDAL's averaged reads of big JP2/tiled files to full resolution (HiRISE: 13x slower at
        # 1/8, 20-80 s at 1/64): our own reads apply it instead (render nodata=). Scale/offset in a VRT cost nothing.
        e["_nodata"], nodata = nodata, None
    fixed = info
    if gt is not None or nodata is not None or so is not None:
        vrt = _dir_for(e) / "georef.vrt"
        rasterio.shutil.copy(serve, vrt, driver="VRT")
        tree = ET.parse(vrt)
        if gt:
            tree.getroot().find("GeoTransform").text = ", ".join(map(repr, gt))
        for band in tree.getroot().iter("VRTRasterBand"):
            for tag, val in (("NoDataValue", nodata), ("Scale", so and so[0]), ("Offset", so and so[1])):
                if val is not None:
                    el = band.find(tag)
                    (el if el is not None else ET.SubElement(band, tag)).text = repr(float(val))
        tree.write(vrt)
        e["_serve"] = str(vrt)
        fixed = describe(str(vrt))
        fixed.update(files=info["files"], label=info["label"], driver=info["driver"])
    if e.get("_nodata") is not None:
        fixed["nodata"] = [e["_nodata"]] * fixed["count"]
    return fixed


def _fallback(e: dict, path: str, gdal_err: str) -> dict:
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head.startswith(b"SIMPLE") or Path(path).suffix.lower() in (".fits", ".fit", ".fts"):
        steps = [_fits_to_array]
    elif head[:4] == b"\x0e\x03\x13\x01":
        return _list_hdf4(e, path)
    else:
        steps = [_pdr_to_array]
    reasons = []
    for step in steps:
        try:
            e.update(status="converting", message=f"Converting with {step.__name__[1:].split('_')[0]}")
            kind, arr, kw, label = step(path)
            out = _dir_for(e) / "converted.tif"
            write_cog(out, arr, **kw)
            e["_serve"] = str(out)
            info = describe(str(out), [path])
            info.update(label=label or read_label("PDS", [path]), files=[path], driver=_FORMAT[kind],
                        converted={"from": kind, "reason": gdal_err[:300]})
            info["band_names"] = kw.get("descriptions") or info["band_names"]
            return info
        except Exception as ex:  # try the next reader
            reasons.append(f"{step.__name__}: {ex}")
    raise RasterioIOError(f"No reader could open this file. GDAL: {gdal_err[:200]}; " + "; ".join(reasons))


_FORMAT = {"pdr": "PDS", "astropy": "FITS"}


def _dir_for(e: dict) -> Path:
    d = config.CACHE_DIR / "downloads" / e["id"][2:]
    d.mkdir(parents=True, exist_ok=True)
    e.setdefault("_dir", str(d))
    return d


def _native(a: np.ndarray) -> np.ndarray:
    return a.astype(a.dtype.newbyteorder("="), copy=False)


def _as_bands(a: np.ndarray) -> np.ndarray:
    a = _native(np.asarray(a))
    if a.ndim == 2:
        return a[None]
    return a[(0,) * (a.ndim - 3)] if a.ndim > 3 else a


def _pdr_to_array(path: str):
    import pdr
    data = pdr.read(path)
    best = None
    for key in data.keys():
        try:
            v = data[key]
        except Exception:
            continue
        if isinstance(v, np.ndarray) and v.ndim in (2, 3) and v.dtype.kind in "uifb" and (best is None or v.size > best[1].size):
            best = (key, v)
    if best is None:
        raise ValueError("no image array found")
    arr = _as_bands(best[1])
    return "pdr", arr, {}, None


def _fits_to_array(path: str):
    from astropy.io import fits
    # raw (unscaled) values, so BSCALE/BZERO/BLANK become scale/offset/nodata instead of a float copy of a big image
    with fits.open(path, memmap=True, do_not_scale_image_data=True) as hdul:
        for hdu in hdul:
            if getattr(hdu, "data", None) is not None and hdu.data.ndim >= 2 and hdu.data.dtype.kind in "uif":
                arr = _as_bands(hdu.data)[:, ::-1]  # FITS row 0 is the bottom of the image
                arr = np.array(arr) if arr.dtype.kind == "f" else arr
                h = hdu.header
                kw = {}
                if "BSCALE" in h or "BZERO" in h:
                    kw["scales"], kw["offsets"] = float(h.get("BSCALE", 1)), float(h.get("BZERO", 0))
                if "BLANK" in h and arr.dtype.kind in "ui" and np.iinfo(arr.dtype).min <= h["BLANK"] <= np.iinfo(arr.dtype).max:
                    kw["nodata"] = int(h["BLANK"])
                return "astropy", arr, kw, h.tostring(sep=chr(10))
    raise ValueError("no image HDU with 2+ dimensions")


def _list_hdf4(e: dict, path: str) -> dict:
    from pyhdf.SD import SD
    sd = SD(path)
    subs = [{"name": f'HDF4PY:"{path}":{n}', "description": f"{n} {list(shape)}"}
            for n, (dims, shape, typ, idx) in sd.datasets().items() if len(shape) >= 2]
    if not subs:
        raise ValueError("HDF4 file has no 2-D+ datasets")
    return _empty_info("HDF4", subs, "\n".join(f"{k} = {v}" for k, v in sd.attributes().items()), [path])


def _empty_info(driver: str, subs: list, label: str | None, files: list) -> dict:
    return {"driver": driver, "width": 0, "height": 0, "count": 0, "dtype": None, "dtypes": [], "nodata": [],
            "scales": [], "offsets": [], "units": [], "band_names": [], "colorinterp": [], "wavelengths": [],
            "crs": None, "transform": None, "bounds": None, "lonlat_bounds": None, "georef": "none",
            "resolution": None, "tiles": None, "overviews": [], "subdatasets": subs, "metadata": {},
            "label": label, "files": files, "converted": None}


def _convert_hdf4_sds(e: dict, spec: str) -> dict:
    from pyhdf.SD import SD
    m = re.match(r'HDF4PY:"(.+)":([^:]+)$', spec)
    path, name = m.group(1), m.group(2)
    sds = SD(path).select(name)
    arr = np.asarray(sds.get())
    if arr.ndim == 3 and arr.shape[2] < arr.shape[0]:
        arr = np.moveaxis(arr, 2, 0)  # (rows, cols, bands) -> bands first
    attrs = sds.attributes()
    kw = {}
    if "_FillValue" in attrs:
        kw["nodata"] = float(attrs["_FillValue"])
    if "scale_factor" in attrs:
        kw["scales"], kw["offsets"] = float(attrs["scale_factor"]), float(attrs.get("add_offset", 0))
    out = _dir_for(e) / "converted.tif"
    write_cog(out, _as_bands(arr), **kw)
    e["_serve"] = str(out)
    info = describe(str(out), [path])
    info.update(label="\n".join(f"{k} = {v}" for k, v in attrs.items()), files=[path],
                driver="HDF4", converted={"from": "pyhdf", "reason": "GDAL has no HDF4 driver"})
    return info


def write_cog(dst: Path, arr: np.ndarray, nodata=None, scales=None, offsets=None, descriptions=None) -> None:
    """Lossless COG from an in-memory/memmapped [bands, rows, cols] array (written in row chunks)."""
    n, h, w = arr.shape
    tmp = dst.with_suffix(".tmp.tif")
    prof = dict(driver="GTiff", width=w, height=h, count=n, dtype=arr.dtype.name, tiled=True, blockxsize=512,
                blockysize=512, BIGTIFF="IF_SAFER")
    if nodata is not None:
        prof["nodata"] = nodata
    with rasterio.open(tmp, "w", **prof) as out:
        step = max(512, (64_000_000 // max(w * n * arr.dtype.itemsize, 1)) // 512 * 512)
        for r in range(0, h, step):
            win = rasterio.windows.Window(0, r, w, min(step, h - r))
            out.write(np.asarray(arr[:, r:r + win.height]), window=win)
        if scales is not None:
            out.scales, out.offsets = [scales] * n, [offsets or 0.0] * n
    try:
        rasterio.shutil.copy(tmp, dst, driver="COG", **COG_OPTS)
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- COG optimisation

def _wants_optimizing(e: dict, info: dict) -> bool:
    if e.get("_stream") or not info["count"] or info["converted"] or info["width"] * info["height"] <= OPTIMIZE_MIN_PIXELS:
        return False
    return not _is_tiled(render.open_ds(e["_serve"]))


def _optimize_job(ds_id: str) -> None:
    e = REG.get(ds_id)
    if e is None:
        return
    out = _dir_for(e) / "optimized.tif"
    tmp = out.with_name("optimized.part.tif")
    try:
        rasterio.shutil.copy(e["_serve"], tmp, driver="COG", **COG_OPTS)
        tmp.replace(out)
        new = describe(str(out))
        old = e["info"]
        for k in ("driver", "crs", "label", "metadata", "files", "band_names", "units", "wavelengths", "colorinterp"):
            new[k] = old[k]
        e["_serve"], e["info"], e["message"] = str(out), new, "Ready"
    except Exception as ex:  # keep serving the original
        tmp.unlink(missing_ok=True)
        e["message"] = f"Ready (optimization failed: {ex})"[:300]
    e["optimizing"] = False
    save()


# ---------------------------------------------------------------- upload and file browser

def save_upload(files: list[tuple[str, object]]) -> dict:
    """files: (filename, binary file object). Saves under cache/uploads/<id>/ and opens the primary file."""
    if not files:
        raise ValueError("no files")
    d = config.CACHE_DIR / "uploads" / uuid.uuid4().hex[:10]
    d.mkdir(parents=True)
    saved = []
    for name, fh in files:
        dest = d / _file_name(name)
        with dest.open("wb") as out:
            shutil.copyfileobj(fh, out)
        saved.append(dest)
    primary = next((p for ext in (".lbl", ".xml") for p in saved if p.suffix.lower() == ext), None) \
        or next((p for p in saved if is_openable(p.name)), saved[0])
    e = open_dataset(path=str(primary), name=primary.name)
    with LOCK:
        REG[e["id"]]["_dir"] = str(d)
    return e


def _hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attrs & 0x2 or attrs & 0x4)  # HIDDEN | SYSTEM


def list_dir(path: str) -> dict:
    drives = [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")] if os.name == "nt" else ["/"]
    if not path:
        home = str(Path.home())
        ents = [{"name": "Home", "path": home, "is_dir": True, "size": None, "openable": False}]
        ents += [{"name": d, "path": d, "is_dir": True, "size": None, "openable": False} for d in drives]
        return {"path": "", "parent": None, "drives": drives, "entries": ents}
    p = Path(path).resolve()
    if not p.is_dir():
        raise FileNotFoundError(path)
    ents = []
    with os.scandir(p) as it:
        for en in it:
            try:
                if _hidden(en):
                    continue
                is_dir = en.is_dir()
                ents.append({"name": en.name, "path": str(p / en.name), "is_dir": is_dir,
                             "size": None if is_dir else en.stat().st_size,
                             "openable": not is_dir and is_openable(en.name, "label" if en.name.lower().endswith(".xml") else "data")})
            except OSError:
                continue
    ents.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "drives": drives, "entries": ents}
