"""Paths and user settings (settings live in cache/settings.json; never commit it)."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
CATALOG_DIR = ROOT / "catalog"
CACHE_DIR = Path(os.environ.get("ORS_CACHE", ROOT / "cache"))
SETTINGS_FILE = CACHE_DIR / "settings.json"
USER_AGENT = "OpenReSense/0.1 (local remote-sensing data browser)"
SECRET_KEYS = {"earthdata_token"}  # never echoed back by the API

try:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
except OSError as _ex:  # e.g. the portable folder unzipped into Program Files
    raise SystemExit(f"OpenReSense needs a folder it can write to, but cannot create {CACHE_DIR} ({_ex.strerror or _ex}). "
                     "Move the OpenReSense folder (e.g. to Documents) or set ORS_CACHE to a writable folder.")


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), "utf-8")
