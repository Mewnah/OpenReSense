# OpenReSense — build contracts

OpenReSense is a local app (FastAPI on `127.0.0.1:8765` + vanilla-JS single page) that

1. holds a curated, searchable **catalog** of every remote-sensing imagery source we can find
   (Earth and all planetary bodies, all agencies / universities / companies / services);
2. runs **live federated searches** against public APIs (NASA CMR, STAC APIs, PDS ODE, PDS OPUS,
   PDS4 Registry, EPN-TAP/VESPA, MAST, ASF, rover raw-image feeds, …) to list real datasets/products;
3. **opens and views raw data** (PDS3/PDS4/ISIS/VICAR/FITS/GeoTIFF/COG/JP2/HDF4/HDF5/NetCDF/…) with
   faithful resampling, live stretch/brightness/contrast/gamma/saturation/invert/B&W/colormaps,
   a pixel-space image view, a georeferenced 2D map view and a 3D globe (any body's ellipsoid).

This file is the single source of truth for interfaces between the parts. Builders implement their
part against it and must not change another part's files.

## 0. Principles (apply to every builder)

* Smallest complete implementation. No frameworks, no build step, no speculative abstraction,
  no config options nobody asked for. Comments only for the non-obvious *why*.
* Python 3.12 venv at `.venv` (run `.venv/Scripts/python.exe`). Installed and allowed:
  rasterio (GDAL 3.12 with GTiff/COG, JP2OpenJPEG, PDS, PDS4, ISIS2, ISIS3, VICAR, HDF5, netCDF,
  SAFE, SENTINEL2, ENVI, NITF, Zarr, CEOS, WMS/WMTS; **no FITS, no HDF4**), numpy, fastapi,
  uvicorn, python-multipart, httpx, pyproj, pillow, pdr, astropy, pyhdf, h5py, pytest.
  Do not pip/npm install anything else; if something is truly missing, report it.
* Frontend: plain ES modules in `web/js/`, globals from `/vendor`: `ol` (OpenLayers 10.11 full
  build), `proj4` (2.22), `Cesium` (1.147, lazy-loaded by the globe module from
  `/vendor/cesium/Cesium.js`; `window.CESIUM_BASE_URL='/vendor/cesium/'` is preset).
* **Faithfulness**: never alter data values silently. Raw values are always available (pixel
  probe). The UI always states the resampling in effect and the stretch applied.
* Network: be polite (User-Agent from `server/config.py`, timeouts, no hammering).
* Local security (`server/app.py`): the server binds 127.0.0.1, accepts only `Host` 127.0.0.1/localhost
  (DNS rebinding) and refuses non-GET requests whose `Origin` is another site (cross-site form posts).
  UI text goes through `h()` (textContent); remote links/sources pass `safeUrl` (http/https only).
* Windows paths, Git Bash/PowerShell available. Don't leave servers running when you finish.

## 1. Layout and ownership

| Path | Owner |
|---|---|
| `CONTRACTS.md`, `requirements.txt`, `package.json`, `server/app.py`, `server/config.py`, `server/__main__.py`, `server/search.py`, `server/connectors/__init__.py`, `server/connectors/base.py`, `catalog/vocab.json`, `scripts/validate_catalog.py`, `web/index.html`, `web/css/base.css`, `web/js/{app,api,bus,bodies,render}.js`, `web/colormaps.json` | scaffold (read-only for builders) |
| `catalog/sources/<domain>.json` | one catalog researcher per domain |
| `catalog/bodies.json`, `web/img/bodies/*` | bodies researcher |
| `server/datasets.py`, `server/raster/**`, `tests/test_raster*.py`, `tests/test_datasets*.py`, `scripts/fetch_samples.py` | raster backend builder |
| `server/connectors/<name>.py`, `tests/test_connector_<name>.py` | the connector builder assigned that name |
| `server/catalog.py`, `web/js/explore.js`, `web/js/search.js`, `web/js/explore-*.js`, `web/js/search-*.js`, `web/css/explore.css`, `tests/test_catalog*.py` | explore/search UI builder |
| `web/js/viewer.js`, `web/js/viewer-*.js`, `web/css/viewer.css` | viewer builder |
| `web/js/globe.js`, `web/js/globe-*.js`, `web/css/globe.css` | globe builder |

Need a change in a file you don't own? Don't edit it — describe it in your final report under
`contract_change_requests`.

## 2. Catalog source entries (`catalog/sources/<domain>.json`)

File shape: `{"domain": "<domain>", "sources": [Entry, ...]}`. Validate with
`.venv/Scripts/python.exe scripts/validate_catalog.py catalog/sources/<domain>.json`.

```jsonc
{
  "id": "pds-imaging-atlas",                 // unique kebab-case slug, globally unique across files
  "name": "PDS Planetary Image Atlas",
  "provider": "NASA PDS Cartography and Imaging Sciences Node (JPL / USGS)",
  "agency": ["NASA", "JPL"],                 // short org names, consistent casing (NASA, ESA, JAXA, ISRO, CNSA, USGS, NOAA, ...)
  "country": ["USA"],                        // optional
  "kind": "archive",                         // vocab.kind
  "url": "https://pds-imaging.jpl.nasa.gov/search/",   // main landing page — MUST load (verified)
  "links": [{"label": "API docs", "url": "https://..."}],   // optional extra verified links (search UI, API, FTP, docs)
  "targets": ["mars", "moon", "titan"],      // vocab.targets ids — list every body the source meaningfully covers
  "regions": ["antarctica"],                 // vocab.regions ids — only for Earth sources with a regional focus ("global" for global)
  "missions": ["Cassini-Huygens"],           // optional, human-readable
  "instruments": ["ISS", "VIMS"],            // optional
  "data_types": ["imagery", "hyperspectral"],// vocab.data_types
  "spectral": ["visible", "nir"],            // vocab.spectral
  "levels": ["raw", "calibrated"],           // vocab.levels
  "formats": ["PDS3", "PDS4"],               // vocab.formats
  "access": "open",                          // vocab.access
  "protocols": ["https", "web-ui", "rest"],  // vocab.protocols
  "resolution": "0.25–1.3 m/px",             // optional free text
  "temporal": "2006–present",                // optional free text
  "description": "2–4 factual sentences: what is there, why it is useful, quirks (login, formats).",
  "tags": ["hirise", "mro"],                 // optional lowercase search helpers (acronyms, alt names)
  "connector": "ode",                        // optional: id of a live connector (section 4) that searches this source
  "verified": "2026-11-05",                  // date you confirmed the url (and links) load
  "notes": "optional internal note (e.g. URL moved from X)"
}
```

## 3. Catalog API and bodies (`server/catalog.py`, `catalog/bodies.json`)

`GET /api/catalog?q=&target=&region=&agency=&kind=&data_types=&spectral=&levels=&formats=&access=&connector=&limit=&offset=`
(`connector` filters by live-connector id; facets include `connector` counts)
(multi-valued filters are comma lists; OR within a facet, AND across facets; `q` matches name,
provider, agency, description, missions, instruments, tags, target names/aliases, region names)
→ `{"total": n, "results": [Entry...], "facets": {"targets": {"mars": 12, ...}, "agency": {...}, "kind": {...}, "data_types": {...}, "spectral": {...}, "levels": {...}, "formats": {...}, "access": {...}, "regions": {...}}}`
(facet counts computed over the query with that facet's own filter removed). Sources are loaded
from `catalog/sources/*.json` plus optional user files `catalog/user/*.json` (same shape), reloaded
when a file's mtime changes. Results are ordered by relevance (whole-word > prefix matches; name > tags/missions/targets > organisation > description), then by focus on the selected target/region (named after it, few targets, data sources before tools), then name.

`GET /api/catalog/{id}` → one Entry. `GET /api/bodies` (scaffold) → `{"bodies": [...], "regions": {...}}`.
`GET /api/vocab` (scaffold) → vocab.json.

`catalog/bodies.json`:

```jsonc
{"bodies": [{
  "id": "moon", "name": "Moon", "aliases": ["Luna", "lunar"],
  "group": "Earth & Moon",            // UI grouping, keep the provisional group names
  "type": "moon",                     // star | planet | dwarf-planet | moon | asteroid | comet | kbo | interstellar | group
  "parent": "earth",                  // body id or null
  "naif_id": 301,
  "radii_m": [1737400, 1737400, 1737400],   // IAU WGCCRE 2015 a, b, c in metres (null for "group" entries)
  "mean_radius_m": 1737400,
  "color": "#b0b0b0",                 // swatch when no thumb
  "thumb": "img/bodies/moon.jpg",     // optional (public-domain / CC; credits in web/img/bodies/CREDITS.md)
  "summary": "One sentence.",
  "basemaps": [Basemap, ...]          // best first; [] if nothing public exists
}]}
```

Basemap (every entry verified by actually fetching a tile/image):

```jsonc
{
  "id": "moon-lroc-wac", "name": "LRO WAC global mosaic", "provider": "NASA Moon Trek",
  "type": "xyz",                    // "xyz" (URL template) | "wms"
  "url": "https://.../{z}/{y}/{x}.jpg",   // xyz: template with {z} {x} {y}; wms: endpoint base URL
  "tiling": "geographic-2x1",       // xyz only: "geographic-2x1" (z0 = 2 cols x 1 row, 180°/tile, y=0 at north)
                                    //            | "webmercator"
  "layers": "...", "version": "1.1.1", "format": "image/jpeg", "srs": ["EPSG:4326"],   // wms only
  "crs": "lonlat",                  // tiles' CRS: "lonlat" (body geographic) | "EPSG:3857" | "EPSG:3031" | ...
  "min_zoom": 0, "max_zoom": 7, "tile_size": 256,
  "kind": "imagery",                // imagery | shaded-relief | color-dem | map
  "attribution": "NASA/GSFC/ASU", "verified": "2026-11-05"
}
```

## 4. Live-search connectors (`server/connectors/<name>.py`)

Module contract (see `base.py`; modules are auto-discovered, no registration edits):

```python
from .base import Query, asset, result, page
CONNECTORS = [{
  "id": "ode", "name": "PDS Orbital Data Explorer", "provider": "NASA PDS Geosciences Node (WUSTL)",
  "targets": ["mars", "moon", "mercury", "venus", "phobos", "deimos"],   # vocab target ids, or ["*"]
  "levels": ["collection", "product"],     # which Query.level values work
  "supports": ["text", "bbox", "time", "instrument"],   # which Query fields are honoured
  "url": "https://ode.rsl.wustl.edu/", "description": "One or two sentences."
}]
async def search(conn_id: str, q: Query, client: httpx.AsyncClient) -> dict:  # -> page(...)
```

Router (scaffold): `GET /api/connectors?target=` → info list; `GET /api/search?connector=&level=&text=&target=&region=&bbox=W,S,E,N&start=&end=&collection=&instrument=&mission=&limit=&offset=`
→ `{"results": [Result...], "total": int|null, "next_offset": int|null, "notes": [str]}`.
Info may carry `"needs_collection": true` when product search requires `Query.collection` (the UI
then lists that connector's collections first). Raise `ValueError("message")` for unsupported combinations (→ HTTP 400); let `httpx` errors
propagate (→ 502). Hard limit: answer within ~30 s (use per-request timeouts, cap fan-out).

Result (build with `result(...)`):

```jsonc
{
  "id": "unique within connector", "connector": "ode", "level": "product",   // or "collection"
  "title": "ESP_012345_1234 RED", "description": "short text or null",
  "target": "mars",                  // vocab target id when known
  "mission": "MRO", "instrument": "HiRISE",
  "start": "2009-01-02T03:04:05Z", "end": null,
  "footprint": {GeoJSON geometry in lon/lat, east-positive -180..180} | null,
  "bbox": [W, S, E, N] | null,
  "thumbnail": "https://... small image" | null,
  "browse": "https://... larger preview image" | null,
  "assets": [{"url": "...", "name": "ESP_..._RED.JP2", "role": "data|label|browse|thumbnail|metadata|other",
              "type": "image/jp2 or format name", "size": bytes|null, "openable": true}],
  "landing": "https://human readable page" | null,
  "collection": "id to pass back as Query.collection for product search (collection-level results)" | null,
  "extra": {"small": "metadata subset shown in a details table"}
}
```

Rules: `assets[].openable` comes from `base.is_openable` unless you know better (e.g. a PDS4
`.xml` label is openable with role "label"). Prefer direct file URLs over HTML pages. For PDS3
products list both the label and the data file. Planetary-Computer assets are returned unsigned;
the backend signs them at open time.

## 5. Datasets API (`server/datasets.py`, `server/raster/`)

All blocking GDAL work runs in sync (`def`) endpoints / worker threads, never on the event loop.

### Open / status

`POST /api/datasets/open` JSON `{"url"?: str, "path"?: str, "name"?: str, "companions"?: [url], "mode"?: "auto"|"stream"|"download", "meta"?: object}` → `DatasetStatus` (returns immediately; work continues in background).

* `path`: local file (or GDAL subdataset string like `HDF5:"C:\x.h5"://grp/var`), opened in place, never copied.
* `url`: `auto` streams COG/GeoTIFF via `/vsicurl/` when it opens and is tiled/has overviews, else
  downloads to `cache/downloads/<hash>/<filename>` with progress. Companion handling: a PDS3 `.LBL`
  → parse `^IMAGE`/`^QUBE`/`^SPECTRAL_QUBE`/… pointers and fetch the data file(s) from the same
  directory (try exact, upper and lower case); a `.IMG` → also try the sibling `.LBL`/`.lbl`
  (absent is fine: attached label); a PDS4 `.xml` → fetch every `<file_name>`; plus every URL in
  `companions`. If a `.LBL`'s `^IMAGE` file does not exist, its `FILE_NAME` entries are tried
  (HiRISE `.JP2`); `.img/.dat/.raw` cubes also fetch the sibling `.HDR` (ENVI) and a sibling `.xml`
  (PDS4) that names the file. Download names: PDS Imaging Atlas/Starbrite `.../prod?q=OFSN = /path/FILE
  AND RT = RAW|PDS_LABEL` and MAST `.../Download/file?uri=mast:.../FILE` are named after FILE; every
  name taken from a URL, label or upload is reduced to a bare file name (never a path). A download
  that answers with an HTML page (login/error/landing page) fails with a message saying so.
  Dropped connections, timeouts, 408/425/429 and 5xx are retried 3 times (1, 2, 4 s), resuming from the
  `.part` file with `Range` + `If-Range` (a file that changed restarts). Optional siblings (labels, `.HDR`) are
  fetched in parallel and only 403/404/410 count as absent; one that keeps failing fails the open instead of
  leaving a dataset without its label. Files that start with a PDS3 label get no sibling probes. Earthdata token (settings `earthdata_token`) sent as `Authorization: Bearer` only to
  NASA Earthdata/ASF hosts; Planetary Computer blob URLs signed via
  `https://planetarycomputer.microsoft.com/api/sas/v1/sign?href=`.
* Reader chain: rasterio/GDAL → `pdr` (PDS3/PDS4 incl. compressed/odd layouts) → `astropy` (FITS)
  → `pyhdf` (HDF4). Non-GDAL results are written once to a lossless COG in the cache and served
  from there (`info.converted`). Files with subdatasets and no top-level bands report
  `info.subdatasets` (open one by POSTing its `name` as `path`).
* PDS3 labels: no-data (`CORE_NULL`/`MISSING_CONSTANT`/`NULL_CONSTANT` of the IMAGE object) and
  `SCALING_FACTOR`/`OFFSET`, which GDAL's PDS driver ignores, are applied through a VRT. Exception: for big
  (> 16 MP) tiled/JPEG 2000 products (HiRISE RDR) the no-data stays out of the VRT, because a VRT NoData makes
  GDAL's reduced-resolution reads go to full resolution (13x slower at 1/8); the server applies it in its own
  reads (tiles, warp, stats, probe, export) and reports it in `info.nodata`.
* Large rasters that are neither tiled nor have overviews get a background lossless COG copy
  (overviews resampled with AVERAGE); `optimizing: true` until done (message says so); tiles work meanwhile.
* Dataset id: stable hash of the source (`d_` + 10 hex), so re-opening returns the same dataset.
  Registry persisted in `cache/datasets.json`.

`DatasetStatus`:

```jsonc
{"id": "d_0123456789", "name": "ESP_012345_1234_RED.JP2", "status": "queued|downloading|converting|ready|error",
 "optimizing": false, "progress": 0.42, "message": "human text", "bytes_done": 1234, "bytes_total": 5678,
 "source": {"url": "...", "path": "..."}, "meta": {...}, "info": DatasetInfo|null}
```

`GET /api/datasets` → `[DatasetStatus]` newest first. `GET /api/datasets/{id}` → `DatasetStatus`.
`DELETE /api/datasets/{id}?purge=0|1` → removes from the list (purge=1 also deletes its cached
downloads/conversions; never deletes user files). `POST /api/datasets/upload` (multipart `files`,
one or more — e.g. a `.LBL` + `.IMG` pair) → saved under `cache/uploads/<hash>/` → `DatasetStatus`.
`GET /api/fs?path=` → `{"path": abs|"" , "parent": abs|null, "drives": ["C:\\", ...], "entries": [{"name", "path", "is_dir", "size", "openable"}]}` (dirs first; `path=""` lists drives + home).

`DatasetInfo`:

```jsonc
{
 "driver": "PDS", "width": 2048, "height": 1024, "count": 1,
 "dtype": "uint16", "dtypes": ["uint16"], "nodata": [0], "scales": [1.0], "offsets": [0.0],
 "units": [""], "band_names": ["Band 1"], "colorinterp": ["gray"], "wavelengths": [null],   // nm when known
 "crs": null | {"wkt": "...", "proj4": "+proj=eqc +R=1737400 ...", "name": "Equirectangular Moon",
                "is_geographic": false, "units": "m"|"degree", "body": "moon"|null, "a": 1737400, "b": 1737400},
 "transform": [a, b, c, d, e, f] | null,     // rasterio Affine: x = a*col + b*row + c, y = d*col + e*row + f
 "bounds": [minx, miny, maxx, maxy] | null,  // native CRS
 "lonlat_bounds": [W, S, E, N] | null,       // geographic on the dataset's own body
 "georef": "transform" | "gcps" | "none",
 "resolution": [xres, yres] | null,          // native units per pixel
 "tiles": {"tile_size": 256, "max_zoom": 3, "overzoom": 4},
 "overviews": [2, 4, 8],
 "subdatasets": [{"name": "HDF5:\"...\"://x", "description": "..."}],
 "metadata": {"": {"k": "v"}, "<domain>": {...}},
 "label": "full text of the PDS3/PDS4/ISIS/VICAR label or FITS header" | null,
 "files": ["C:\\...\\x.IMG", "C:\\...\\x.LBL"],
 "converted": null | {"from": "pdr|astropy|pyhdf|cog", "reason": "..."}
}
```

`nodata` per band: number, `"nan"`/`"inf"`/`"-inf"` (JSON has no NaN) or null. `crs.name` is the CRS's
own name when meaningful, else `"<projection> · <Body>"`. FITS keeps raw integers with BSCALE/BZERO as
scales/offsets and BLANK as nodata. Every JSON response turns NaN/±Infinity into `null` (e.g. probe
values of NaN pixels).

`count == 0` with a non-empty `subdatasets` means "open one of the subdatasets" (the viewer shows a
picker). `driver` names the source format (PDS, FITS, HDF4, ...) even when `converted` is set.
`lonlat_bounds` is normalised to −180..180; W > E means the dataset crosses the antimeridian.

`body` detection: match datum/ellipsoid/CRS names (e.g. "Moon_2000", "GCS_Mars_2000",
"D_Titan") against body ids/names, else match the semi-major axis to `catalog/bodies.json` radii
(±0.5 %); Earth ellipsoids → "earth".

### Statistics

`GET /api/datasets/{id}/stats?bands=1,2,3&exact=0` →

```jsonc
{"exact": false, "sample_shape": [1024, 512], "bands": [{
   "band": 1, "min": 3.0, "max": 4011.0, "mean": 812.3, "std": 140.2, "valid": 524000, "total": 524288,
   "percentiles": {"0.1": .., "0.5": .., "1": .., "2": .., "5": .., "95": .., "98": .., "99": .., "99.5": .., "99.9": ..},
   "quantiles": [33 values at k/32, k = 0..32],
   "histogram": {"counts": [256 ints], "min": 3.0, "max": 4011.0}}]}
```

Approximate stats use a nearest-neighbour decimated read (≤ ~1024² px, true values, no
averaging); `exact=1` scans the full image (8/16-bit data in one pass that counts every value). A shared JPEG 2000
handle is locked per read, not for the scan: one codec block per read when blocks are ≥ 1024 px, else ~4 MP strips, so
tiles keep loading during the scan (HiRISE RDR: ~2.5 min, tiles ~0.25 s meanwhile). Raw (unscaled) values, nodata/mask (including label
no-data) excluded; the approximate sample also leaves out the blend ring described under tiles.
Cached per (dataset, band, exact).

### Pixel-space tiles (image view)

```
N = max(width, height); max_zoom = max(0, ceil(log2(N / 256)))     # level where 1 tile px == 1 image px
scale(z) = 2 ** (max_zoom - z)       # image px per tile px; fractional for overzoom z > max_zoom (up to max_zoom + overzoom)
tile (x, y) covers image cols [x*256*scale, (x+1)*256*scale) and rows [y*256*scale, (y+1)*256*scale), y=0 at the top
```

`GET /api/datasets/{id}/tile/{z}/{x}/{y}?bands=1,2,3&resampling=average&magnify=nearest` →
`application/octet-stream`: Float32 little-endian, 256×256 pixels, pixel-interleaved
`[b1, b2, b3, alpha]` (nb + 1 channels; header `X-Bands: nb+1`). Raw unscaled values. Alpha = 1
valid / 0 nodata-masked-or-outside (fractional for partial alpha masks); invalid values set to 0. At reduced
resolution of JPEG 2000 sources and of label no-data (above), pixels within 3 output px of no-data (12 px inside
the 4 px along the image edge) are also 0: the resolution levels blend no-data into them (measured on HiRISE:
up to 37 % off; 1-5 % from 4 px on, like the levels' interior). No-data smaller than an output px there (a
lone pixel, a 1-px line) is blended in, not hidden. Zoom-in with a smooth `magnify` hides the kernel's reach
(radius / scale px) around label no-data.
`resampling` (used when z < max_zoom): nearest | bilinear | cubic | cubic_spline | lanczos | average
| mode | rms. `magnify` (used when z > max_zoom): nearest | bilinear | cubic | cubic_spline |
lanczos. At z == max_zoom pixels are copied 1:1. 409 if the dataset is not ready.

Client mapping (OpenLayers): projection units `pixels`, extent `[0, -H, W, 0]`, tile grid
`origin [0, 0]`, `resolutions = [2**max_zoom, ..., 1, 1/2, ...]`, `tileSize 256`; OL's `{z}/{x}/{y}`
then equal the server's. Image col = x, row = -y.

### Warped tiles (map view, globe, exports)

`GET /api/datasets/{id}/warp?crs=&bbox=minx,miny,maxx,maxy&width=256&height=256&bands=&resampling=&magnify=&format=f32|png[&render params]`

* `crs`: `native` | `lonlat` (geographic lat/lon on the dataset's own ellipsoid, east-positive,
  −180..180) | `EPSG:n` | proj4 string | WKT. `bbox` in that CRS's units (lonlat boxes may overshoot
  ±180/±90 by a pixel or two; outside areas come back transparent). width/height ≤ 1024.
* The server picks `resampling` when the output is coarser than the source, else `magnify`, and
  reads from the best overview level (never warps a whole full-res image for a small tile).
* `format=f32`: same binary layout and no-data rules (label no-data, blend ring) as `/tile`. `format=png`: RGBA
  PNG rendered with section 6.
* Warp results stay in a 256 MB in-memory LRU per (dataset, crs, bbox, size, bands, resampling, magnify), so a
  repeat or a `format=png` restyle of the same area skips the source read; deleting a dataset clears it.
* 400 for datasets without georeferencing.

### Export (save what you see, or the raw data)

`GET /api/datasets/{id}/export?kind=rendered|raw&format=png|jpg|tif&scale=1&window=col0,row0,col1,row1[&render params]`
→ file download (`Content-Disposition: attachment; filename="<name>_<kind>.<ext>"`).

* `window`: pixel window (default whole image); `scale` in (0, 1]: output = window × scale, downsampled
  with `resampling` (never upsampled).
* `rendered`: section 6 pipeline (bands/stretch/min/max/gamma/... exactly as the PNG render) → 8-bit
  RGBA PNG, RGB JPEG (nodata → black) or RGBA GeoTIFF that keeps georeferencing when present.
* `raw`: the selected `bands` (default all) with original dtype, nodata and scale/offset → GeoTIFF
  (DEFLATE, tiled; georeferencing + window transform kept). `png/jpg` not allowed for raw (400). At 100 % the
  values are an exact copy; below that GDAL's resampling, which leaves known no-data out. Reduced exports of JPEG
  2000 with no-data or of label no-data are read like the display (its pixel grid and blend ring), each band with
  its own mask: masked or no-data-blended pixels are written as nodata.
* Refuse outputs above 400 million samples (rendered: width × height; raw: × bands) with 413; the
  detail names the largest scale that fits. Stream the file
  from a temp file in the cache; delete it after sending.

### Pixel probe

`GET /api/datasets/{id}/pixel?col=&row=` or `?x=&y=&crs=lonlat|native|...` →
`{"col": 10, "row": 20, "inside": true, "values": [raw per band], "scaled": [raw*scale+offset], "nodata": [false], "lonlat": [lon, lat]|null, "native": [x, y]|null}` (all bands;
`nodata` is true where the band is masked or equals the label no-data).

## 6. Render pipeline (identical client- and server-side)

Settings (`web/js/render.js`): `bands` (1 → gray/colormap, 3 → RGB), `stretch`
(linear|equalize|log), `min[]`/`max[]` per displayed band (raw units), `gamma` (1 = none),
`brightness`, `contrast`, `exposure`, `saturation` (each −1..1, 0 = none), `invert`, `grayscale`
(→ saturation −1), `colormap` (name in `web/colormaps.json`), `resampling`, `magnify`,
`interpolate` (client only), `nodata` (transparent|black). Server query params are produced by
`renderParams(settings)`: `bands, stretch, min, max, gamma, brightness, contrast, exposure,
saturation, invert(0|1), colormap, resampling, magnify, nodata`.

Per pixel, per displayed band b:

1. invalid (nodata/mask) → output alpha 0 (`nodata=transparent`) or opaque black (`black`).
2. `t = clamp((v − min_b) / (max_b − min_b), 0, 1)` (if max == min: t = 0).
   `equalize`: `t` = piecewise-linear interpolation of v through the points (quantiles[k], k/32)
   from `/stats` (approximate stats; min/max ignored). `log`: `t = ln(1 + 255·t) / ln(256)`.
3. `invert`: `t = 1 − t`.
4. one band: `rgb = colormap(t)` (linear interpolation between the 33 stops; "gray" = t,t,t);
   three bands: `rgb = (t_r, t_g, t_b)`.
5. Adjustments in OpenLayers' order, each clamped to [0,1]:
   contrast `c`: `rgb = (c+1)·rgb − c/2`; exposure `e`: `rgb = (e+1)·rgb`;
   saturation `s` (S = s+1; sr=(1−S)·0.2126, sg=(1−S)·0.7152, sb=(1−S)·0.0722):
   `r' = (sr+S)·r + sg·g + sb·b`, `g' = sr·r + (sg+S)·g + sb·b`, `b' = sr·r + sg·g + (sb+S)·b`;
   gamma `γ`: `rgb = rgb^(1/γ)`; brightness `β`: `rgb = rgb + β`.
6. 8-bit output = round(255·rgb); alpha 255 for valid pixels.

## 7. Frontend contract

`web/index.html` (scaffold) loads vendor scripts, `css/base.css` + module CSS, and `js/app.js`,
which initialises **every** module at startup (hidden tabs too) by calling `init(sectionEl)`:
`explore.js` → `#tab-explore`, `search.js` → `#tab-search`, `viewer.js` → `#tab-viewer`. Modules
render their own DOM into their section (do not edit index.html). Hidden sections have zero size —
react to `tab:shown` (e.g. `map.updateSize()`).

Shared modules (scaffold): `api.js` (`getJSON`, `postJSON`, `putJSON`, `delJSON`, `qs`),
`bus.js` (`emit`, `on`), `bodies.js` (`getBodies`, `bodyById`, `bodyForInfo`), `render.js`
(`PRESETS`, `presetRange`, `applyPreset`, `defaultSettings`, `effectiveSaturation`,
`renderParams`, `getColormaps`).

Bus events:

| event | detail | emitted by → handled by |
|---|---|---|
| `nav` | `{tab}` | anyone → app.js |
| `tab:shown` | `{tab}` | app.js → modules |
| `search:prefill` | `{target?, region?, text?, connector?, collection?, collection_label?, level?, run?: bool}` | explore → search (search switches nothing itself; emitter also emits `nav`) |
| `viewer:open` | `{url?, path?, name?, companions?: [url], meta?: Result}` | explore/search → viewer (viewer opens it, emits `nav` to viewer) |
| `viewer:active` | `{id, status}` (status.info present) | viewer → anyone |
| `render:change` | `{id, settings}` | viewer → anyone |

Globe module API (owned by the globe builder, consumed by the viewer):

```js
// web/js/globe.js
export async function createGlobe(containerEl, { body }) // body = bodies.json entry; lazy-loads Cesium
  → {
    setBody(body),                  // rebuild ellipsoid (radii_m) + basemap for another body
    setDataset(status | null, settings), // drape dataset via /warp?crs=lonlat&format=png + renderParams(settings)
    setRender(settings),            // re-render draped imagery with new settings
    flyToDataset(),
    setTerrain(status | null, exaggeration), // optional: single-band DEM as terrain via /warp?format=f32
    resize(), destroy()
  }
```

Call `setBody` before `setDataset` when the body changes (it clears dataset and terrain). `setDataset`
may rebuild the Cesium viewer (dataset radii differ from the body's), so never hold Cesium objects.

Class prefixes: explore `.ex-`, search `.se-`, viewer `.vw-`, globe `.gl-`. Reuse `base.css`
variables and primitives (`.btn`, `.chip`, `.card`, `.input`, `.select`, `.badge`, `.row`,
`.col`, `.muted`, `.spinner`, `.progress`, `.dlg`).

## 8. Running and testing

* Run app: `.venv/Scripts/python.exe -m server --port <your port> --no-browser`. Builder ports:
  backend 8801, connectors 8811/8812/8813, explore/search 8821, viewer 8822, globe 8823. Stop your
  server when done.
* Python tests: `.venv/Scripts/python.exe -m pytest -q tests/<file>`; mark network tests with
  `@pytest.mark.network` and keep each under ~30 s. Prefer `fastapi.testclient.TestClient(app)`.
  `pytest.ini` is scaffold; there is no shared `conftest.py` — keep fixtures inside your own test files.
* JS syntax: `node --check web/js/<file>.js` (package.json has `"type": "module"`).
* Test data: `scripts/fetch_samples.py` downloads small public samples into `tests/data/`
  (gitignored).
