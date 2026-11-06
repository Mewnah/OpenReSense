"""Catalog of imagery sources (CONTRACTS.md section 3): catalog/sources/*.json + catalog/user/*.json,
reloaded when any file's mtime changes; in-memory scoring search with faceted filters."""
import json
import logging
import re
import threading
from collections import Counter

from fastapi import APIRouter, HTTPException, Request

from . import config

log = logging.getLogger("openresense.catalog")
router = APIRouter()

# filter query param -> (facet name in the response, entry key)
FILTERS = {
    "target": ("targets", "targets"), "region": ("regions", "regions"), "agency": ("agency", "agency"),
    "kind": ("kind", "kind"), "data_types": ("data_types", "data_types"), "spectral": ("spectral", "spectral"),
    "levels": ("levels", "levels"), "formats": ("formats", "formats"), "access": ("access", "access"),
    "connector": ("connector", "connector"),
}
# search weights: name > identifying keywords (tags, missions, targets, regions) > organisations > description
W_NAME, W_KEYS, W_ORG, W_DESC = 10, 6, 3, 1

_lock = threading.Lock()
_state = {"sig": None, "entries": [], "by_id": {}, "target_names": {}, "regions": {}}


def _read(path):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as e:
        log.warning("skipping %s: %s", path, e)
        return None


def _list(v):
    return v if isinstance(v, list) else [] if v is None else [v]


def _index(entry, target_names, regions):
    """Lowercased search blobs, one per weight class."""
    tnames = [n for t in _list(entry.get("targets")) for n in target_names.get(t, [t])]
    rnames = [n for r in _list(entry.get("regions")) for n in [r, regions.get(r, {}).get("name", ""),
                                                                *regions.get(r, {}).get("aliases", [])]]
    keys = [entry["id"], *_list(entry.get("tags")), *_list(entry.get("missions")),
            *_list(entry.get("instruments")), *tnames, *rnames]
    org = [entry.get("provider", ""), *_list(entry.get("agency")), *_list(entry.get("country"))]
    blobs = [(W_NAME, entry["name"]), (W_KEYS, "\n".join(keys)), (W_ORG, "\n".join(map(str, org))),
             (W_DESC, entry.get("description") or "")]
    return [(w, text.lower()) for w, text in blobs]


def _load():
    root = config.CATALOG_DIR
    files = sorted([*(root / "sources").glob("*.json"), *(root / "user").glob("*.json"),
                    root / "vocab.json", root / "bodies.json"])
    sig = []
    for p in files:
        try:
            sig.append((str(p), p.stat().st_mtime_ns))
        except OSError:
            pass
    sig = tuple(sig)
    with _lock:
        if sig == _state["sig"]:
            return _state
        vocab = _read(root / "vocab.json") or {}
        regions = vocab.get("regions", {})
        target_names = {t: [t, label] for t, label in vocab.get("targets", {}).items()}
        for b in (_read(root / "bodies.json") or {}).get("bodies", []):
            target_names.setdefault(b["id"], [b["id"]])
            target_names[b["id"]] += [b["name"], *b.get("aliases", [])]
        entries, by_id = [], {}
        for p in files:
            if p.name in ("vocab.json", "bodies.json") and p.parent == root:
                continue
            data = _read(p)
            for e in (data or {}).get("sources", []) if isinstance(data, dict) else []:
                if not isinstance(e, dict) or not e.get("id") or not e.get("name"):
                    log.warning("%s: entry without id/name skipped", p.name)
                elif e["id"] in by_id:
                    log.warning("%s: duplicate id %s skipped", p.name, e["id"])
                else:
                    by_id[e["id"]] = e
                    entries.append((e, _index(e, target_names, regions)))
        _state.update(sig=sig, entries=entries, by_id=by_id, target_names=target_names, regions=regions)
        return _state


def _terms(q):
    """Quoted phrases stay whole; every term matches at a word start. Returns (prefix, whole-word, name-start) regexes."""
    out = []
    for m in re.finditer(r'"([^"]*)"|(\S+)', q.lower()):
        t = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
        if t:
            e = re.escape(t)
            out.append((re.compile(r"(?<!\w)" + e), re.compile(r"(?<!\w)" + e + r"(?!\w)"), re.compile("^" + e)))
    return out


def _score(terms, blobs):
    total = 0
    for prefix, whole, lead in terms:
        # a whole-word hit ("terra") outranks a prefix hit ("terrain"); a name that starts with the term edges ahead
        best = max((w * (1 if whole.search(text) else .5) + (2 if w == W_NAME and lead.match(text) else 0)
                    for w, text in blobs if prefix.search(text)), default=0)
        if not best:
            return 0
        total += best
    return total


KIND_RANK = {"dataset": 3, "archive": 3, "mission-site": 3, "raw-feed": 3, "map-service": 2, "portal": 2,
             "api": 2, "catalog": 1, "tool": 0}


def _focus(entry, names):
    """Tie-breaker: sources named after the selected body/region and covering few targets come before
    broad multi-body archives, and data sources before tools."""
    title = entry["name"].lower()
    named = any(re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", title) for n in names)
    return 4 * named + 2 / (len(_list(entry.get("targets"))) or 1) + KIND_RANK.get(entry.get("kind"), 1) / 3


def _csv(v):
    return {s.strip() for s in v.split(",") if s.strip()} if v else set()


def query(params: dict, limit=50, offset=0) -> dict:
    terms = _terms(params.get("q", ""))
    filters = {p: s for p in FILTERS if (s := _csv(params.get(p)))}
    matched = []  # (entry, score, names of filters it fails)
    for entry, blobs in _load()["entries"]:
        score = _score(terms, blobs) if terms else 1
        if not score:
            continue
        failed = [p for p, want in filters.items() if not want.intersection(map(str, _list(entry.get(FILTERS[p][1]))))]
        if len(failed) <= 1:
            matched.append((entry, score, failed))
    counters = {facet: Counter() for facet, _ in FILTERS.values()}
    for entry, _, failed in matched:
        # a facet counts entries passing every *other* filter, so its own selection doesn't hide siblings
        for p, (facet, key) in FILTERS.items():
            if not failed or failed == [p]:
                counters[facet].update(set(map(str, _list(entry.get(key)))))
    st = _load()
    names = {n.lower() for t in filters.get("target", ()) for n in st["target_names"].get(t, [t])}
    names |= {n.lower() for r in filters.get("region", ()) for n in [st["regions"].get(r, {}).get("name", r),
                                                                     *st["regions"].get(r, {}).get("aliases", [])]}
    hits = sorted((m for m in matched if not m[2]),
                  key=lambda m: (-m[1], -_focus(m[0], names), m[0]["name"].lower()))
    return {"total": len(hits), "results": [m[0] for m in hits[offset:offset + limit]],
            "facets": {f: dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))) for f, c in counters.items()}}


@router.get("/catalog")
def catalog_search(request: Request):
    p = dict(request.query_params)
    try:
        limit, offset = max(0, min(int(p.get("limit", 50)), 1000)), max(0, int(p.get("offset", 0)))
    except ValueError:
        raise HTTPException(400, "limit and offset must be integers")
    return query(p, limit, offset)


@router.get("/catalog/{source_id}")
def catalog_entry(source_id: str):
    entry = _load()["by_id"].get(source_id)
    if entry is None:
        raise HTTPException(404, f"unknown source {source_id!r}")
    return entry
