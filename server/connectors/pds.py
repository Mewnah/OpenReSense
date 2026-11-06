"""PDS4 Registry Search API connector (all PDS nodes; PDS4 archives only).

Collection = a PDS4 Product_Collection of type Data that references the target.
Product = a Product_Observational inside a collection (id = lidvid), with data/label file URLs.
API: https://nasa-pds.github.io/pds-api/  (served from pds.mcp.nasa.gov; pds.nasa.gov/api/search/1 redirects there)
"""
import re

import httpx

from .base import Query, asset, page, result
from ._util import check_times, json_of

API = "https://pds.mcp.nasa.gov/api/search/1/products"
TIMEOUT = httpx.Timeout(25, connect=10)
TARGET_LID = "urn:nasa:pds:context:target:"
MAX_WINDOW = 1000  # the registry has no 'start' parameter; offset paging is done by fetching offset+limit rows

_SATELLITES = {"earth": "moon", "mars": "phobos deimos",
               "jupiter": "io europa ganymede callisto amalthea himalia",
               "saturn": "mimas enceladus tethys dione rhea titan hyperion iapetus phoebe janus epimetheus prometheus pandora pan",
               "uranus": "miranda ariel umbriel titania oberon", "neptune": "triton proteus nereid",
               "134340_pluto": "charon nix hydra"}
# vocab target id -> context target LID suffixes ('*' = wildcard)
TARGETS = {
    "sun": ["star.sun"], "mercury": ["planet.mercury"], "venus": ["planet.venus"], "earth": ["planet.earth"],
    "mars": ["planet.mars"], "jupiter": ["planet.jupiter"], "saturn": ["planet.saturn"], "uranus": ["planet.uranus"],
    "neptune": ["planet.neptune"], "pluto": ["dwarf_planet.134340_pluto"], "ceres": ["dwarf_planet.1_ceres"],
    "vesta": ["asteroid.4_vesta"], "psyche": ["asteroid.16_psyche"], "lutetia": ["asteroid.21_lutetia"],
    "steins": ["asteroid.2867_steins"], "gaspra": ["asteroid.951_gaspra"], "ida": ["asteroid.243_ida"],
    "mathilde": ["asteroid.253_mathilde"], "annefrank": ["asteroid.5535_annefrank"],
    "dinkinesh": ["asteroid.152830_dinkinesh"], "donaldjohanson": ["asteroid.52246_donaldjohanson"],
    "eros": ["asteroid.433_eros"], "itokawa": ["asteroid.25143_itokawa"], "ryugu": ["asteroid.162173_ryugu"],
    "bennu": ["asteroid.101955_bennu"], "didymos": ["asteroid.65803_didymos"],
    "dimorphos": ["satellite.65803_didymos.dimorphos"], "toutatis": ["asteroid.4179_toutatis"],
    "halley": ["comet.1p_halley"], "borrelly": ["comet.19p_borrelly"], "wild-2": ["comet.81p_wild_2"],
    "tempel-1": ["comet.9p_tempel_1"], "hartley-2": ["comet.103p_hartley_2"],
    "67p": ["comet.67p_churyumov-gerasimenko"], "3i-atlas": ["comet.3i_atlas"],
    "arrokoth": ["trans-neptunian_object.486958_2014_mu69"],
    "asteroids": ["asteroid.*"], "comets": ["comet.*"], "kbos": ["trans-neptunian_object.*"], "rings": ["ring.*"],
}
for _parent, _names in _SATELLITES.items():
    for _n in _names.split():
        TARGETS[_n] = [f"satellite.{_parent}.{_n}"]
_LID_TO_ID = {s: t for t, suffixes in TARGETS.items() for s in suffixes if "*" not in s}
_CATEGORY_TO_ID = {"asteroid": "asteroids", "comet": "comets", "trans-neptunian_object": "kbos", "ring": "rings"}

CONNECTORS = [{
    "id": "pds", "name": "PDS4 Registry (all nodes)", "provider": "NASA Planetary Data System",
    "targets": list(TARGETS), "levels": ["collection", "product"], "supports": ["text", "time", "instrument"],
    "url": "https://pds.nasa.gov/datasearch/data-search/",
    "description": "Federated PDS4 registry: data collections and observational products from every PDS node "
                   "(Atmospheres, Geosciences, Imaging, Ring-Moon, Small Bodies, ...). PDS3-only archives are not "
                   "listed. Collection text filters need a target; product text matches the start of the product name.",
}]


async def _query(client: httpx.AsyncClient, q: str, fields: str, limit: int) -> dict:
    params = {"q": f"({q})", "fields": fields, "limit": limit, "sort": "lidvid"}
    r = await client.get(API, params=params, timeout=TIMEOUT)
    if r.status_code == 400:  # malformed q, e.g. quotes in free text
        raise ValueError("PDS registry rejected the query: " + r.text.split("message:")[-1].strip()[:200])
    r.raise_for_status()
    return json_of(r)


def _target_clause(target: str) -> str:
    if target not in TARGETS:
        raise ValueError(f"PDS registry connector has no mapping for target {target!r}")
    return " or ".join(f'ref_lid_target {"like" if "*" in s else "eq"} "{TARGET_LID}{s}"' for s in TARGETS[target])


def _target_of(row: dict, wanted: str | None) -> str | None:
    ids = []
    for t in row.get("targets", []):
        suffix = t["id"].removeprefix(TARGET_LID)
        tid = _LID_TO_ID.get(suffix) or _CATEGORY_TO_ID.get(suffix.split(".")[0])
        if tid:
            ids.append(tid)
    return wanted if wanted in ids else (ids[0] if ids else wanted)


def _mission_instrument(row: dict) -> tuple[str | None, str | None]:
    """Readable names from context LIDs ('mission.lunar_reconnaissance_orbiter', 'instrument:ocams.orex')."""
    mission = next((i["id"].rsplit(":", 1)[-1].split(".", 1)[-1].split("::")[0] for i in row.get("investigations", [])), None)
    if mission and "_" in mission:
        mission = mission.replace("_", " ").title()
    elif mission:
        mission = mission.upper()
    instrument = next((i["id"].rsplit(":", 1)[-1].split("::")[0].replace(".", " ").upper()
                       for i in row.get("observing_system_components", []) if ":instrument:" in i["id"]), None)
    return (None if mission == "NONE" else mission), instrument


def _iso(t: str | None, end: bool = False) -> str | None:
    if not t or t == "null":
        return None
    if len(t) == 10:
        t += "T23:59:59Z" if end else "T00:00:00Z"
    return t


def _window(q: Query) -> int:
    if q.offset + q.limit > MAX_WINDOW:
        raise ValueError(f"PDS registry results can only be paged to the first {MAX_WINDOW}; narrow the search")
    return q.offset + q.limit


def _lid_part(text: str) -> str:  # keeps user text from breaking out of the quoted q string
    return re.sub(r"[^\w.\-]", "", text.lower())


def _prop(row: dict, key: str) -> list:
    return row.get("properties", {}).get(key, [])


def _collection_result(row: dict, target: str | None) -> dict:
    mission, instrument = _mission_instrument(row)
    lidvid = row["id"]
    label = row["metadata"].get("label_url")
    return result(
        id=lidvid, connector="pds", level="collection", title=row["title"], description=lidvid,
        target=_target_of(row, target), mission=mission, instrument=instrument,
        start=_iso(row.get("start_date_time")), end=_iso(row.get("stop_date_time"), True),
        assets=[asset(label, role="metadata", openable=False)] if label else [], landing=label, collection=lidvid,
        extra={"lidvid": lidvid, "updated": row["metadata"].get("update_date_time")})


async def _collections(q: Query, client: httpx.AsyncClient) -> dict:
    clauses = ['product_class eq "Product_Collection"', 'pds:Collection.pds:collection_type eq "Data"']
    filters = [(q.text, ""), (q.instrument, ":instrument:"), (q.mission, ":investigation:")]
    words = [(w, kind) for text, kind in filters for w in (text or "").lower().split()]
    if words and not q.target:
        raise ValueError("PDS collection text/instrument/mission filters need a target (the registry has no full-text search)")
    if q.target:
        clauses.append(f"({_target_clause(q.target)})")
    # The registry cannot do substring matches, so filter locally over the first MAX_WINDOW collections.
    data = await _query(client, " and ".join(clauses), "lidvid", MAX_WINDOW if words else _window(q))
    rows, hits = data["data"], data["summary"]["hits"]
    notes = []
    if words:
        def matches(r, word, kind):  # word must start a token: 'iss' matches 'ISSNA' but not 'mission'
            text = " ".join([r["title"], r["id"], *(i["id"] for i in r.get("investigations", []) + r.get("observing_system_components", []) if kind in i["id"])])
            return re.search(r"(?<![a-z0-9])" + re.escape(word), text.lower())
        rows = [r for r in rows if all(matches(r, w, kind) for w, kind in words)]
        if hits > len(data["data"]):
            notes.append(f"text filter applied to the first {len(data['data'])} of {hits} collections")
        hits = len(rows)
    out = [_collection_result(r, q.target) for r in rows[q.offset:q.offset + q.limit]]
    end = q.offset + len(out)
    return page(out, hits, end if end < min(hits, MAX_WINDOW) else None, notes)


def _product_result(row: dict, target: str | None) -> dict:
    mission, instrument = _mission_instrument(row)
    names, refs = _prop(row, "ops:Data_File_Info.ops:file_name"), _prop(row, "ops:Data_File_Info.ops:file_ref")
    sizes, mimes = _prop(row, "ops:Data_File_Info.ops:file_size"), _prop(row, "ops:Data_File_Info.ops:mime_type")
    assets = [asset(ref, names[i] if i < len(names) else None, "data",
                    mimes[i] if i < len(mimes) else None, int(sizes[i]) if i < len(sizes) else None)
              for i, ref in enumerate(refs)]
    label = (_prop(row, "ops:Label_File_Info.ops:file_ref") or [None])[0]
    if label:
        size = (_prop(row, "ops:Label_File_Info.ops:file_size") or [None])[0]
        assets.append(asset(label, role="label", type="PDS4 label", size=int(size) if size else None))
    return result(
        id=row["id"], connector="pds", level="product", title=row["title"], description=row["id"],
        target=_target_of(row, target), mission=mission, instrument=instrument,
        start=_iso(row.get("start_date_time")), end=_iso(row.get("stop_date_time"), True),
        assets=assets, landing=row["metadata"].get("label_url"), extra={"lidvid": row["id"]})


async def _products(q: Query, client: httpx.AsyncClient) -> dict:
    if not (q.collection or q.target):
        raise ValueError("PDS product search needs a collection (lidvid from a collection-level result) or a target")
    clauses = ['product_class eq "Product_Observational"']
    if q.collection:
        lid, _, version = q.collection.partition("::")
        parent = (f'ops:Provenance.ops:parent_collection_identifier eq "{q.collection}"' if version
                  else f'ops:Provenance.ops:parent_collection_identifier like "{lid}::*"')
        clauses.append(parent)
        if q.text:  # leading wildcards are not supported by the registry, so match the start of the product name
            clauses.append(f'lid like "{lid}:{_lid_part(q.text)}*"')
    elif q.text:
        raise ValueError("PDS product text search needs a collection (matches the start of the product name)")
    target_clause = f"({_target_clause(q.target)})" if q.target else None
    if target_clause:
        clauses.append(target_clause)
    if q.start:
        clauses.append(f'pds:Time_Coordinates.pds:start_date_time ge "{_iso(q.start)}"')
    if q.end:
        clauses.append(f'pds:Time_Coordinates.pds:start_date_time le "{_iso(q.end, True)}"')
    if q.instrument:
        clauses.append(f'ref_lid_instrument like "{TARGET_LID.replace("target", "instrument")}{_lid_part(q.instrument)}.*"')
    stop = _window(q)
    fields = ",".join(["lidvid", "ref_lid_target", *(f"ops:{a}.ops:{b}" for a, b in (
        ("Data_File_Info", "file_ref"), ("Data_File_Info", "file_name"), ("Data_File_Info", "file_size"),
        ("Data_File_Info", "mime_type"), ("Label_File_Info", "file_ref"), ("Label_File_Info", "file_size")))])
    data = await _query(client, " and ".join(clauses), fields, stop)
    if q.collection and not data["summary"]["hits"]:  # some archives (LROC ...) omit the parent link; products share the collection's LID prefix
        clauses[clauses.index(parent)] = f'lid like "{lid}:*"'
        data = await _query(client, " and ".join(clauses), fields, stop)
    notes = []
    if q.collection and target_clause and not (q.text or q.start or q.end or q.instrument) and not data["summary"]["hits"]:  # target only declared on the collection, not on its products
        clauses.remove(target_clause)
        data = await _query(client, " and ".join(clauses), fields, stop)
        if data["summary"]["hits"]:
            notes.append(f"products do not reference {q.target} individually; showing the whole collection")
    out = [_product_result(r, q.target) for r in data["data"][q.offset:]]
    hits, end = data["summary"]["hits"], q.offset + len(out)
    return page(out, hits, end if end < min(hits, MAX_WINDOW) else None, notes)


async def search(conn_id: str, q: Query, client: httpx.AsyncClient) -> dict:
    check_times(q)
    return await (_collections if q.level == "collection" else _products)(q, client)
