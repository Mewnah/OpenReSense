"""Generate synthetic test rasters for the viewer: .venv/Scripts/python.exe tests/data/viewer/make_samples.py"""
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

OUT = Path(__file__).parent
rng = np.random.default_rng(7)


def yx(h, w):
    return np.mgrid[0:h, 0:w].astype("float64")


def write(name, arr, **kw):
    arr = arr if arr.ndim == 3 else arr[None]
    with rasterio.open(OUT / name, "w", driver="GTiff", count=arr.shape[0], height=arr.shape[1], width=arr.shape[2],
                       dtype=arr.dtype, compress="deflate", **kw) as dst:
        dst.write(arr)
    print(name, arr.shape, arr.dtype)


# gray uint16 with nodata=0, WGS84 over Colorado; bright blobs + a nodata hole + a no-data corner
h, w = 800, 1200
y, x = yx(h, w)
img = 6000 + 2500 * np.sin(x / 90) * np.cos(y / 70) + 8000 * np.exp(-((x - 800) ** 2 + (y - 300) ** 2) / 8e3) + rng.normal(0, 150, (h, w))
img[(x - 300) ** 2 + (y - 500) ** 2 < 60 ** 2] = 0
img[:120, :200] = 0
write("gray_u16_nodata.tif", np.clip(img, 1, 65535).astype("uint16") * (img != 0).astype("uint16"), nodata=0, crs="EPSG:4326",
      transform=from_origin(-105.0, 41.0, 3 / w, 2 / h))

# RGB uint8 test chart in UTM 13N
h, w = 768, 1024
y, x = yx(h, w)
rgb = np.stack([(x / w * 255), (y / h * 255), 255 - (x / w * 255)]).astype("uint8")
rgb[:, (x.astype(int) % 128 == 0) | (y.astype(int) % 128 == 0)] = 255
rgb[:, 300:340, 400:440] = np.array([255, 0, 0])[:, None, None]
write("rgb_u8.tif", rgb, crs="EPSG:32613", transform=from_origin(500000, 4500000, 30, 30), photometric="RGB")

# lunar float32 DEM, custom equirectangular Moon CRS, nodata -3.4e38
h, w = 600, 900
y, x = yx(h, w)
dem = 1500 * np.sin(x / 70) * np.cos(y / 55) + 4000 * np.exp(-((x - 450) ** 2 + (y - 300) ** 2) / 2e4) - 2000 * np.exp(-((x - 200) ** 2 + (y - 400) ** 2) / 6e3)
dem = dem.astype("float32")
dem[:40, :] = -3.4e38
write("moon_dem_f32.tif", dem, nodata=-3.4e38,
      crs=rasterio.crs.CRS.from_string("+proj=eqc +lat_ts=0 +lat_0=0 +lon_0=0 +x_0=0 +y_0=0 +R=1737400 +units=m +no_defs"),
      transform=from_origin(-3_000_000, 1_000_000, 1000, 1000))

# 1-pixel checkerboard + ruler ticks, not georeferenced: exactness of 1:1 and nearest magnification
h, w = 200, 300
y, x = yx(h, w)
chk = (((x.astype(int) + y.astype(int)) % 2) * 255).astype("uint8")
chk[:, ::50] = 128
write("checker_u8.tif", chk)

# multispectral 7-band uint16 with wavelengths in tags, WGS84
h, w = 400, 500
y, x = yx(h, w)
bands = np.stack([(3000 + 1500 * np.sin(x / (40 + 12 * b)) * np.cos(y / 50) + 400 * b) for b in range(7)]).astype("uint16")
write("ms7_u16.tif", bands, crs="EPSG:4326", transform=from_origin(10.0, 50.0, 0.001, 0.001))

# crosses the antimeridian (170E..190E), WGS84: the map view must frame the footprint, not the whole world
h, w = 100, 200
y, x = yx(h, w)
write("antimeridian_u8.tif", (60 + 180 * (x / w) * (0.5 + 0.5 * np.sin(y / 9))).astype("uint8"), crs="EPSG:4326",
      transform=from_origin(170.0, 10.0, 0.1, 0.1))

# Antarctic polar-stereographic (EPSG:3031) DEM, 5 km pixels around the pole
h = w = 300
y, x = yx(h, w)
write("antarctic_dem_f32.tif", (2500 + 800 * np.cos(x / 25) * np.sin(y / 31) - 6 * np.hypot(x - 150, y - 150)).astype("float32"),
      crs="EPSG:3031", transform=from_origin(-750000, 750000, 5000, 5000))

# large 8000 x 8000 uint16, striped and without overviews (exercises the server's optimiser); pixel grid every 100 px
h = w = 8000
y, x = yx(h, w)
big = (20000 + 15000 * np.sin(x / 600) * np.cos(y / 450)).astype("uint16")
big[(x.astype(int) % 100 == 0) | (y.astype(int) % 100 == 0)] = 65000
write("big_8000_u16.tif", big, crs="EPSG:32613", transform=from_origin(400000, 4600000, 10, 10))
