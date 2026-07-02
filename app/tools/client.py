"""Reusable async HTTP client for the Natural Nutrition mock backend.

One shared `httpx.AsyncClient` for the app lifetime (opened in FastAPI's
lifespan). Day 3 part 2 adds tool functions on top; for now this module
exposes only the client accessor + a one-shot health probe (which replaces
the disposable startup probe that lived in the old `main.py`).
"""
import logging

import httpx

from app.config import MOCK_BACKEND_URL

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


async def startup() -> None:
    """Open the shared client. Call once from FastAPI's lifespan."""
    global _client
    _client = httpx.AsyncClient(base_url=MOCK_BACKEND_URL, timeout=5.0)


async def shutdown() -> None:
    """Close the shared client. Call once from FastAPI's lifespan."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def get_client() -> httpx.AsyncClient:
    """Return the shared client. Raises if lifespan startup hasn't run."""
    if _client is None:
        raise RuntimeError("tools client not initialized — did lifespan startup() run?")
    return _client


async def health_probe() -> None:
    """Log whether the mock backend is reachable. Non-fatal on failure.

    TODO(day3-tools): real tool calls need bounded retry+backoff on 5xx per
    PLAN §5 TOOL-ERR. This one-shot probe stays a boot-time sanity check on
    the 6PN wire and doesn't need retries.
    """
    url = f"{MOCK_BACKEND_URL}/health"
    try:
        r = await get_client().get("/health")
        log.info(f"Mock backend reachable at {url} — {r.status_code} {r.text}")
    except Exception as e:
        log.warning(f"Mock backend NOT reachable at {url}: {e.__class__.__name__}: {e}")
