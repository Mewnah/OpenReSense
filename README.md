# OpenReSense

One local app to find, open and inspect raw remote-sensing imagery of Earth and every other
body: a searchable catalog of 1,014 archives, live search across 16 archive APIs, and a viewer
that shows the original pixels in 2D, on a map or on a 3D globe.

## Download (Windows 10/11, 64-bit)

Get the [latest release](../../releases/latest); nothing else to install (no Python, no Node.js):

* **`OpenReSense-<version>-setup.exe`**: installs for your user account (no admin rights) with a Start-menu
  entry; uninstall it in Settings > Apps. Your downloads and settings survive updates.
* **`OpenReSense-<version>-portable.zip`**: unzip into a short folder you can write to (e.g. `C:\` or
  Documents; `README-portable.txt` gives the exact path limit) and double-click **`OpenReSense.exe`**.

OpenReSense opens in its own window; closing the window stops it. The window uses the Microsoft Edge WebView2
Runtime that comes with Windows 11 and updated Windows 10; without it (or with `OpenReSense.exe --browser`) the app
opens in your web browser instead. The files are not code-signed yet: if Windows says "Windows protected your PC",
click *More info*, then *Run anyway*. `SHA256SUMS.txt` on the release page lets you check a download.

## Run from source

Double-click **`run.bat`**, or run in PowerShell:

```bash
.\run.ps1
```

The first run creates `.venv` (Python 3.12) and installs the map/globe libraries with npm
(Node.js needed once). It then opens <http://127.0.0.1:8765/>. Options: `.\run.ps1 --port 9000 --no-browser`.
macOS and Linux are untested; the same steps by hand may work there (`python3.12 -m venv .venv`,
`.venv/bin/pip install -r requirements.txt`, `npm install`, `.venv/bin/python -m server`).

## The three tabs

**Explore** lists every catalogued source (agencies, universities, mission teams, map services):
75 targets from the Sun to comets and 27 Earth regions (Antarctica, Arctic, Amazon, ...).
Click a target such as *Titan* or *Moon*, or a region such as *Antarctica*, to see:

* every source covering it, ranked so sources dedicated to that body come first;
* live dataset strips pulled from the archive APIs at that moment (e.g. OPUS "Cassini ISS - Titan:
  45,170 observations"). Click a strip to list its products in Search;
* facets for agency, data type, spectral range, processing level, format and access.

**Search** queries the archives live and returns products you can open:

| Connector | Covers |
|---|---|
| PDS Orbital Data Explorer (ODE) | Mars, Moon, Mercury, Venus orbiters (HiRISE, CTX, CRISM, LROC, ...) |
| PDS Imaging Atlas, PDS4 Registry, OPUS | All PDS imaging (Cassini, Voyager, Galileo, MESSENGER, rovers, ...) |
| Mars rover raw images | Perseverance, Curiosity, Ingenuity, InSight raw frames |
| VESPA EPN-TAP | Europlanet services incl. ESA PSA |
| MAST | HST, JWST, HLA, IUE observations of solar-system targets |
| NASA CMR, ASF | All NASA Earthdata collections, SAR (Sentinel-1, ALOS, ...) |
| STAC: Earth Search, Planetary Computer, Copernicus Data Space, USGS Landsat, USGS Astrogeology, DEA, DE Africa | Sentinel, Landsat, DEMs, planetary maps |

Pick a target, region or bbox, dates and keywords. Archives that only search inside a dataset
list their datasets first ("Browse products"). Each result has thumbnails, footprints on a map,
links to the source page, and **Open in viewer** (labels and companion files are fetched too).

Every search is kept: **← Back** (or Alt+←) returns to the previous results instantly, e.g. from a dataset's
products to the dataset list, or to Explore; → goes forward again. Drag the divider beside the map to resize it
(double-click resets it).

**Viewer** opens a URL, a local path (or **Browse...**), or dropped files (drop a `.LBL` and an
`.IMG` together).

* **Image view**: the raster in its own pixel grid. At 1:1 every screen pixel is one data pixel
  (*Nearest - true pixels*). Pick the zoom-out method (average, nearest, bilinear, cubic, Lanczos,
  mode, RMS) and the zoom-in method separately. The status bar always says which is in use.
* **Map view**: native projection, lon/lat, polar stereographic or Web Mercator, over the
  body's basemaps, with a graticule and scale bar.
* **Globe**: CesiumJS globe with the body's true radii, the dataset draped on it, and DEMs as
  3D terrain with exaggeration.
* **Adjustments** (live, no refetch): stretch presets (data-type range, min-max, 0.1-99.9 % ...,
  mean ± 2σ/3σ, manual on the histogram), linear / histogram-equalize / log, gamma, brightness,
  contrast, exposure, saturation, invert (`i`), black & white (`g`), 15 colormaps, RGB band
  composites, no-data transparent or black, reset (`r`). Keys `0` fit, `1` 1:1, `+`/`-` zoom.
* **Pixel probe**: click for raw and scaled values of every band, with lon/lat. Multi-band cubes
  show a spectrum plot (by wavelength when the file has them).
* **Info**: driver, size, data type, no-data, CRS, bounds, full PDS/ISIS/VICAR label or FITS header.
* **Export**: the rendered image (PNG, JPEG or GeoTIFF exactly as displayed) or the raw data
  (GeoTIFF, original data type, no-data, scale/offset and georeferencing), whole image or current
  view, at 100 % down to 6.25 %. **Save view PNG** grabs the map as shown.

Faithfulness: tiles carry the raw values as float32 and all display processing happens on the
GPU, so adjustments never alter the data. Server-rendered PNGs (globe, exports) use the same
formulas, matching within 1/255.

## Formats

GDAL 3.12 (via rasterio) reads GeoTIFF/COG, JPEG 2000, PDS3 (attached or detached labels), PDS4,
ISIS2/3 cubes, VICAR, HDF5, netCDF, ENVI, NITF, Sentinel SAFE, Zarr, PNG/JPEG and more. When GDAL
cannot, it falls back to `pdr` (odd PDS3/PDS4 layouts), `astropy` (FITS, keeping BSCALE/BZERO
and BLANK) or `pyhdf` (HDF4, e.g. MODIS/ASTER). Those are converted once to a lossless COG in the
cache. Files with several arrays (HDF/netCDF) show a subdataset picker. Large untiled files get a
background optimized copy; tiles work meanwhile.

Remote GeoTIFFs that are cloud-optimized are streamed (nothing downloaded). Everything else is
downloaded once, with progress, into `cache/downloads/`; a dropped connection is retried and resumes where it
stopped.

## Logins

* **NASA Earthdata** (CMR, ASF, LP DAAC, ...): create a token at
  <https://urs.earthdata.nasa.gov/> → *Generate Token*, then paste it in **⚙ Settings**. It is sent
  only to NASA Earthdata/ASF hosts, over https.
* Planetary Computer assets are signed automatically.
* Copernicus Data Space and USGS EROS (LandsatLook) downloads need their own logins, which are not
  supported yet. Search works; opening their files fails with a 401 or "web page instead of the
  file" message.

## Add your own sources

Create `catalog/user/<anything>.json` with `{"sources": [ ... ]}`. Entries use the schema in
[CONTRACTS.md](CONTRACTS.md) section 2 (`id`, `name`, `url`, `targets`, `data_types`, ...). The
app reloads the catalog automatically when a file changes. Check entries with:

```bash
.venv/Scripts/python.exe scripts/validate_catalog.py catalog/user/my-sources.json
```

In an installed or portable release, use `python\python.exe scripts\validate_catalog.py ...` from the app folder.

## Where things live

| Path | What |
|---|---|
| `server/` | FastAPI backend: catalog search, live connectors (`server/connectors/`), raster engine (`server/raster/`) |
| `web/` | Frontend (plain ES modules, no build step); OpenLayers, CesiumJS and proj4 come from `node_modules` |
| `catalog/` | `sources/*.json` (the catalog), `bodies.json` (radii, basemaps), `vocab.json` (targets, regions, vocabularies) |
| `cache/` | Downloads, conversions, dataset list (safe to delete while the app is stopped) |
| `installer/` | `launcher.cs` (`OpenReSense.exe`: the app window), icon, Inno Setup script for the installer |
| `scripts/build_release.ps1` | Builds the release into `dist/` (see *Making a release*) |
| `.github/workflows/release.yml` | Builds and publishes a GitHub Release for every `v*` tag |
| `CONTRACTS.md` | API and data contracts between all parts |

The trash-can button next to a dataset removes it and deletes its cached files. Your own files
opened by path are never modified or deleted.

## Security

The server listens on 127.0.0.1 only and can read local files, so it accepts only
`Host: 127.0.0.1/localhost` (blocks DNS rebinding) and refuses state-changing requests sent by
other web sites. Names taken from remote URLs and labels can never write outside the cache.

## Tests

```bash
.venv/Scripts/python.exe -m pytest -q -m "not network"
```

`-m network` runs the live archive tests (slower; they depend on the archives being up).
`scripts/fetch_samples.py` downloads the real-format samples used by `tests/test_raster_samples.py`.

## Making a release

Set the new version in `package.json`, commit, then push a tag with the same version:

```bash
git tag v0.1.0
```

```bash
git push origin v0.1.0
```

GitHub Actions (`.github/workflows/release.yml`) runs the offline tests on Windows, builds the installer, the
portable zip and `SHA256SUMS.txt`, and publishes them as a GitHub Release with generated notes ("Run workflow"
on the Actions tab builds the same files as a downloadable artifact without publishing).

To build the same files locally (needs the source setup above, plus [Inno Setup 6](https://jrsoftware.org/isdl.php)
for the installer; the first build downloads Python's embeddable package, the wheels and the WebView2 SDK from nuget.org):

```bash
powershell -ExecutionPolicy Bypass -File scripts\build_release.ps1 -Installer
```

The release bundles Python's embeddable package with the exact package versions the tests use, the app, the web
libraries and `OpenReSense.exe`, compiled from `installer/launcher.cs` against Microsoft's WebView2 SDK (fetched from
nuget.org at a pinned version and checked). Code signing (e.g. the free
[SignPath Foundation](https://signpath.org) program for open-source projects) would remove the SmartScreen warning.

## Known limits

* Zoomed out, JPEG 2000 products (e.g. HiRISE) hide a 3-px band next to no-data (12 px where a
  no-data wedge meets the image edge): the codec's reduced resolutions blend no-data into those values.
  No-data specks smaller than a screen pixel are blended in, not hidden. At 1:1 and zoomed in, every pixel
  shows exactly.
* Exact statistics (full scan) of a big JPEG 2000 product take minutes (HiRISE RDR: ~2.5 min); the viewer keeps
  working meanwhile.
* FITS world coordinates (WCS) are not used: FITS files open in pixel space.
* Magellan FMIDR/FBIDR sinusoidal labels open without georeferencing.
* Rotated or GCP-only rasters (e.g. Sentinel-1 GRD) warp from a decimated whole-image read.
* Web Mercator is not offered for datasets crossing the antimeridian (lon/lat and native work).
* Live archives vary: VESPA can take ~15 s to list services, OPUS has no footprints, some PDS
  Atlas entries point at files the archive no longer serves.
* The release needs a folder with a short path (about 120 characters at most; the installer checks it and
  `README-portable.txt` states the exact limit): deeper, Windows cannot create some of its files.

## License

MIT (see [LICENSE](LICENSE)). The releases also contain Python, the packages from `requirements.txt` with the
native libraries they bundle (GDAL, GEOS, PROJ, ...), OpenLayers, CesiumJS and proj4js, each under its own license:
see [THIRD-PARTY-NOTICES.txt](THIRD-PARTY-NOTICES.txt). The body thumbnails in `web/img/bodies` come with their own
credits ([CREDITS.md](web/img/bodies/CREDITS.md)); two ESA images there are CC BY-SA 3.0 IGO.
