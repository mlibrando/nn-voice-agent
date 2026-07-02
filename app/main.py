"""FastAPI entrypoint — routes + lifespan.

Behavior parity with the pre-refactor `main.py`: same routes (`/`,
`/incoming-call`, `/media-stream`), same TwiML, same startup mock-backend
probe. The actual bridge logic lives in `app.bridge`.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse

from app.bridge import media_stream
from app.config import PORT
from app.tools import client as tools_client

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup: open the shared HTTP client + one-shot mock-backend reachability
    # probe. Day 3 part 2 uses this same client for the real tool calls.
    await tools_client.startup()
    await tools_client.health_probe()
    yield
    # Shutdown
    await tools_client.shutdown()


app = FastAPI(title="Natural Nutrition Voice Agent", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def health():
    return "<h1>Natural Nutrition Voice Agent</h1><p>Server is running.</p>"


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request):
    """Twilio webhook — returns TwiML that opens a bidirectional Media Stream."""
    host = request.headers.get("host", "localhost")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/media-stream" />
    </Connect>
</Response>"""
    return HTMLResponse(content=twiml, media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream_route(twilio_ws: WebSocket):
    await media_stream(twilio_ws)


if __name__ == "__main__":
    import uvicorn

    log.info(f"Starting server on port {PORT}")
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT)
