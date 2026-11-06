"""Shared types for live-search connectors (CONTRACTS.md section 4).

A connector module in this package exposes:
    CONNECTORS: list[dict]   # one info dict per connector id it serves
    async def search(conn_id: str, q: Query, client: httpx.AsyncClient) -> dict   # returns page(...)
Modules are auto-discovered by server/connectors/__init__.py; no registration edits needed.
"""
import json
from dataclasses import dataclass
from functools import lru_cache

from ..config import CATALOG_DIR

INFO_KEYS = {"id", "name", "provider", "targets", "levels", "supports", "url", "description",
             "needs_collection"}  # needs_collection: product search requires Query.collection
RESULT_KEYS = {"id", "connector", "level", "title", "description", "target", "mission", "instrument",
               "start", "end", "footprint", "bbox", "thumbnail", "browse", "assets", "landing",
               "collection", "extra"}
ASSET_ROLES = {"data", "label", "browse", "thumbnail", "metadata", "other"}

# Extensions the viewer can open (GDAL + pdr/astropy/pyhdf fallbacks). '.xml' only as a PDS4 label.
OPENABLE_EXT = {".tif", ".tiff", ".gtif", ".jp2", ".j2k", ".img", ".lbl", ".cub", ".fits", ".fit",
                ".fts", ".hdf", ".h4", ".hdf4", ".he4", ".h5", ".he5", ".hdf5", ".nc", ".nc4",
                ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".vic", ".vicar", ".qub", ".imq", ".ntf",
                ".nitf", ".rgb", ".dat", ".zarr"}


@dataclass
class Query:
    level: str = "product"           # "collection" | "product"
    text: str = ""                   # free-text keywords
    target: str | None = None        # body id from catalog/vocab.json "targets" (e.g. "titan")
    region: str | None = None        # earth region id from vocab.json "regions" (only when target == "earth")
    bbox: tuple[float, float, float, float] | None = None  # W,S,E,N degrees, east-positive, -180..180; W>E crosses antimeridian
    start: str | None = None         # ISO 8601
    end: str | None = None           # ISO 8601
    collection: str | None = None    # connector-specific id taken from a collection-level result's "collection"
    instrument: str | None = None
    mission: str | None = None
    limit: int = 25
    offset: int = 0


def is_openable(name: str, role: str = "data") -> bool:
    path = name.split("?", 1)[0].split("#", 1)[0].lower()
    ext = path[path.rfind("."):] if "." in path.rsplit("/", 1)[-1] else ""
    if ext == ".xml":
        return role == "label"
    return ext in OPENABLE_EXT and role in ("data", "label", "browse")


def asset(url: str, name: str | None = None, role: str = "data", type: str | None = None,
          size: int | None = None, openable: bool | None = None) -> dict:
    if role not in ASSET_ROLES:
        raise ValueError(f"bad asset role {role!r}")
    name = name or url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return {"url": url, "name": name, "role": role, "type": type, "size": size,
            "openable": is_openable(name if "." in name else url, role) if openable is None else openable}


def result(**kw) -> dict:
    unknown = set(kw) - RESULT_KEYS
    if unknown:
        raise ValueError(f"unknown result keys {unknown}")
    r = dict.fromkeys(RESULT_KEYS)
    r.update(level="product", assets=[], extra={})
    r.update(kw)
    return r


def page(results: list[dict], total: int | None = None, next_offset: int | None = None,
         notes: list[str] | None = None) -> dict:
    return {"results": results, "total": total, "next_offset": next_offset, "notes": notes or []}


# ---- helpers shared by several connectors -------------------------------------------------------

@lru_cache(maxsize=1)
def vocab() -> dict:
    return json.loads((CATALOG_DIR / "vocab.json").read_text("utf-8"))


def query_bbox(q: Query):
    """Explicit bbox, else the vocab bbox of q.region ("global" means no spatial filter). W>E crosses the antimeridian."""
    if q.bbox:
        return q.bbox
    if not q.region:
        return None
    region = vocab()["regions"].get(q.region)
    if region is None:
        raise ValueError(f"unknown region {q.region!r}")
    return None if q.region == "global" else tuple(region["bbox"])


def rect(w, s, e, n) -> dict:
    """GeoJSON polygon for a W,S,E,N box (MultiPolygon split at +-180 when W>E)."""
    ring = lambda a, b: [[a, s], [b, s], [b, n], [a, n], [a, s]]  # noqa: E731
    if w > e:
        return {"type": "MultiPolygon", "coordinates": [[ring(w, 180)], [ring(-180, e)]]}
    return {"type": "Polygon", "coordinates": [ring(w, e)]}


def bounds(geom):
    """[W,S,E,N] of polygon geometry; W>E when an edge jumps across +-180."""
    rings = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    pts = [p for poly in rings for ring in poly for p in ring]
    lons, lats = [p[0] for p in pts], [p[1] for p in pts]
    jumps = any(abs(r[i][0] - r[i + 1][0]) > 180 for poly in rings for r in poly for i in range(len(r) - 1))
    if jumps:
        pos, neg = [x for x in lons if x >= 0], [x for x in lons if x < 0]
        return [min(pos), min(lats), max(neg), max(lats)]
    return [min(lons), min(lats), max(lons), max(lats)]


@lru_cache(maxsize=1)
def _target_names() -> dict[str, str]:
    out: dict[str, str] = {}
    for tid, label in vocab()["targets"].items():
        for n in (tid, tid.replace("-", " "), label.split(" (")[0], label.split("/")[0]):
            out.setdefault(n.lower(), tid)
    return out


def known_target(name: str | None) -> str | None:
    """vocab target id for a body name (case-insensitive), or None."""
    return _target_names().get((name or "").strip().lower())


def target_id(name: str) -> str:
    """vocab target id for a body name, else the name itself."""
    return known_target(name) or name.strip()


def target_variants(target: str) -> list[str]:
    """Spellings an archive may store for a vocab target (case variants of id and label forms)."""
    label = vocab()["targets"].get(target)
    names = {target, target.replace("-", " ")}
    if label:
        names |= {label.split(" (")[0], label.split("/")[0]}
    return sorted({v for n in names if n.isascii() for v in (n, n.lower(), n.upper(), n.title())})
