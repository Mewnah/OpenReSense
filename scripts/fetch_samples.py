"""Download small public sample rasters into tests/data/<name>/ (idempotent; the folder is gitignored).

Usage: .venv/Scripts/python.exe scripts/fetch_samples.py
"""
import sys
from pathlib import Path

import httpx

DATA = Path(__file__).resolve().parent.parent / "tests" / "data"
GDAL = "https://raw.githubusercontent.com/OSGeo/gdal/master/autotest/gdrivers/data"
# Cassini ISS NAC image of Titan (512x512, ~0.5 MB) from the PDS Rings Node archive, PDS3 detached label.
RINGS = "https://opus.pds-rings.seti.org/holdings/volumes/COISS_2xxx/COISS_2008/data/1481618078_1481626758"
# USGS Astrogeology STAC item asset (Mars THEMIS controlled mosaic); used by the streaming test, not downloaded.
COG_URL = "https://astrogeo-ard.s3.us-west-2.amazonaws.com/mars/mo/themis/controlled_mosaics/mc29_eridania_nir/mc29_eridania_nir.tif"

SAMPLES = {
    "pds3": [f"{GDAL}/pds/LDEM_4.LBL", f"{GDAL}/pds/LDEM_4.IMG"],
    "pds4": [f"{GDAL}/pds4/byte_pds4_cart_1700.xml", f"{GDAL}/pds4/byte_pds4_cart_1700.img"],
    "isis3": [f"{GDAL}/isis3/isis3_detached.lbl", f"{GDAL}/isis3/isis3_detached.cub"],
    "vicar": [f"{GDAL}/vicar/vicar_int16.vic"],
    "fits": [f"{GDAL}/fits/image_in_second_hdu.fits", f"{GDAL}/fits/byte_merc.fits"],
    "hdf4": [f"{GDAL}/hdf4/hdifftst2.hdf"],
    "hdf5": [f"{GDAL}/hdf5/deflate.h5"],
    "netcdf": [f"{GDAL}/netcdf/byte.nc"],
    "cassini_titan": [f"{RINGS}/N1481618305_4.LBL", f"{RINGS}/N1481618305_4.IMG"],
}


def main() -> int:
    failed = 0
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30, read=120),
                      headers={"User-Agent": "OpenReSense/0.1 sample fetcher"}) as c:
        for name, urls in SAMPLES.items():
            d = DATA / name
            d.mkdir(parents=True, exist_ok=True)
            for url in urls:
                dest = d / url.rsplit("/", 1)[-1]
                if dest.exists() and dest.stat().st_size:
                    continue
                try:
                    r = c.get(url)
                    r.raise_for_status()
                    dest.write_bytes(r.content)
                    print(f"{name}/{dest.name}  {len(r.content)} bytes")
                except httpx.HTTPError as ex:
                    failed += 1
                    print(f"FAILED {url}: {ex}", file=sys.stderr)
    (DATA / "cog_url.txt").write_text(COG_URL + "\n")
    print(f"done, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
