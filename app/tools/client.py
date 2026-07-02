"""Shared async HTTP client for the Natural Nutrition mock backend.

Single httpx.AsyncClient opened at FastAPI lifespan startup and closed at
shutdown. Every tool handler routes through `call_mock`, which:

- Retries transient failures (503 + network errors) up to `max_attempts`
  with exponential backoff (200ms, 400ms). Cap = 3 attempts total.
- Fails immediately on contract errors (400/404/409) — those are the caller's
  fault, retrying won't help.
- Preserves the mock's `{"error": {"code","message"}}` envelope and raises a
  typed `ToolError` handlers can convert into a structured result for the model.

Worst-case retry delay: ~600ms. Combined with the mock's ambient 300–1500ms
per attempt and the +1200ms on `/subscriptions`, a hit-then-retry can sit in
this layer for 2–5s — that's exactly the dead-air window §3 filler covers,
and Day 6 wires filler to fire on tool dispatch (not here).
"""
import asyncio
import logging

import httpx

from app.config import MOCK_BACKEND_URL

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

# httpx exceptions worth retrying — transient network faults. Don't include
# HTTPStatusError; we branch on status_code ourselves so 4xx never retries.
_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)


class ToolError(Exception):
    """Raised by `call_mock` on final failure. Handlers convert to a
    structured `{ok: false, error: {code, message}}` result for the model.
    Preserves the mock's error envelope where present."""

    def __init__(self, code: str, message: str, status: int | None = None):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"[{code}] {message}")


async def startup() -> None:
    """Open the shared client. Call once from FastAPI's lifespan."""
    global _client
    _client = httpx.AsyncClient(base_url=MOCK_BACKEND_URL, timeout=10.0)


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


async def call_mock(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    max_attempts: int = 3,
) -> dict:
    """Call the mock. Retry 503 + transient network errors up to `max_attempts`.

    Returns the parsed JSON body on 2xx. Raises `ToolError` on final failure
    (network exhausted, 503 exhausted, or any 4xx contract error).
    """
    delay = 0.2

    for attempt in range(1, max_attempts + 1):
        try:
            r = await get_client().request(method, path, params=params, json=json_body)
        except _RETRYABLE_EXCEPTIONS as e:
            log.warning(
                f"{method} {path} attempt {attempt}/{max_attempts} — "
                f"network error: {e.__class__.__name__}: {e}"
            )
            if attempt == max_attempts:
                raise ToolError(
                    "network_unavailable",
                    f"Backend not reachable ({e.__class__.__name__}).",
                ) from e
            await asyncio.sleep(delay)
            delay *= 2
            continue

        if r.status_code < 400:
            return r.json()

        # Parse mock's error envelope { "error": { "code", "message" } } if present.
        try:
            body = r.json()
        except Exception:
            body = {}
        err = body.get("error", {}) if isinstance(body, dict) else {}
        code = err.get("code") or f"http_{r.status_code}"
        message = err.get("message") or r.text or f"HTTP {r.status_code}"

        # Retry only on 503. 400/404/409 are contract failures — retry can't fix.
        if r.status_code == 503 and attempt < max_attempts:
            log.warning(
                f"{method} {path} attempt {attempt}/{max_attempts} — "
                f"503 {code}: {message}"
            )
            await asyncio.sleep(delay)
            delay *= 2
            continue

        raise ToolError(code, message, status=r.status_code)

    # Loop guarantees a return or raise; this is unreachable.
    raise ToolError("exhausted", "retry loop exited without a result")


async def health_probe() -> None:
    """Log whether the mock is reachable at boot. Non-fatal on failure —
    the app should still accept calls even if the mock is down at startup."""
    url = f"{MOCK_BACKEND_URL}/health"
    try:
        r = await get_client().get("/health")
        log.info(f"Mock backend reachable at {url} — {r.status_code} {r.text}")
    except Exception as e:
        log.warning(f"Mock backend NOT reachable at {url}: {e.__class__.__name__}: {e}")
