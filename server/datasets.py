"""Datasets API (CONTRACTS.md sections 5-6): open/status, file browser, stats, tiles, warp, pixel probe."""
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

import numpy as np
import rasterio
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel
from pyproj.exceptions import ProjError
from rasterio._err import CPLE_BaseError

from . import config
from .raster import render, sources

router = APIRouter()


class OpenRequest(BaseModel):
    url: str | None = None
    path: str | None = None
    name: str | None = None
    companions: list[str] | None = None
    mode: str = "auto"
    meta: dict | None = None


def _call(fn, *args, **kw):
    """Run raster code, mapping its failures to HTTP errors."""
    try:
        return fn(*args, **kw)
    except KeyError:
        raise HTTPException(404, "unknown dataset")
    except FileNotFoundError as ex:
        raise HTTPException(404, f"not found: {ex}")
    except (ValueError, LookupError, ProjError) as ex:
        raise HTTPException(400, str(ex))
    except OverflowError as ex:
        raise HTTPException(413, str(ex))
    except rasterio.errors.RasterioIOError as ex:
        raise HTTPException(502, f"could not read the data: {ex}")
    except CPLE_BaseError as ex:  # e.g. no coordinate operation between a Moon raster and EPSG:3857
        raise HTTPException(400, str(ex))


def _entry(ds_id: str) -> dict:
    return _call(sources.get, ds_id)


def _ready(ds_id: str) -> tuple[dict, str]:
    e = _entry(ds_id)
    if e["status"] != "ready":
        raise HTTPException(409, f"dataset is {e['status']}")
    if not e["info"]["count"]:
        raise HTTPException(400, "dataset has no raster bands; open one of its subdatasets")
    return e, sources.serve_path(e)


def _bands(spec: str | None, count: int) -> list[int]:
    try:
        bands = [int(b) for b in spec.split(",")] if spec else list(range(1, min(count, 3) + 1))
    except ValueError:
        raise HTTPException(400, "bands must be comma separated integers")
    if not bands or any(not 1 <= b <= count for b in bands):
        raise HTTPException(400, f"bands must be within 1..{count}")
    return bands


# ---------------------------------------------------------------- open / status

@router.post("/datasets/open")
def open_dataset(req: OpenRequest):
    return _call(sources.open_dataset, req.url, req.path, req.name, req.companions, req.mode, req.meta)


@router.post("/datasets/upload")
def upload(files: list[UploadFile] = File(...)):
    return _call(sources.save_upload, [(f.filename or "upload", f.file) for f in files])


@router.get("/datasets")
def list_datasets():
    return sources.listing()


@router.get("/datasets/{ds_id}")
def dataset_status(ds_id: str):
    return sources.public(_entry(ds_id))


@router.delete("/datasets/{ds_id}")
def delete_dataset(ds_id: str, purge: int = 0):
    _call(sources.delete, ds_id, bool(purge))
    _stats.clear()
    _forget_warps()
    return {"ok": True}


@router.get("/fs")
def fs(path: str = ""):
    try:
        return sources.list_dir(path)
    except FileNotFoundError:
        raise HTTPException(404, "not a directory")
    except PermissionError:
        raise HTTPException(403, "permission denied")


# ---------------------------------------------------------------- statistics

_stats: dict[tuple, dict] = {}
_stats_lock = threading.Lock()


def band_stats(ds_id: str, bands: list[int], exact: bool) -> dict:
    e, path = _ready(ds_id)
    todo = [b for b in bands if (ds_id, b, exact) not in _stats]
    if todo:
        res = render.stats(path, todo, exact, e.get("_nodata"))
        with _stats_lock:
            for b in res["bands"]:
                _stats[(ds_id, b["band"], exact)] = {"shape": res["sample_shape"], "band": b}
    got = [_stats[(ds_id, b, exact)] for b in bands]
    return {"exact": exact, "sample_shape": got[0]["shape"], "bands": [g["band"] for g in got]}


@router.get("/datasets/{ds_id}/stats")
def stats(ds_id: str, bands: str | None = None, exact: int = 0):
    count = _ready(ds_id)[0]["info"]["count"]
    return _call(band_stats, ds_id, _bands(bands, count) if bands else list(range(1, count + 1)), bool(exact))


# ---------------------------------------------------------------- tiles, warp, pixel

def _f32_response(arr: np.ndarray) -> Response:
    return Response(np.ascontiguousarray(arr, dtype="<f4").tobytes(), media_type="application/octet-stream",
                    headers={"X-Bands": str(arr.shape[2])})


@router.get("/datasets/{ds_id}/tile/{z}/{x}/{y}")
def tile(ds_id: str, z: int, x: int, y: int, bands: str | None = None, resampling: str = "average",
         magnify: str = "nearest"):
    e, path = _ready(ds_id)
    arr = _call(render.tile_f32, path, z, x, y, _bands(bands, e["info"]["count"]), resampling, magnify, e.get("_nodata"))
    return _f32_response(arr)


def _floats(spec: str | None, n: int, default: list[float]) -> list[float]:
    try:
        v = [float(s) for s in spec.split(",")] if spec else default
    except ValueError:
        raise HTTPException(400, "min/max must be comma separated numbers")
    if len(v) != n:
        raise HTTPException(400, f"expected {n} min/max values")
    return v


def _render_params(ds_id, bl, stretch, min, max, gamma, brightness, contrast, exposure, saturation, invert,
                   colormap, nodata) -> dict:
    if len(bl) not in (1, 3):
        raise HTTPException(400, "rendering needs 1 or 3 bands")
    if stretch not in ("linear", "equalize", "log") or nodata not in ("transparent", "black"):
        raise HTTPException(400, "bad stretch or nodata")
    st = band_stats(ds_id, bl, False)["bands"]
    return {"stretch": stretch, "min": _floats(min, len(bl), [s["min"] for s in st]),
            "max": _floats(max, len(bl), [s["max"] for s in st]), "quantiles": [s["quantiles"] for s in st],
            "invert": bool(invert), "colormap": colormap, "gamma": gamma, "brightness": brightness,
            "contrast": contrast, "exposure": exposure, "saturation": saturation, "nodata": nodata}


_warps: OrderedDict[tuple, np.ndarray] = OrderedDict()
_warps_lock = threading.Lock()
_warp_bytes = 0
WARP_CACHE_BYTES = 256 << 20


def _forget_warps() -> None:
    global _warp_bytes
    with _warps_lock:
        _warps.clear()
        _warp_bytes = 0


def _cached_warp(e: dict, path: str, crs: str, box: tuple, width: int, height: int, bl: list[int], resampling: str,
                 magnify: str) -> np.ndarray:
    """render.warp_f32 behind an LRU: panning back in the map view or restyling the globe (same area, other colours)
    skips the source read, which for a remote COG refetches its blocks (~1 s per 512 px warp)."""
    global _warp_bytes
    key = (e["id"], e.get("_created"), e.get("_serve"), e.get("_stream"), e.get("_nodata"), crs, box, width, height,
           tuple(bl), resampling, magnify)
    with _warps_lock:
        if key in _warps:
            _warps.move_to_end(key)
            return _warps[key]
    arr = _call(render.warp_f32, path, crs, box, width, height, bl, resampling, magnify, e.get("_nodata"))
    arr.setflags(write=False)  # shared by later responses
    if arr.nbytes > WARP_CACHE_BYTES // 4:  # e.g. a 224-band cube: it would push everything else out
        return arr
    with _warps_lock:
        if key not in _warps:
            _warps[key] = arr
            _warp_bytes += arr.nbytes
        while _warp_bytes > WARP_CACHE_BYTES:
            _warp_bytes -= _warps.popitem(last=False)[1].nbytes
    return arr


@router.get("/datasets/{ds_id}/warp")
def warp(ds_id: str, crs: str, bbox: str, width: int = Query(256, ge=1, le=1024), height: int = Query(256, ge=1, le=1024),
         bands: str | None = None, resampling: str = "average", magnify: str = "nearest", format: str = "f32",
         stretch: str = "linear", min: str | None = None, max: str | None = None, gamma: float = 1.0,
         brightness: float = 0.0, contrast: float = 0.0, exposure: float = 0.0, saturation: float = 0.0,
         invert: int = 0, colormap: str = "gray", nodata: str = "transparent"):
    e, path = _ready(ds_id)
    if format not in ("f32", "png"):
        raise HTTPException(400, "format must be f32 or png")
    try:
        box = tuple(float(v) for v in bbox.split(","))
        assert len(box) == 4 and box[0] < box[2] and box[1] < box[3]
    except (ValueError, AssertionError):
        raise HTTPException(400, "bbox must be minx,miny,maxx,maxy")
    bl = _bands(bands, e["info"]["count"])
    arr = _cached_warp(e, path, crs, box, width, height, bl, resampling, magnify)
    if format == "f32":
        return _f32_response(arr)
    p = _render_params(ds_id, bl, stretch, min, max, gamma, brightness, contrast, exposure, saturation, invert,
                       colormap, nodata)
    rgba = render.render_rgba(arr[..., :-1].transpose(2, 0, 1), arr[..., -1], p)
    return Response(render.png_bytes(rgba), media_type="image/png")


@router.get("/datasets/{ds_id}/pixel")
def pixel(ds_id: str, col: float | None = None, row: float | None = None, x: float | None = None,
          y: float | None = None, crs: str = "lonlat"):
    e, path = _ready(ds_id)
    if (col is None or row is None) and (x is None or y is None):
        raise HTTPException(400, "give col,row or x,y")
    if col is None or row is None:
        return _call(render.pixel, path, x=x, y=y, crs=crs, nodata=e.get("_nodata"))
    return _call(render.pixel, path, col=int(col // 1), row=int(row // 1), nodata=e.get("_nodata"))


# ---------------------------------------------------------------- export

_MEDIA = {"png": "image/png", "jpg": "image/jpeg", "tif": "image/tiff"}


def _sweep_exports(d: Path) -> None:
    for f in d.glob("*"):  # leftovers of interrupted exports
        try:
            if time.time() - f.stat().st_mtime > 3600:
                f.unlink()
        except OSError:  # removed meanwhile by its own request, or still open on Windows
            pass


@router.get("/datasets/{ds_id}/export")
def export(ds_id: str, kind: str = "rendered", format: str = "png", scale: float = 1.0, window: str | None = None,
           bands: str | None = None, resampling: str = "average", stretch: str = "linear", min: str | None = None,
           max: str | None = None, gamma: float = 1.0, brightness: float = 0.0, contrast: float = 0.0,
           exposure: float = 0.0, saturation: float = 0.0, invert: int = 0, colormap: str = "gray",
           nodata: str = "transparent"):
    e, path = _ready(ds_id)
    if kind not in ("rendered", "raw") or format not in _MEDIA:
        raise HTTPException(400, "kind must be rendered|raw and format png|jpg|tif")
    if kind == "raw" and format != "tif":
        raise HTTPException(400, "raw export is GeoTIFF only (format=tif)")
    try:
        win = tuple(int(float(v)) for v in window.split(",")) if window else None
        assert win is None or len(win) == 4
    except (ValueError, AssertionError):
        raise HTTPException(400, "window must be col0,row0,col1,row1")
    count = e["info"]["count"]
    bl = _bands(bands, count) if bands or kind == "rendered" else list(range(1, count + 1))
    p = _render_params(ds_id, bl, stretch, min, max, gamma, brightness, contrast, exposure, saturation, invert,
                       colormap, nodata) if kind == "rendered" else None
    d = config.CACHE_DIR / "exports"
    d.mkdir(parents=True, exist_ok=True)
    _sweep_exports(d)
    dst = d / f"{uuid.uuid4().hex}.{format}"
    try:
        _call(render.export_file, path, dst, kind, format, bl, win, scale, resampling, p, e.get("_nodata"))
    except BaseException:
        dst.unlink(missing_ok=True)
        raise
    stem = Path(e["name"]).stem.replace('"', "_")
    return FileResponse(dst, media_type=_MEDIA[format], filename=f"{stem}_{kind}.{format}",
                        background=BackgroundTask(lambda: dst.unlink(missing_ok=True)))
