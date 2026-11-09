"""FastAPI app: wires routers and static files. Feature code lives in the routers (see CONTRACTS.md)."""
import hashlib
import json
import math
import mimetypes

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import catalog, datasets, search
from .config import CATALOG_DIR, ROOT, SECRET_KEYS, WEB_DIR, load_settings, save_settings


def _finite(v):
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_finite(x) for x in v]
    return v


class FiniteJSONResponse(JSONResponse):
    """NaN/Infinity are not JSON (browsers reject them, Starlette raises): they become null."""
    def render(self, content) -> bytes:
        try:
            return super().render(content)
        except ValueError:
            return super().render(_finite(content))


class SameOriginWrites:
    """Refuse state-changing requests sent by other web pages: a cross-site form POST (a "simple" request
    with no CORS preflight) could otherwise upload files or open local paths. Non-browser clients send no Origin."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            hdr = dict(scope["headers"])
            origin = hdr.get(b"origin")
            if origin is not None and origin != b"http://" + hdr.get(b"host", b""):
                return await PlainTextResponse("cross-origin request refused", 403)(scope, receive, send)
        await self.app(scope, receive, send)


app = FastAPI(title="OpenReSense", default_response_class=FiniteJSONResponse)
app.add_middleware(SameOriginWrites)
# The API can read local files: reject any other Host header so a website using DNS rebinding
# (evil.example -> 127.0.0.1) cannot read our responses. "testserver" is FastAPI's TestClient.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
# compresslevel 1: catalog JSON shrinks a lot, float tiles barely compress and must stay cheap.
app.add_middleware(GZipMiddleware, minimum_size=2048, compresslevel=1)
app.include_router(catalog.router, prefix="/api")
app.include_router(search.router, prefix="/api")
app.include_router(datasets.router, prefix="/api")


def _read_json(name: str) -> dict:
    try:
        return json.loads((CATALOG_DIR / name).read_text("utf-8"))
    except FileNotFoundError:
        return {}


@app.get("/api/vocab")
def vocab():
    return _read_json("vocab.json")


@app.get("/api/bodies")
def bodies():
    return {"bodies": _read_json("bodies.json").get("bodies", []),
            "regions": _read_json("vocab.json").get("regions", {})}


@app.get("/api/instance")
def instance():
    """Which app folder this server runs from, as a hash (not the path): OpenReSense.exe only reuses a server of its own
    folder, never another copy's or another user's that happens to listen on the port."""
    return {"root": hashlib.sha1(str(ROOT).lower().encode()).hexdigest()[:16]}


@app.get("/api/settings")
def get_settings():
    return {k: ("set" if k in SECRET_KEYS and v else v) for k, v in load_settings().items()}


@app.put("/api/settings")
def put_settings(body: dict):
    s = load_settings()
    # "set" is the masked placeholder we hand out; keep the stored secret when it comes back.
    s.update({k: v for k, v in body.items() if not (k in SECRET_KEYS and v == "set")})
    save_settings(s)
    return get_settings()


class AppFiles(StaticFiles):
    """Revalidate app files on each load (a cheap 304 when unchanged) so an update never runs stale ES modules."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# Python reads these types from the Windows registry, where .js is sometimes text/plain: browsers then refuse to run
# the app's modules (blank page on that PC).
for _ext, _type in {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".wasm": "application/wasm",
                    ".json": "application/json", ".svg": "image/svg+xml"}.items():
    mimetypes.add_type(_type, _ext)

_nm = ROOT / "node_modules"
app.mount("/vendor/ol", StaticFiles(directory=_nm / "ol"), name="ol")
app.mount("/vendor/cesium", StaticFiles(directory=_nm / "cesium" / "Build" / "Cesium"), name="cesium")
app.mount("/vendor/proj4", StaticFiles(directory=_nm / "proj4" / "dist"), name="proj4")
app.mount("/", AppFiles(directory=WEB_DIR, html=True), name="web")
