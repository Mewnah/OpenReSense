"""Auto-discovers connector modules in this package (see base.py for the module contract)."""
import importlib
import logging
import pkgutil

log = logging.getLogger("openresense.connectors")
REGISTRY: dict[str, tuple] = {}  # connector id -> (module, info dict)

for _m in pkgutil.iter_modules(__path__):
    if _m.name == "base" or _m.name.startswith("_"):
        continue
    try:
        _mod = importlib.import_module(f"{__name__}.{_m.name}")
    except Exception:  # one broken connector must not take the app down
        log.exception("connector module %s failed to import", _m.name)
        continue
    for _info in getattr(_mod, "CONNECTORS", []):
        REGISTRY[_info["id"]] = (_mod, _info)
