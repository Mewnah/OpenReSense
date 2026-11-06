"""Validate catalog source files against CONTRACTS.md section 2.

Usage: python scripts/validate_catalog.py [file.json ...]   (default: catalog/sources/*.json)
Exit code 1 if any error. Warnings do not fail.
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOCAB = json.loads((ROOT / "catalog" / "vocab.json").read_text("utf-8"))
REQUIRED = ["id", "name", "provider", "agency", "kind", "url", "targets", "description"]
LIST_VOCABS = {k: VOCAB[k] for k in ("data_types", "spectral", "levels", "formats", "protocols")}
STR_LISTS = ["agency", "country", "missions", "instruments", "tags"]
KNOWN = set(REQUIRED) | set(LIST_VOCABS) | set(STR_LISTS) | {
    "links", "regions", "access", "resolution", "temporal", "connector", "verified", "notes"}
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
URL = re.compile(r"^(https?|ftp)://[^\s]+$")


def check(e: dict) -> tuple[list[str], list[str]]:
    err, warn = [], []
    for k in REQUIRED:
        if not e.get(k):
            err.append(f"missing {k}")
    for k in set(e) - KNOWN:
        err.append(f"unknown field {k!r}")
    if e.get("id") and not SLUG.match(e["id"]):
        err.append(f"bad id {e['id']!r} (kebab-case slug)")
    if e.get("url") and not URL.match(e["url"]):
        err.append(f"bad url {e['url']!r}")
    if e.get("kind") and e["kind"] not in VOCAB["kind"]:
        err.append(f"bad kind {e['kind']!r}")
    if e.get("access") and e["access"] not in VOCAB["access"]:
        err.append(f"bad access {e['access']!r}")
    for t in e.get("targets", []):
        if t not in VOCAB["targets"]:
            err.append(f"unknown target {t!r}")
    for r in e.get("regions", []):
        if r not in VOCAB["regions"]:
            err.append(f"unknown region {r!r}")
    if e.get("regions") and "earth" not in e.get("targets", []):
        err.append("regions given but 'earth' not in targets")
    for k, vocab in LIST_VOCABS.items():
        v = e.get(k, [])
        if not isinstance(v, list):
            err.append(f"{k} must be a list")
            continue
        for x in v:
            if x not in vocab:
                err.append(f"bad {k} value {x!r}")
    for k in STR_LISTS + ["targets", "regions"]:
        if k in e and not (isinstance(e[k], list) and all(isinstance(x, str) and x for x in e[k])):
            err.append(f"{k} must be a list of non-empty strings")
    for ln in e.get("links", []):
        if not (isinstance(ln, dict) and ln.get("label") and URL.match(ln.get("url", ""))):
            err.append(f"bad link {ln!r}")
    if e.get("verified"):
        try:
            date.fromisoformat(e["verified"])
        except ValueError:
            err.append(f"bad verified date {e['verified']!r}")
    else:
        warn.append("not verified")
    for k in ("data_types", "levels", "formats", "access"):
        if not e.get(k):
            warn.append(f"no {k}")
    if len(e.get("description", "")) < 40:
        warn.append("short description")
    return err, warn


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] or sorted((ROOT / "catalog" / "sources").glob("*.json"))
    seen: dict[str, str] = {}
    n_err = n = 0
    for f in files:
        try:
            doc = json.loads(f.read_text("utf-8"))
        except ValueError as ex:
            print(f"{f.name}: INVALID JSON: {ex}")
            n_err += 1
            continue
        entries = doc.get("sources") if isinstance(doc, dict) else None
        if not isinstance(entries, list):
            print(f"{f.name}: top level must be {{\"domain\": ..., \"sources\": [...]}}")
            n_err += 1
            continue
        for i, e in enumerate(entries):
            n += 1
            err, warn = check(e) if isinstance(e, dict) else (["entry is not an object"], [])
            sid = e.get("id", f"#{i}") if isinstance(e, dict) else f"#{i}"
            if sid in seen:
                err.append(f"duplicate id (also in {seen[sid]})")
            seen[sid] = f.name
            for m in err:
                print(f"{f.name}: {sid}: ERROR {m}")
            for m in warn:
                print(f"{f.name}: {sid}: warn {m}")
            n_err += len(err)
    print(f"{n} entries in {len(files)} file(s), {n_err} error(s)")
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
