"""Live federated search: /api/connectors and /api/search dispatch to server/connectors/*."""
import asyncio

import httpx
from fastapi import APIRouter, HTTPException

from .config import USER_AGENT
from .connectors import REGISTRY
from .connectors.base import Query

router = APIRouter()
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(40, connect=15), follow_redirects=True,
                                    headers={"User-Agent": USER_AGENT})
    return _client


@router.get("/connectors")
def connectors(target: str | None = None):
    infos = [info for _, info in REGISTRY.values()]
    if target:
        infos = [i for i in infos if "*" in i["targets"] or target in i["targets"]]
    return infos


@router.get("/search")
async def search(connector: str, level: str = "product", text: str = "", target: str | None = None,
                 region: str | None = None, bbox: str | None = None, start: str | None = None,
                 end: str | None = None, collection: str | None = None, instrument: str | None = None,
                 mission: str | None = None, limit: int = 25, offset: int = 0):
    if connector not in REGISTRY:
        raise HTTPException(404, f"unknown connector {connector!r}")
    if level not in ("collection", "product"):
        raise HTTPException(400, "level must be 'collection' or 'product'")
    box = None
    if bbox:
        try:
            box = tuple(float(v) for v in bbox.split(","))
            assert len(box) == 4
        except (ValueError, AssertionError):
            raise HTTPException(400, "bbox must be W,S,E,N")
    q = Query(level=level, text=text.strip(), target=target, region=region, bbox=box, start=start,
              end=end, collection=collection, instrument=instrument, mission=mission,
              limit=max(1, min(limit, 200)), offset=max(0, offset))
    mod, _ = REGISTRY[connector]
    try:
        return await asyncio.wait_for(mod.search(connector, q, client()), 90)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"{connector} did not answer in time")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"{connector}: {type(e).__name__}: {e}")
    except ValueError as e:  # connectors raise ValueError for unsupported query combinations
        raise HTTPException(400, f"{connector}: {e}")
    except (KeyError, TypeError, IndexError) as e:  # upstream answered in a shape the connector does not know
        raise HTTPException(502, f"{connector}: unexpected upstream response ({type(e).__name__}: {e})")
