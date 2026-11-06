"""Reading, tiling, warping, statistics, probing and PNG rendering of GDAL-readable rasters.

Everything here is blocking (GDAL); callers run it in worker threads. Functions take a path that
GDAL can open (local file, /vsicurl/ URL, subdataset string).
"""
import contextlib
import functools
import json
import math
import os
import threading
import re
import warnings
from collections import OrderedDict
from functools import lru_cache

for _k, _v in {
    # No VSI_CACHE: it gives every thread's handle its own 64 MB cache, local files included (6 parallel tile reads were
    # 3.7x slower, +430 MB). Remote reads share curl's region cache instead, sized for COGs with 1024 px blocks (the
    # 16 MB default refetched revisited Copernicus DEM blocks: 1.75-3.5 s instead of 0.2 s).
    "CPL_VSIL_CURL_CACHE_SIZE": str(128 << 20),
    "GDAL_HTTP_MULTIRANGE": "YES", "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES", "GDAL_CACHEMAX": "512",
    "GDAL_HTTP_TIMEOUT": "30", "GDAL_HTTP_CONNECTTIMEOUT": "15", "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
}.items():
    os.environ.setdefault(_k, _v)

import numpy as np  # noqa: E402  (GDAL env must be set before rasterio initialises GDAL)
import rasterio  # noqa: E402
import rasterio.shutil  # noqa: E402
from affine import Affine  # noqa: E402
from pyproj import CRS as PCRS, Transformer  # noqa: E402
from rasterio.crs import CRS  # noqa: E402
from rasterio.enums import Resampling  # noqa: E402
from rasterio.transform import from_bounds  # noqa: E402
from rasterio.warp import reproject, transform_bounds  # noqa: E402
from rasterio.windows import Window  # noqa: E402

from .. import config  # noqa: E402

TILE = 256
OVERZOOM = 4
DOWNSAMPLE = {"nearest", "bilinear", "cubic", "cubic_spline", "lanczos", "average", "mode", "rms"}
MAGNIFY = {"nearest", "bilinear", "cubic", "cubic_spline", "lanczos"}
PERCENTILES = [0.1, 0.5, 1, 2, 5, 95, 98, 99, 99.5, 99.9]

# ---------------------------------------------------------------- handles

_local = threading.local()
_caches: list[OrderedDict] = []  # every thread's handle cache, so close_all can reach them
MAX_HANDLES = 12
_shared: dict[str, tuple] = {}  # path -> (dataset, lock): JP2 decoding is multi-threaded and cache-hungry, so concurrent
_serial: set[str] = set()       # per-thread handles thrash each other (64 parallel tiles: 10x slower); one handle, one reader
_gate = threading.Condition()
_active = 0
_closing = False


def guarded(fn):
    """Public raster entry points run inside this gate so close_all can wait for them (no nesting)."""
    @functools.wraps(fn)
    def run(*a, **kw):
        global _active
        with _gate:
            while _closing:
                _gate.wait()
            _active += 1
        try:
            return fn(*a, **kw)
        finally:
            with _gate:
                _active -= 1
                _gate.notify_all()
    return run


def close_all() -> None:
    """Close every handle of every thread (Windows cannot delete a file that is still open): waits for running
    reads to finish and holds new ones off meanwhile."""
    global _closing
    with _gate:
        _closing = True
        while _active:
            _gate.wait()
        try:
            for cache in _caches:
                for ds in cache.values():
                    ds.close()
                cache.clear()
            for ds, _ in _shared.values():
                ds.close()
            _shared.clear()
        finally:
            _closing = False
            _gate.notify_all()


def open_raw(path: str):
    """rasterio.open; remote files skip the directory listing, local ones keep it (ENVI .hdr, aux files, ... are found through it)."""
    with rasterio.Env(**({"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR"} if path.startswith("/vsi") else {})):
        return rasterio.open(path)


def _io(ds):
    """Lock to hold while reading `ds` (a no-op for ordinary per-thread handles)."""
    return _shared.get(ds.name, (None, contextlib.nullcontext()))[1]


def _jp2(ds, depth: int = 0) -> bool:
    """JPEG 2000 underneath: directly, through a PDS label (HiRISE) or through a VRT over either (a VRT lists only
    its own sources, so look through the labels and VRTs among them)."""
    files = ds.files
    if ds.driver.startswith("JP2") or any(f.lower().endswith((".jp2", ".j2k")) for f in files):
        return True
    if ds.driver != "VRT" or depth > 2:
        return False
    for f in files:
        if f != ds.name and f.lower().endswith((".lbl", ".lab", ".xml", ".vrt")):  # a mosaic's TIFFs are not opened
            try:
                with open_raw(f) as src:
                    if _jp2(src, depth + 1):
                        return True
            except rasterio.errors.RasterioIOError:
                pass
    return False


def open_ds(path: str):
    """Per-thread bounded handle cache: rasterio datasets are not thread-safe."""
    if path in _serial:
        with _gate:
            if path not in _shared:
                _shared[path] = (open_raw(path), threading.Lock())
            return _shared[path][0]
    cache = getattr(_local, "cache", None)
    if cache is None:
        cache = _local.cache = OrderedDict()
        with _gate:
            _caches.append(cache)
    ds = cache.get(path)
    if ds is None:
        ds = open_raw(path)
        if _jp2(ds):
            ds.close()
            _serial.add(path)
            return open_ds(path)
        cache[path] = ds
        if len(cache) > MAX_HANDLES:
            cache.popitem(last=False)[1].close()
    else:
        cache.move_to_end(path)
    return ds


# ---------------------------------------------------------------- geometry helpers

def max_zoom(width: int, height: int) -> int:
    return max(0, math.ceil(math.log2(max(width, height) / TILE)))


def _rs(name: str, allowed: set[str], what: str) -> Resampling:
    if name not in allowed:
        raise ValueError(f"bad {what} {name!r}")
    return Resampling[name]


@lru_cache(64)
def _geodetic_wkt(wkt: str) -> str:
    pc = PCRS.from_wkt(wkt)
    g = pc.geodetic_crs
    if g is None:
        e = pc.ellipsoid
        g = PCRS.from_proj4(f"+proj=longlat +a={e.semi_major_metre} +b={e.semi_minor_metre} +no_defs")
    return g.to_wkt()


@lru_cache(64)
def _transformer(src: str, dst: str, thread: int) -> Transformer:  # Transformer is not thread-safe
    return Transformer.from_crs(PCRS.from_user_input(src), PCRS.from_user_input(dst), always_xy=True)


def transformer(src_wkt: str, dst_wkt: str) -> Transformer:
    return _transformer(src_wkt, dst_wkt, threading.get_ident())


def geodetic_wkt(ds) -> str:
    return _geodetic_wkt(ds.crs.to_wkt())


def georef(ds) -> str:
    if ds.crs and ds.transform != Affine.identity():
        return "transform"
    return "gcps" if ds.gcps[0] and ds.gcps[1] else "none"


_CYLINDRICAL = re.compile(r"\+proj=(eqc|merc|cea)")  # x depends on longitude only, so lon_0 is a constant x offset


def _lon_extent(ds) -> tuple[float, float] | None:
    """Raw (unwrapped) longitude extent when the dataset is geographic, e.g. (0, 360)."""
    if ds.crs and ds.crs.is_geographic and georef(ds) == "transform":
        return ds.bounds.left, ds.bounds.right
    return None


def geo_view(ds) -> tuple[CRS, Affine, float | None]:
    """(crs, transform, seam longitude or None) to warp and probe with. A 0..360 map in a cylindrical projection would be folded by PROJ
    into -180..180 and miss; moving lon_0 to the centre of the data (and shifting x to match) keeps the same
    geographic position of every pixel while making -180..180 requests land on it."""
    if georef(ds) != "transform" or ds.crs.is_geographic:
        return ds.crs, ds.transform, None
    pc = PCRS.from_wkt(ds.crs.to_wkt())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        proj = pc.to_proj4().replace("+type=crs", "").strip()
    if not _CYLINDRICAL.search(proj):
        return ds.crs, ds.transform, None
    west, _, east, _ = _projected_extent(ds)
    if west >= -180 and east <= 180:
        return ds.crs, ds.transform, None
    m = re.search(r"\+lon_0=(\S+)", proj)
    lon0 = float(m.group(1)) if m else 0.0
    centre = (west + east) / 2
    x0, _ = transformer(_geodetic_wkt(pc.to_wkt()), pc.to_wkt()).transform(lon0, 0)
    x1, _ = transformer(_geodetic_wkt(pc.to_wkt()), pc.to_wkt()).transform(lon0 + 10, 0)
    dx = (x1 - x0) / 10 * (centre - lon0)
    proj = re.sub(r"\+lon_0=\S+", "", proj) + f" +lon_0={centre}"
    t = ds.transform
    return CRS.from_string(proj), Affine(t.a, t.b, t.c - dx, t.d, t.e, t.f), (centre + 360) % 360 - 180


def _lon_shifts(ds, lonlat: bool, bbox) -> list[float]:
    """Longitude offsets under which a -180..180 request overlaps a 0..360 (or otherwise shifted) raster."""
    ext = _lon_extent(ds)
    if not lonlat or ext is None or (ext[0] >= -180 and ext[1] <= 180):
        return [0.0]
    return [s for s in (0.0, 360.0, -360.0) if bbox[0] + s < ext[1] and bbox[2] + s > ext[0]]


def lonlat_bounds(ds) -> list[float] | None:
    """[W, S, E, N] east-positive; W > E when crossing the antimeridian."""
    mode = georef(ds)
    if mode == "none":
        return None
    try:
        if mode == "gcps":
            xs, ys = zip(*((g.x, g.y) for g in ds.gcps[0]))
            t = transformer(ds.gcps[1].to_wkt(), geodetic_wkt_of(ds.gcps[1]))
            lon, lat = t.transform(xs, ys)
            w, s, e, n = min(lon), min(lat), max(lon), max(lat)
        elif ds.crs.is_geographic:
            w, s, e, n = ds.bounds
        else:
            w, s, e, n = _projected_extent(ds)
    except Exception:
        return None
    if not all(map(math.isfinite, (w, s, e, n))):
        return None
    if e - w >= 359.999:
        w, e = -180.0, 180.0
    else:
        span = e - w
        w = (w + 180) % 360 - 180
        e = w + span
        if e > 180:
            e -= 360
    return [w, max(s, -90.0), e, min(n, 90.0)]


def _projected_extent(ds) -> tuple[float, float, float, float]:
    """Geodetic extent by walking the outline; unwrapping keeps 0..360 and antimeridian-crossing rasters right
    (transform_bounds would fold both into -180..180)."""
    l, b, r, t = ds.bounds
    u = np.linspace(0, 1, 33)
    xs = np.concatenate([l + u * (r - l), np.full(33, r), r - u * (r - l), np.full(33, l)])
    ys = np.concatenate([np.full(33, t), t - u * (t - b), np.full(33, b), b + u * (t - b)])
    tr = transformer(ds.crs.to_wkt(), geodetic_wkt(ds))
    lon, lat = (np.asarray(v) for v in tr.transform(xs, ys))
    if not np.isfinite(lon).all() or not np.isfinite(lat).all():  # outline leaves the globe (geostationary full disk): use the interior
        gx, gy = np.meshgrid(np.linspace(l, r, 257), np.linspace(b, t, 257))
        lon, lat = (np.asarray(v).ravel() for v in tr.transform(gx.ravel(), gy.ravel()))
        ok = np.isfinite(lon) & np.isfinite(lat)
        lon, lat = lon[ok], lat[ok]
        if not lon.size:
            raise ValueError("no pixel falls on the globe")
        return lon.min(), lat.min(), lon.max(), lat.max()
    lon = np.unwrap(lon, period=360)
    return lon.min(), lat.min(), lon.max(), lat.max()


def geodetic_wkt_of(crs) -> str:
    return _geodetic_wkt(crs.to_wkt())


# ---------------------------------------------------------------- block reads

def _segments(length: float, scale: float) -> list[tuple[float, float, int, int]]:
    """(src_offset, src_len, out_offset, out_len): whole output pixels of `scale` source px, plus one
    trailing partial pixel, so output pixel i always covers source [i*scale, (i+1)*scale)."""
    k = int(length / scale + 1e-9)
    segs = [(0.0, k * scale, 0, k)] if k else []
    if length - k * scale > 1e-9:
        segs.append((k * scale, length - k * scale, k, 1))
    return segs


_KERNEL_RADIUS = {"nearest": 0, "bilinear": 1, "cubic": 2, "cubic_spline": 2, "lanczos": 3}  # source px
# Output px of no-data leaking into JPEG 2000 resolution levels. Measured on a HiRISE RDR at 1/2, 1/8 and 1/64: values
# within 3 px of an exact no-data pixel are off by up to 37 % from the full-resolution average of their valid pixels;
# from 4 px on they agree within the levels' usual 1-5 %. In the EDGE_BAND px along the image edge, where a no-data
# wedge thins to a point (swath corners), leaks reach further: EDGE_BLEED left none at 1/4, 1/16 and 1/64 (16: same;
# a 3 px band still let 2 through at 1/64).
BLEED = 3
EDGE_BAND = 4
EDGE_BLEED = 12


def _bleed(ds, scale: float, resampling: Resampling, nodata) -> int:
    """Output px next to masked pixels whose values may contain no-data, where GDAL cannot leave it out itself:
    reduced resolutions of JPEG 2000 (wavelet levels blend the edge) or of a file whose no-data GDAL does not know
    (`nodata`, from a PDS label), and interpolated zoom-in reads of the latter."""
    if scale > 1:
        return BLEED if nodata is not None or ds.name in _serial else 0
    if scale < 1 and nodata is not None:
        return math.ceil(_KERNEL_RADIUS.get(resampling.name, 0) / scale)
    return 0


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    """Binary dilation by a (2r+1) square, separable via running sums."""
    for ax in (0, 1):
        pad = [(r + 1, r) if a == ax else (0, 0) for a in (0, 1)]
        c = np.cumsum(np.pad(mask, pad), axis=ax, dtype=np.int32)
        n = c.shape[ax] - 2 * r - 1
        mask = np.take(c, np.arange(2 * r + 1, c.shape[ax]), axis=ax) - np.take(c, np.arange(n), axis=ax) > 0
    return mask


def _blends(masked: np.ndarray, r: int, edge: np.ndarray | None = None) -> np.ndarray:
    """Pixels to hide: within r px of masked ones, and within EDGE_BLEED px of them on `edge` (image-edge px)."""
    hide = _dilate(masked, r)
    if edge is not None and edge.any():
        hide |= edge & _dilate(masked, EDGE_BLEED)
    return hide


def _edge(rows: np.ndarray, cols: np.ndarray, nr: int, nc: int) -> np.ndarray:
    """Output px within EDGE_BAND of the image edge, for absolute output row/col indices."""
    b = EDGE_BAND
    return ((rows < b) | (rows >= nr - b))[:, None] | ((cols < b) | (cols >= nc - b))[None, :]


def read_block(ds, bands: list[int], col0: float, row0: float, width: float, height: float,
               scale: float, resampling: Resampling, nodata: float | None = None, out_dtype: str = "float32"):
    """Read source window (col0,row0,width,height) at `scale` source px per output px.
    Returns (data [nb,oh,ow] of out_dtype, alpha float32 [oh,ow] in 0..1). The window must lie inside the image.
    nodata: a no-data value GDAL does not know (PDS label): masked here, like GDAL's own."""
    if scale == 1:
        resampling = Resampling.nearest
    all_valid = all(set(f) == {rasterio.enums.MaskFlags.all_valid}
                    for f in (ds.mask_flag_enums[b - 1] for b in bands))
    r = _bleed(ds, scale, resampling, nodata) if nodata is not None or not all_valid else 0
    nr, nc = (math.ceil(n / scale - 1e-9) for n in (ds.height, ds.width))  # image size in output px
    edge = r and scale > 1 and (col0 / scale < EDGE_BAND or row0 / scale < EDGE_BAND
                                or (col0 + width) / scale > nc - EDGE_BAND or (row0 + height) / scale > nr - EDGE_BAND)
    m = max(r, EDGE_BLEED) if edge else r
    # m output px of margin (inside the image, on the same pixel grid) so the dilation below agrees across tile seams
    ml, mt = (min(m, int(v / scale + 1e-9)) for v in (col0, row0))
    # far side in source px, to the image edge at most: a trailing partial px is read as in a whole-image read
    er, eb = (max(0.0, min(m * scale, n - v - w)) for n, v, w in ((ds.width, col0, width), (ds.height, row0, height)))
    ow, oh = (sum(s[3] for s in _segments(v, scale)) for v in (width, height))
    data, alpha = _read(ds, bands, col0 - ml * scale, row0 - mt * scale, width + ml * scale + er,
                        height + mt * scale + eb, scale, resampling, nodata, all_valid, out_dtype)
    if r:
        rows = (row0 - mt * scale) / scale + np.arange(alpha.shape[0])
        cols = (col0 - ml * scale) / scale + np.arange(alpha.shape[1])
        alpha[_blends(alpha == 0, r, _edge(rows, cols, nr, nc) if edge else None)] = 0
        data, alpha = data[:, mt:mt + oh, ml:ml + ow], alpha[mt:mt + oh, ml:ml + ow]
    data[:, alpha == 0] = 0
    return np.ascontiguousarray(data), np.ascontiguousarray(alpha)


def _stored(nodata, dtype):
    """`nodata` as stored in `dtype`: a label value such as -9999.9 equals float32 data only after rounding."""
    dt = np.dtype(dtype)
    return dt.type(nodata) if nodata is not None and dt.kind == "f" else nodata


def _read(ds, bands, col0, row0, width, height, scale, resampling, nodata, all_valid, out_dtype="float32"):
    segs_x, segs_y = _segments(width, scale), _segments(height, scale)
    ow, oh = sum(s[3] for s in segs_x), sum(s[3] for s in segs_y)
    data = np.zeros((len(bands), oh, ow), out_dtype)
    alpha = np.ones((oh, ow), np.float32)
    with _io(ds):
        for sy, ly, oy, ny in segs_y:
            for sx, lx, ox, nx in segs_x:
                win = Window(col0 + sx, row0 + sy, lx, ly)
                sl = (slice(None), slice(oy, oy + ny), slice(ox, ox + nx))
                data[sl] = ds.read(bands, window=win, out_shape=(len(bands), ny, nx), resampling=resampling,
                                   out_dtype=out_dtype)
                if not all_valid:
                    m = ds.read_masks(bands, window=win, out_shape=(len(bands), ny, nx),
                                      resampling=Resampling.nearest).min(axis=0)
                    alpha[sl[1:]] = m / 255.0
    if nodata is not None:
        alpha[(data == _stored(nodata, data.dtype)).any(axis=0)] = 0
    if data.dtype.kind == "f":
        alpha[~np.isfinite(data).all(axis=0)] = 0
        data[~np.isfinite(data)] = 0
    return data, alpha


def pack(data: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """-> [h, w, nb+1] float32 pixel-interleaved."""
    return np.concatenate([data, alpha[None]], axis=0).transpose(1, 2, 0)


@guarded
def tile_f32(path: str, z: int, x: int, y: int, bands: list[int], resampling: str = "average",
             magnify: str = "nearest", nodata: float | None = None) -> np.ndarray:
    ds = open_ds(path)
    mz = max_zoom(ds.width, ds.height)
    if not 0 <= z <= mz + OVERZOOM:
        raise ValueError("zoom out of range")
    out = np.zeros((TILE, TILE, len(bands) + 1), np.float32)
    scale = 2.0 ** (mz - z)
    span = TILE * scale
    col0, row0 = x * span, y * span
    if x < 0 or y < 0 or col0 >= ds.width or row0 >= ds.height:
        return out
    rs = _rs(resampling if scale > 1 else magnify, DOWNSAMPLE if scale > 1 else MAGNIFY,
             "resampling" if scale > 1 else "magnify")
    vw, vh = min(span, ds.width - col0), min(span, ds.height - row0)
    data, alpha = read_block(ds, bands, col0, row0, vw, vh, scale, rs, nodata)
    h, w = alpha.shape
    out[:h, :w] = pack(data, alpha)
    return out


def _parse_crs(ds, crs: str) -> CRS:
    if crs == "native":
        return ds.crs
    if crs == "lonlat":
        return CRS.from_wkt(geodetic_wkt(ds))
    return CRS.from_user_input(crs)


@guarded
def warp_f32(path: str, crs: str, bbox: tuple, width: int, height: int, bands: list[int],
             resampling: str = "average", magnify: str = "nearest", nodata: float | None = None) -> np.ndarray:
    ds = open_ds(path)
    if georef(ds) == "none":
        raise LookupError("dataset has no georeferencing")
    dst_crs = _parse_crs(ds, crs)
    out = np.zeros((len(bands) + 1, height, width), np.float32)
    view = geo_view(ds)
    seam = view[2] if crs == "lonlat" else None  # lon where the re-centred map wraps: read each side separately
    for shift in _lon_shifts(ds, crs == "lonlat", bbox):
        b = (bbox[0] + shift, bbox[1], bbox[2] + shift, bbox[3])
        eps = 1e-7  # keeps each piece's edge off the seam, where the projection's longitude is ambiguous
        # a seam on a tile edge counts too (float noise puts it a hair to either side): an edge left on it makes
        # the source window span the whole map (coarse read) and leaves transparent columns along that edge
        pieces = [b] if seam is None or not b[0] - eps < seam < b[2] + eps else \
            [p for p in ((b[0], b[1], seam - eps, b[3]), (seam + eps, b[1], b[2], b[3])) if p[2] > p[0]]
        for piece in pieces:
            part = _warp_one(ds, bands, dst_crs, b, piece, width, height, resampling, magnify, view, nodata)
            if part is not None:
                better = part[-1] > out[-1]
                out[:, better] = part[:, better]
    out[:-1, out[-1] == 0] = 0
    return out.transpose(1, 2, 0)


def _warp_one(ds, bands, dst_crs, bbox, window_bbox, w, h, resampling, magnify, view, nodata=None):
    """Warp onto the grid of `bbox`, reading only the source covering `window_bbox` (a part of it)."""
    src_crs, t, _ = view
    dst_tf = from_bounds(*bbox, w, h)
    nb = len(bands)
    gcps = None
    north_up = georef(ds) == "transform" and t.b == 0 and t.d == 0 and t.a > 0 and t.e < 0
    if north_up:
        try:
            left, bottom, right, top = transform_bounds(dst_crs, src_crs, *window_bbox, densify_pts=21)
        except Exception:
            left = bottom = right = top = math.nan
        if not all(map(math.isfinite, (left, bottom, right, top))) or left > right:
            left, bottom, right, top = ds.bounds
        c0, c1 = (left - t.c) / t.a, (right - t.c) / t.a
        r0, r1 = (t.f - top) / -t.e, (t.f - bottom) / -t.e
        # source px per output px, before rounding to whole px
        ratio = min((c1 - c0) / (w * (window_bbox[2] - window_bbox[0]) / (bbox[2] - bbox[0])),
                    (r1 - r0) / (h * (window_bbox[3] - window_bbox[1]) / (bbox[3] - bbox[1])))
        c0, c1, r0, r1 = max(0, math.floor(c0)), min(ds.width, math.ceil(c1)), max(0, math.floor(r0)), min(ds.height, math.ceil(r1))
        if c1 <= c0 or r1 <= r0:
            return None
        scale = max(1, int(ratio + 1e-6))
        scale = max(scale, math.ceil((c1 - c0) / 8192), math.ceil((r1 - r0) / 8192))
        m = 2 * scale  # margin for the resampling kernel; window start snapped to the decimation grid
        c0, r0 = max(0, (c0 - m) // scale * scale), max(0, (r0 - m) // scale * scale)
        c1, r1 = min(ds.width, c1 + m), min(ds.height, r1 + m)
        src_tf = Affine(t.a * scale, 0, t.c + c0 * t.a, 0, t.e * scale, t.f + r0 * t.e)
        win = (c0, r0, c1 - c0, r1 - r0)
    else:  # rotated / GCP rasters: whole image, decimated; rare, so no windowing
        scale = max(1, math.ceil(max(ds.width, ds.height) / 2048))
        win = (0, 0, ds.width, ds.height)
        ratio = min(ds.width / w, ds.height / h)
        if georef(ds) == "transform":
            src_tf = t * Affine.scale(scale)
        else:
            from rasterio.control import GroundControlPoint
            src_tf = None
            gcps = [GroundControlPoint(g.row / scale, g.col / scale, g.x, g.y, g.z) for g in ds.gcps[0]]
            src_crs = ds.gcps[1]
    coarse = ratio > 1 + 1e-6
    kernel = _rs(resampling if coarse else magnify, DOWNSAMPLE if coarse else MAGNIFY, "resampling")
    data, alpha = read_block(ds, bands, *win, scale, kernel if scale > 1 else Resampling.nearest, nodata)
    src = np.concatenate([data, alpha[None]], axis=0)
    dst = np.zeros((nb + 1, h, w), np.float32)
    reproject(src, dst, src_transform=src_tf, gcps=gcps, src_crs=src_crs, dst_transform=dst_tf, dst_crs=dst_crs,
              resampling=kernel, src_alpha=nb + 1, dst_alpha=nb + 1)
    if north_up and dst_crs != src_crs:
        # Far from the raster GDAL's approximate transformer extrapolates edge pixels over whole regions (a world view of a
        # polar DEM paints a quadrant): drop everything that exact coordinates place outside the raster.
        x0, x1, y1, y0 = t.c - t.a, t.c + t.a * (ds.width + 1), t.f + t.e * (ds.height + 1), t.f - t.e
        if not (left >= x0 and right <= x1 and bottom >= y1 and top <= y0):
            gx, gy = np.meshgrid(bbox[0] + (np.arange(w) + .5) * (bbox[2] - bbox[0]) / w,
                                 bbox[3] - (np.arange(h) + .5) * (bbox[3] - bbox[1]) / h)
            sx, sy = (np.asarray(v).reshape(h, w) for v in transformer(dst_crs.to_wkt(), src_crs.to_wkt()).transform(gx.ravel(), gy.ravel()))
            dst[-1][~((sx >= x0) & (sx <= x1) & (sy >= y1) & (sy <= y0))] = 0  # NaN/inf compare False
    np.clip(dst[-1], 0, 1, out=dst[-1])
    return dst


# ---------------------------------------------------------------- statistics

def _summarise(values: np.ndarray, band: int, total: int, sample: tuple | None = None, hist=None) -> dict:
    """values: 1-D float64 of valid pixels. hist=(counts_fine, lo, hi) switches to histogram-derived quantiles."""
    qs = [100 * k / 32 for k in range(33)]
    base = {"band": band, "valid": int(values.size) if hist is None else int(hist[0].sum()), "total": total}
    if hist is None and values.size == 0 or hist is not None and hist[0].sum() == 0:
        zero = {"min": 0.0, "max": 0.0, "mean": 0.0, "std": 0.0}  # no valid pixels: valid == 0 says so
        return {**base, **zero, "percentiles": {f"{p:g}": 0.0 for p in PERCENTILES}, "quantiles": [0.0] * 33,
                "histogram": {"counts": [0] * 256, "min": 0.0, "max": 0.0}}
    if hist is None:
        lo, hi = float(values.min()), float(values.max())
        pc = np.percentile(values, PERCENTILES + qs)
        counts = np.histogram(values, 256, (lo, hi))[0]
        stats = {"min": lo, "max": hi, "mean": float(values.mean()), "std": float(values.std())}
    else:
        fine, lo, hi, stats = hist
        centers = np.linspace(lo, hi, fine.size + 1)
        cdf = np.concatenate([[0], np.cumsum(fine)]) / fine.sum()
        pc = np.interp(np.array(PERCENTILES + qs) / 100, cdf, centers)
        counts = fine.reshape(256, -1).sum(axis=1)
    return {**base, **stats, "percentiles": {f"{p:g}": float(v) for p, v in zip(PERCENTILES, pc)},
            "quantiles": [float(v) for v in pc[len(PERCENTILES):]],
            "histogram": {"counts": [int(c) for c in counts], "min": float(lo), "max": float(hi)}}


@guarded
def stats(path: str, bands: list[int], exact: bool = False, nodata: float | None = None) -> dict:
    return _stats(open_ds(path), bands, exact, nodata)


def _stats(ds, bands: list[int], exact: bool, nodata: float | None = None) -> dict:
    """Reads lock a shared JPEG 2000 handle one at a time, so tiles keep loading during a scan of minutes."""
    out = []
    if not exact:
        k = max(1, math.ceil(max(ds.width, ds.height) / 1024))
        oh, ow = math.ceil(ds.height / k), math.ceil(ds.width / k)
        # nearest keeps true values; sampling one pixel per k x k cell
        win = Window(0, 0, ds.width, ds.height)
        for b in bands:
            with _io(ds):
                v = ds.read(b, window=win, out_shape=(oh, ow), resampling=Resampling.nearest, out_dtype="float64")
                m = ds.read_masks(b, window=win, out_shape=(oh, ow), resampling=Resampling.nearest) > 0
            m &= np.isfinite(v)
            if nodata is not None:
                m &= v != _stored(nodata, ds.dtypes[b - 1])
            if k > 1 and _bleed(ds, k, Resampling.nearest, nodata):  # reduced-resolution blends at no-data edges
                m &= ~_blends(~m, BLEED, _edge(np.arange(oh), np.arange(ow), oh, ow))
            out.append(_summarise(v[m], b, oh * ow))
        return {"exact": False, "sample_shape": [oh, ow], "bands": out}
    total, rows = ds.width * ds.height, max(1, 4_000_000 // ds.width)
    # Big JPEG 2000 blocks one at a time (HiRISE: 1024 px, ~0.2 s under the lock; a strip decodes a row of 24 = 3.4 s).
    # Everything else, small JPEG 2000 blocks included (GDAL decodes a strip's blocks in parallel), in ~4 MP strips.
    wins = ([w for _, w in ds.block_windows(bands[0])] if ds.name in _serial and min(ds.block_shapes[bands[0] - 1]) >= 1024
            else [Window(0, r, ds.width, min(rows, ds.height - r)) for r in range(0, ds.height, rows)])

    def valid(win, b, dtype=None):
        with _io(ds):
            v = ds.read(b, window=win, out_dtype=dtype)
            ok = ds.read_masks(b, window=win) > 0
        if v.dtype.kind == "f":
            ok &= np.isfinite(v)
        nd = _stored(nodata, ds.dtypes[b - 1])
        return v[ok & (v != nd) if nd is not None else ok]

    dts = [np.dtype(ds.dtypes[b - 1]) for b in bands]
    if all(dt.kind in "iu" and dt.itemsize <= 2 for dt in dts):
        # 8/16-bit: one pass counting every value, all bands per window (each block is decoded once)
        offs = [int(np.iinfo(dt).min) for dt in dts]
        counts = [np.zeros(1 << 8 * dt.itemsize, np.int64) for dt in dts]
        for win in wins:
            for b, o, c in zip(bands, offs, counts):
                c += np.bincount(valid(win, b).astype(np.int64) - o, minlength=c.size)
        for b, o, c in zip(bands, offs, counts):
            vals = np.flatnonzero(c)
            w, vals = c[vals], vals.astype(np.float64) + o
            n = int(w.sum())
            if n == 0:
                out.append(_summarise(np.empty(0), b, total))
                continue
            lo, hi, mean = float(vals[0]), float(vals[-1]), float((vals * w).sum() / n)
            out.append(_summarise(np.empty(0), b, total, hist=(
                np.histogram(vals, 65536, (lo, hi), weights=w)[0], lo, hi,
                {"min": lo, "max": hi, "mean": mean, "std": math.sqrt(float((w * (vals - mean) ** 2).sum() / n))})))
        return {"exact": True, "sample_shape": [ds.height, ds.width], "bands": out}
    for b in bands:  # wider types: min/max/mean first, then the histogram and the spread around the mean
        n = s = 0.0
        lo, hi = math.inf, -math.inf
        for win in wins:
            v = valid(win, b, "float64")
            if v.size:
                n += v.size; s += v.sum()
                lo, hi = min(lo, v.min()), max(hi, v.max())
        if n == 0:
            out.append(_summarise(np.empty(0), b, total))
            continue
        mean, m2 = s / n, 0.0  # sum((v - mean)^2): sum(v^2) - n mean^2 cancels for values far from 0 (1e12 +- 1)
        fine = np.zeros(65536, np.int64)
        for win in wins:
            v = valid(win, b, "float64")
            fine += np.histogram(v, 65536, (lo, hi))[0]
            m2 += ((v - mean) ** 2).sum()
        out.append(_summarise(np.empty(0), b, total, hist=(
            fine, lo, hi, {"min": float(lo), "max": float(hi), "mean": mean, "std": math.sqrt(m2 / n)})))
    return {"exact": True, "sample_shape": [ds.height, ds.width], "bands": out}


# ---------------------------------------------------------------- pixel probe

@guarded
def pixel(path: str, col=None, row=None, x=None, y=None, crs: str = "lonlat", nodata: float | None = None) -> dict:
    ds = open_ds(path)
    mode = georef(ds)
    geo = ds.crs and mode == "transform"
    if col is None:
        if not geo:
            raise LookupError("dataset has no georeferencing")
        dst = _parse_crs(ds, crs).to_wkt()
        if crs == "native":
            nx, ny = x, y
        else:
            ext = _lon_extent(ds) if crs == "lonlat" else None
            if ext and not ext[0] <= x <= ext[1] and (ext[0] < -180 or ext[1] > 180):
                x += 360 if x < ext[0] else -360
            vcrs, vt, _ = geo_view(ds)
            nx, ny = transformer(dst, vcrs.to_wkt()).transform(x, y)
            if vt != ds.transform:  # back to the dataset's own native coordinates
                nx, ny = ds.transform * (~vt * (nx, ny))
        row, col = ds.index(nx, ny, op=math.floor)
    col, row = int(col), int(row)
    inside = 0 <= col < ds.width and 0 <= row < ds.height
    res = {"col": col, "row": row, "inside": inside, "values": None, "scaled": None, "nodata": None,
           "lonlat": None, "native": None}
    if geo:
        nx, ny = ds.xy(row, col)
        res["native"] = [float(nx), float(ny)]
        try:
            lon, lat = transformer(ds.crs.to_wkt(), geodetic_wkt(ds)).transform(nx, ny)
            res["lonlat"] = [(lon + 180) % 360 - 180, lat]
        except Exception:
            pass
    if inside:
        win = Window(col, row, 1, 1)
        with _io(ds):
            raw = ds.read(window=win)[:, 0, 0]
            mask = ds.read_masks(window=win)[:, 0, 0]
        res["values"] = [v.item() for v in raw]
        res["scaled"] = [v.item() * s + o for v, s, o in zip(raw, ds.scales, ds.offsets)]
        res["nodata"] = [bool(m == 0 or (nodata is not None and v == nodata)) for v, m in zip(raw, mask)]
    return res


# ---------------------------------------------------------------- section 6 rendering

_cmaps: dict | None = None


def colormaps() -> dict:
    global _cmaps
    if _cmaps is None:
        raw = json.loads((config.WEB_DIR / "colormaps.json").read_text("utf-8"))["maps"]
        _cmaps = {k: np.array(v, np.float32) for k, v in raw.items()}
    return _cmaps


def colormap_rgb(name: str, t: np.ndarray) -> np.ndarray:
    cm = colormaps().get(name)
    if name == "gray" or cm is None:
        return np.stack([t, t, t], axis=-1)
    return np.stack([np.interp(t, cm[:, 0], cm[:, i]) / 255.0 for i in (1, 2, 3)], axis=-1).astype(np.float32)


def stretch_band(v: np.ndarray, lo: float, hi: float, stretch: str, quantiles=None) -> np.ndarray:
    if stretch == "equalize":
        t = np.interp(v, quantiles, np.arange(33) / 32.0).astype(np.float32)
    else:
        t = np.clip((v - lo) / (hi - lo), 0, 1).astype(np.float32) if hi != lo else np.zeros_like(v, np.float32)
        if stretch == "log":
            t = (np.log1p(255 * t) / math.log(256)).astype(np.float32)
    return t


def adjust(rgb: np.ndarray, contrast=0.0, exposure=0.0, saturation=0.0, gamma=1.0, brightness=0.0) -> np.ndarray:
    """Contract section 6 step 5; rgb float32 [..., 3] in 0..1."""
    rgb = np.clip((contrast + 1) * rgb - contrast / 2, 0, 1)
    rgb = np.clip((exposure + 1) * rgb, 0, 1)
    S = saturation + 1
    sr, sg, sb = (1 - S) * 0.2126, (1 - S) * 0.7152, (1 - S) * 0.0722
    m = np.array([[sr + S, sg, sb], [sr, sg + S, sb], [sr, sg, sb + S]], np.float32)
    rgb = np.clip(rgb @ m.T, 0, 1)
    rgb = np.clip(rgb ** (1.0 / max(gamma, 1e-6)), 0, 1)
    return np.clip(rgb + brightness, 0, 1).astype(np.float32)


def render_rgba(data: np.ndarray, alpha: np.ndarray, p: dict) -> np.ndarray:
    """data [nb,h,w] raw values (nb = 1 or 3), alpha [h,w] 0..1 -> uint8 [h,w,4].
    p: stretch, min[], max[], quantiles[][] (equalize), invert, colormap, gamma, brightness, contrast,
    exposure, saturation, nodata."""
    ts = []
    for i in range(data.shape[0]):
        t = stretch_band(data[i], p["min"][i], p["max"][i], p["stretch"], p.get("quantiles", [None] * 3)[i])
        ts.append(1 - t if p["invert"] else t)
    rgb = colormap_rgb(p["colormap"], ts[0]) if len(ts) == 1 else np.stack(ts, axis=-1)
    rgb = adjust(rgb, p["contrast"], p["exposure"], p["saturation"], p["gamma"], p["brightness"])
    out = np.zeros(alpha.shape + (4,), np.uint8)
    out[..., :3] = np.floor(rgb * 255 + 0.5)
    invalid = alpha == 0
    out[invalid, :3] = 0
    out[..., 3] = 255 if p["nodata"] == "black" else np.floor(alpha * 255 + 0.5)
    return out


def png_bytes(rgba: np.ndarray) -> bytes:
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", compress_level=1)
    return buf.getvalue()


# ---------------------------------------------------------------- export

EXPORT_MAX_PIXELS = 400_000_000
_EXPORT_STRIP = 16_000_000  # output pixels per strip, bounds memory for any output size


@guarded
def export_file(path: str, dst, kind: str, fmt: str, bands: list[int], window: tuple | None, scale: float,
                resampling: str, p: dict | None, nodata: float | None = None) -> None:
    """Write the pixel window (col0,row0,col1,row1) at `scale` (<= 1) to `dst`.
    raw: original dtype/nodata/scale/offset GeoTIFF. rendered: section 6 pipeline as tif (RGBA), png (RGBA) or jpg (RGB)."""
    ds = open_ds(path)
    c0, r0, c1, r1 = window or (0, 0, ds.width, ds.height)
    if not (0 <= c0 < c1 <= ds.width and 0 <= r0 < r1 <= ds.height):
        raise ValueError("window must lie inside the image: col0,row0,col1,row1")
    if not 0 < scale <= 1:
        raise ValueError("scale must be in (0, 1]")
    ww, wh = c1 - c0, r1 - r0
    step = 1 / scale  # source px per output px
    ow, oh = math.ceil(ww / step - 1e-9), math.ceil(wh / step - 1e-9)
    rendered = kind == "rendered"
    planes = 1 if rendered else len(bands)  # raw cubes count every band
    if ow * oh * planes > EXPORT_MAX_PIXELS:
        s = math.sqrt(EXPORT_MAX_PIXELS / (ww * wh * planes))
        what = f"{ow}x{oh} px" + (f" x {planes} bands" if planes > 1 else "")
        raise OverflowError(f"output would be {what} (> 400 million samples); use scale <= {s:.3g} or a smaller window")
    rs = _rs(resampling, DOWNSAMPLE, "resampling") if scale < 1 else Resampling.nearest
    nb = (3 if fmt == "jpg" else 4) if rendered else len(bands)
    prof = dict(driver="GTiff", width=ow, height=oh, count=nb, tiled=True, blockxsize=256, blockysize=256,
                compress="DEFLATE", BIGTIFF="IF_SAFER")
    if rendered:
        prof["dtype"] = "uint8"
    else:
        prof["dtype"] = np.result_type(*[ds.dtypes[b - 1] for b in bands]).name
        if ds.nodatavals[bands[0] - 1] is not None:
            prof["nodata"] = ds.nodatavals[bands[0] - 1]
        elif nodata is not None:
            prof["nodata"] = nodata
    if ds.crs and georef(ds) == "transform":
        prof.update(crs=ds.crs, transform=ds.transform @ Affine.translation(c0, r0) @ Affine.scale(step))
    tmp = dst if fmt == "tif" else dst.with_suffix(".tmp.tif")
    rows = _EXPORT_STRIP // (ow * len(bands))  # samples read per strip stay bounded for many-band cubes
    rows = rows // 256 * 256 if rows >= 256 else max(1, rows)
    try:
        with rasterio.open(tmp, "w", **prof) as out:
            if georef(ds) == "gcps":
                from rasterio.control import GroundControlPoint
                out.gcps = ([GroundControlPoint((g.row - r0) / step, (g.col - c0) / step, g.x, g.y, g.z) for g in ds.gcps[0]],
                            ds.gcps[1])
            if not rendered:
                out.scales, out.offsets = [ds.scales[b - 1] for b in bands], [ds.offsets[b - 1] for b in bands]
                out.descriptions = [ds.descriptions[b - 1] for b in bands]
            elif fmt == "tif":
                out.colorinterp = [rasterio.enums.ColorInterp.red, rasterio.enums.ColorInterp.green,
                                   rasterio.enums.ColorInterp.blue, rasterio.enums.ColorInterp.alpha]
            for orow in range(0, oh, rows):
                n = min(rows, oh - orow)
                win = (c0, r0 + orow * step, ww, min(n * step, wh - orow * step))
                if rendered:
                    data, alpha = read_block(ds, bands, *win, step, rs, nodata)
                    rgba = render_rgba(data, alpha, p)
                    if nb == 3:
                        rgba = rgba[..., :3]
                    block = rgba.transpose(2, 0, 1)
                elif prof.get("nodata") is None or not _bleed(ds, step, rs, nodata):  # GDAL leaves no-data out itself
                    with _io(ds):
                        block = ds.read(bands, window=Window(*win), out_shape=(len(bands), n, ow), resampling=rs,
                                        out_dtype=prof["dtype"])
                else:  # reduced JPEG 2000 / label no-data: the display's grid and edges, each band with its own mask
                    block = np.empty((len(bands), n, ow), prof["dtype"])
                    for k, b in enumerate(bands):
                        d, alpha = read_block(ds, [b], *win, step, rs, nodata, prof["dtype"])
                        d[:, alpha == 0] = prof["nodata"]
                        block[k] = d[0]
                out.write(block, window=Window(0, orow, ow, n))
        if fmt != "tif":
            opts = {"quality": 92} if fmt == "jpg" else {"ZLEVEL": 3}
            rasterio.shutil.copy(tmp, dst, driver="JPEG" if fmt == "jpg" else "PNG", **opts)
    finally:
        if tmp != dst:
            tmp.unlink(missing_ok=True)
            dst.with_name(dst.name + ".aux.xml").unlink(missing_ok=True)
