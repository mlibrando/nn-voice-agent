"""Twilio Media Stream ↔ OpenAI Realtime bridge.

Handles one call: opens both websockets, forwards audio in both directions,
and manages barge-in (caller starts talking → cancel any in-flight OpenAI
response and clear the Twilio audio buffer).
"""
import asyncio
import json
import logging
import ssl

import certifi
import websockets
from fastapi import WebSocket, WebSocketDisconnect

from app.config import (
    LOG_EVENTS,
    OPENAI_API_KEY,
    OPENAI_REALTIME_URL,
    SESSION_UPDATE_PAYLOAD,
)
from app.session import new_session

log = logging.getLogger(__name__)


async def media_stream(twilio_ws: WebSocket) -> None:
    """Bidirectional bridge for a single call:
      Twilio Media Stream  ←→  OpenAI Realtime API

    Audio format: g711 μ-law 8kHz (native to Twilio, supported by Realtime).
    Turn detection: server_vad (OpenAI handles endpointing).
    Barge-in: on speech_started, if a response is in flight → cancel OpenAI
    response + clear Twilio buffer.
    """
    await twilio_ws.accept()
    log.info("Twilio WebSocket connected")

    session = new_session()
    openai_ws = None

    try:
        # --- Connect to OpenAI Realtime (GA gpt-realtime — no beta header) ---
        # Explicit certifi CA bundle so TLS verification works regardless of
        # how Python was installed or where this is deployed.
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        openai_ws = await websockets.connect(
            OPENAI_REALTIME_URL,
            additional_headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            ssl=ssl_context,
        )
        log.info("Connected to OpenAI Realtime API")

        await openai_ws.send(json.dumps(SESSION_UPDATE_PAYLOAD))

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
) -> None:
    """Forward caller audio from Twilio to OpenAI Realtime."""
    try:
        async for raw in twilio_ws.iter_text():
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "media":
                # Forward audio chunk (already base64 g711_ulaw)
                await openai_ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": msg["media"]["payload"],
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
) -> None:
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

            # --- Response lifecycle: track whether OpenAI is generating audio ---
            elif event_type == "response.created":
                session["active_response"] = True

            elif event_type in ("response.done", "response.output_audio.done"):
                session["active_response"] = False
                if event_type in LOG_EVENTS:
                    log.info(f"OpenAI event: {event_type}")

            # --- Barge-in: only cancel + clear when a response is actually in flight ---
            elif event_type == "input_audio_buffer.speech_started":
                if session["active_response"]:
                    log.info("Barge-in detected — clearing Twilio buffer + cancelling OpenAI response")
                    await twilio_ws.send_json({
                        "event": "clear",
                        "streamSid": session["stream_sid"],
                    })
                    await openai_ws.send(json.dumps({"type": "response.cancel"}))
                    session["active_response"] = False
                # else: caller spoke while nothing was being said — nothing to cancel.

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

            # --- Selective logging (events not handled explicitly above) ---
            elif event_type in LOG_EVENTS:
                log.info(f"OpenAI event: {event_type}")

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        log.error(f"OpenAI→Twilio error: {e}", exc_info=True)
