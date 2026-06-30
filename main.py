"""
Natural Nutrition Voice Agent — Twilio ↔ OpenAI Realtime bridge.

This server handles:
  1. /incoming-call  — Twilio webhook, returns TwiML to open a Media Stream
  2. /media-stream   — WebSocket bridge: Twilio audio ↔ OpenAI Realtime API
"""

import os
import ssl
import json
import base64
import asyncio
import logging
from datetime import datetime, timezone

import certifi
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", 5050))

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is required — add it to .env")

# OpenAI Realtime
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime"
VOICE = "ash"  # Options: ash, ballad, coral, sage, verse — test a few on Day 9

SYSTEM_MESSAGE = """
You are Ashley, a friendly and efficient customer support agent for Natural Nutrition,
a health supplement subscription company. You're warm, concise, and helpful.

For now, greet the caller, ask how you can help, and have a natural conversation.
Keep your responses short — one or two sentences at a time, like a real phone call.
Don't ramble or give long lists. If you're unsure about something, say so honestly.

Remember: you're on a phone call, not writing an email. Be conversational.
""".strip()

# Events we want to log (keeps the console readable)
LOG_EVENTS = {
    "session.created",
    "session.updated",
    "input_audio_buffer.speech_started",
    "input_audio_buffer.speech_stopped",
    "response.output_audio.done",
    "response.done",
    "error",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s  %(message)s")
log = logging.getLogger("voice-agent")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Natural Nutrition Voice Agent")


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
async def media_stream(twilio_ws: WebSocket):
    """
    Bidirectional bridge:
      Twilio Media Stream  ←→  OpenAI Realtime API

    Audio format: g711 μ-law 8kHz (native to Twilio, supported by Realtime).
    Turn detection: server_vad (OpenAI handles endpointing).
    Barge-in: on speech_started → cancel OpenAI response + clear Twilio buffer.
    """
    await twilio_ws.accept()
    log.info("Twilio WebSocket connected")

    # Per-call state — this will grow to include auth, customer, etc.
    session = {
        "stream_sid": None,
        "call_sid": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "transcript_in": [],   # caller utterances (from transcription)
        "transcript_out": [],  # agent utterances (from transcription)
    }

    openai_ws = None

    try:
        # --- Connect to OpenAI Realtime (GA gpt-realtime — no beta header) ---
        # Explicit certifi CA bundle so TLS verification works regardless of
        # how Python was installed or where this is deployed.
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        openai_ws = await websockets.connect(
            OPENAI_REALTIME_URL,
            additional_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
            },
            ssl=ssl_context,
        )
        log.info("Connected to OpenAI Realtime API")

        # Configure the session — GA gpt-realtime schema: nested audio.input/output,
        # output_modalities (not modalities), format is an object, transcription
        # lives under audio.input.
        await openai_ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": "gpt-realtime",
                "output_modalities": ["audio"],
                "instructions": SYSTEM_MESSAGE,
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcmu"},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 500,
                        },
                        "transcription": {
                            "model": "gpt-4o-mini-transcribe",
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcmu"},
                        "voice": VOICE,
                    },
                },
            },
        }))

        # Run both directions concurrently
        await asyncio.gather(
            _twilio_to_openai(twilio_ws, openai_ws, session),
            _openai_to_twilio(openai_ws, twilio_ws, session),
        )

    except WebSocketDisconnect:
        log.info("Twilio WebSocket disconnected (caller hung up)")
    except websockets.exceptions.ConnectionClosed:
        log.info("OpenAI WebSocket closed")
    except Exception as e:
        log.error(f"Bridge error: {e}", exc_info=True)
    finally:
        # websockets v13+ removed `.closed`; close() is a safe no-op if already closed.
        if openai_ws is not None:
            await openai_ws.close()
        log.info(f"Call ended — stream_sid={session.get('stream_sid')}")


# ---------------------------------------------------------------------------
# Twilio → OpenAI
# ---------------------------------------------------------------------------
async def _twilio_to_openai(
    twilio_ws: WebSocket,
    openai_ws: websockets.ClientConnection,
    session: dict,
):
    """Forward caller audio from Twilio to OpenAI Realtime."""
    try:
        async for raw in twilio_ws.iter_text():
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "media":
                # Forward audio chunk (already base64 g711_ulaw)
                audio_payload = msg["media"]["payload"]
                await openai_ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": audio_payload,
                }))

            elif event == "start":
                session["stream_sid"] = msg["start"]["streamSid"]
                session["call_sid"] = msg["start"].get("callSid")
                log.info(
                    f"Stream started — stream_sid={session['stream_sid']}  "
                    f"call_sid={session['call_sid']}"
                )

            elif event == "stop":
                log.info("Twilio stream stopped")
                break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error(f"Twilio→OpenAI error: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# OpenAI → Twilio
# ---------------------------------------------------------------------------
async def _openai_to_twilio(
    openai_ws: websockets.ClientConnection,
    twilio_ws: WebSocket,
    session: dict,
):
    """Forward agent audio from OpenAI Realtime to Twilio, handle barge-in."""
    try:
        async for raw in openai_ws:
            msg = json.loads(raw)
            event_type = msg.get("type", "")

            # --- Audio chunk → send to Twilio ---
            if event_type == "response.output_audio.delta" and msg.get("delta"):
                await twilio_ws.send_json({
                    "event": "media",
                    "streamSid": session["stream_sid"],
                    "media": {"payload": msg["delta"]},
                })

            # --- Barge-in: caller started talking while agent is speaking ---
            elif event_type == "input_audio_buffer.speech_started":
                log.info("Barge-in detected — clearing Twilio buffer")
                # 1. Tell Twilio to stop playing queued audio
                await twilio_ws.send_json({
                    "event": "clear",
                    "streamSid": session["stream_sid"],
                })
                # 2. Cancel the in-progress OpenAI response
                await openai_ws.send(json.dumps({
                    "type": "response.cancel",
                }))

            # --- Capture transcripts for later ---
            elif event_type == "conversation.item.input_audio_transcription.completed":
                transcript = msg.get("transcript", "").strip()
                if transcript:
                    session["transcript_in"].append(transcript)
                    log.info(f"Caller: {transcript}")

            elif event_type == "response.output_audio_transcript.done":
                transcript = msg.get("transcript", "").strip()
                if transcript:
                    session["transcript_out"].append(transcript)
                    log.info(f"Agent:  {transcript}")

            # --- Error handling ---
            elif event_type == "error":
                log.error(f"OpenAI error: {msg.get('error', msg)}")

            # --- Selective logging ---
            elif event_type in LOG_EVENTS:
                log.info(f"OpenAI event: {event_type}")

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        log.error(f"OpenAI→Twilio error: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    log.info(f"Starting server on port {PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
