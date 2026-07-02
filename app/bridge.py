"""Twilio Media Stream ↔ OpenAI Realtime bridge.

Handles one call: opens both websockets, forwards audio in both directions,
and manages barge-in cleanly during Twilio playback (not just during OpenAI
generation — see the playback-tracking notes in `session.py`).

Barge-in model (CONV-1 P0). On `input_audio_buffer.speech_started`, if there's
audio buffered/playing on Twilio (`mark_queue` non-empty), we:
  1. Compute `audio_end_ms = latest_media_timestamp - response_start_ts_twilio`
     — how many ms of the current response have actually reached the caller.
  2. Send OpenAI a `conversation.item.truncate` at that offset so its
     conversation history matches what the caller actually heard.
  3. Send Twilio a `clear` to drain the queued audio.
  4. Reset the tracking state.
This is the OpenAI/Twilio-Realtime idiomatic pattern; the older `response.cancel`
approach cancels generation but doesn't truncate the item, so the model's next
turn is out of sync with what the caller heard.
"""
import asyncio
import base64
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
from app.tools import handlers as tool_handlers

log = logging.getLogger(__name__)


def _delta_audio_ms(b64_delta: str) -> float:
    """Compute the duration in ms of one base64-encoded g711 μ-law audio chunk.

    Format contract: 8 kHz μ-law, 1 sample = 1 byte. So decoded_bytes / 8 = ms.
    Base64 expands 3 bytes → 4 chars, with up to 2 trailing `=` padding chars
    that don't contribute to the decoded length. The math-only formula
    `(len(delta) * 3 // 4) - padding` matches an actual `b64decode` byte count
    exactly — verified by the one-shot [audio-math check] log emitted on the
    first delta of each call.
    """
    padding = b64_delta.count("=")
    decoded_bytes = (len(b64_delta) * 3) // 4 - padding
    return decoded_bytes / 8.0


async def media_stream(twilio_ws: WebSocket) -> None:
    """Bidirectional bridge for a single call:
      Twilio Media Stream  ←→  OpenAI Realtime API

    Audio format: g711 μ-law 8kHz (native to Twilio, supported by Realtime).
    Turn detection: server_vad (OpenAI handles endpointing).
    Proactive greeting: kicked off from `_twilio_to_openai` on the Twilio
    `start` event (so `stream_sid` is known before OpenAI can emit audio).
    Barge-in: see module docstring.
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

        # Run both directions concurrently. The Twilio side kicks the
        # proactive greeting once it captures `stream_sid` from the `start`
        # event — that avoids a race where OpenAI's first audio delta arrives
        # before Twilio's stream is ready to receive media frames.
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
    """Forward caller audio from Twilio to OpenAI Realtime, track Twilio's
    audio clock for barge-in, kick the proactive greeting on `start`."""
    try:
        async for raw in twilio_ws.iter_text():
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "media":
                # Advance our copy of Twilio's audio clock. This is what
                # barge-in truncation uses to compute `audio_end_ms`.
                ts = msg["media"].get("timestamp")
                if ts is not None:
                    session["latest_media_timestamp"] = int(ts)
                # Forward the caller's audio chunk (already base64 g711_ulaw).
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
                # Proactive greeting (A1). The system prompt in
                # SESSION_UPDATE_PAYLOAD sets the persona; this per-response
                # instruction is a small nudge to make her speak *first* so
                # the caller isn't sitting on silence. Full persona is Day 5.
                await openai_ws.send(json.dumps({
                    "type": "response.create",
                    "response": {
                        "instructions": (
                            "Greet the caller warmly as Ashley from Natural "
                            "Nutrition and ask how you can help. Keep it to "
                            "one short sentence — this is a phone call."
                        ),
                    },
                }))

            elif event == "mark":
                # Twilio echoed one of our marks — that chunk of audio has
                # finished playing to the caller. Pop one off the queue.
                if session["mark_queue"]:
                    session["mark_queue"].pop(0)

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
    """Forward agent audio from OpenAI Realtime to Twilio, handle barge-in
    via the mark-queue + truncate pattern (see module docstring)."""
    try:
        async for raw in openai_ws:
            msg = json.loads(raw)
            event_type = msg.get("type", "")

            # --- Audio chunk → send to Twilio, track for barge-in ---
            if event_type == "response.output_audio.delta" and msg.get("delta"):
                delta_b64 = msg["delta"]
                item_id = msg.get("item_id")

                # (Anchor-by-item-id.) Re-anchor whenever a NEW item_id
                # appears — not just on the "anchor is None" edge. This is
                # the fix for the stale-anchor bug: previously the anchor
                # was only cleared on barge-in, so a normal turn completion
                # left it pointing at an earlier turn's start, and the
                # `elapsed_ms` on later barge-ins came out as cumulative
                # call time (18–25s) instead of per-response elapsed.
                if item_id and item_id != session["last_assistant_item"]:
                    session["response_start_timestamp_twilio"] = session["latest_media_timestamp"]
                    session["last_assistant_item"] = item_id
                    session["audio_sent_ms"] = 0.0

                # Cumulative audio duration sent for this item — safety-net
                # clamp on the truncate offset.
                chunk_ms = _delta_audio_ms(delta_b64)
                session["audio_sent_ms"] += chunk_ms

                # One-shot arithmetic sanity check per call: log the formula
                # result alongside an actual `base64.b64decode` byte count so
                # we can confirm the math is right on a real chunk. If these
                # diverge, the clamp is under-counting and would clip context.
                if not session.get("_logged_chunk_math"):
                    actual_bytes = len(base64.b64decode(delta_b64))
                    formula_bytes = (len(delta_b64) * 3) // 4 - delta_b64.count("=")
                    log.info(
                        f"[audio-math check] delta base64 len={len(delta_b64)} "
                        f"padding={delta_b64.count('=')} "
                        f"formula_bytes={formula_bytes} decoded_bytes={actual_bytes} "
                        f"→ chunk_ms={actual_bytes / 8:.3f} "
                        f"(match={'YES' if formula_bytes == actual_bytes else 'NO — CLAMP WILL UNDER-COUNT'})"
                    )
                    session["_logged_chunk_math"] = True

                await twilio_ws.send_json({
                    "event": "media",
                    "streamSid": session["stream_sid"],
                    "media": {"payload": delta_b64},
                })
                # Send a mark right after each chunk. Twilio echoes it back
                # when that chunk finishes playing; the queue's length is our
                # "audio still in flight" gate for barge-in.
                await twilio_ws.send_json({
                    "event": "mark",
                    "streamSid": session["stream_sid"],
                    "mark": {"name": "responsePart"},
                })
                session["mark_queue"].append("responsePart")

            # --- Function calling: model wants to invoke a tool ---
            elif event_type == "response.function_call_arguments.done":
                # TODO Day 6: emit a spoken filler here on tool dispatch — a
                # short response.create with an ack-only instruction (in the
                # model's own voice) before we await the mock. This is the
                # deterministic cover for §3's tool-call dead-air window.
                call_id = msg.get("call_id")
                name = msg.get("name")
                args_json = msg.get("arguments", "{}")
                try:
                    args = json.loads(args_json) if args_json else {}
                except json.JSONDecodeError:
                    args = {}
                log.info(f"Tool call: {name}({args_json})")
                result = await tool_handlers.dispatch(name, args, session)
                log.info(f"Tool result: {name} -> ok={result.get('ok')}")
                # 1. Return the result to the model
                await openai_ws.send(json.dumps({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result),
                    },
                }))
                # 2. Ask the model to speak with the result in hand
                await openai_ws.send(json.dumps({"type": "response.create"}))

            # --- Barge-in: truncate the item at the actual audio-out offset ---
            elif event_type == "input_audio_buffer.speech_started":
                if session["mark_queue"] and session["response_start_timestamp_twilio"] is not None:
                    elapsed_ms = (
                        session["latest_media_timestamp"]
                        - session["response_start_timestamp_twilio"]
                    )
                    audio_sent_ms = int(session["audio_sent_ms"])
                    # Clamp: cannot ask OpenAI to cut past what we actually
                    # generated. When the anchor is correct, elapsed_ms will
                    # already be ≤ audio_sent_ms, so this is a no-op and we
                    # log which one won for verification. If audio_sent_ms
                    # consistently wins, the base64 math is under-counting.
                    audio_end_ms = max(0, min(elapsed_ms, audio_sent_ms))
                    winner = "elapsed" if elapsed_ms <= audio_sent_ms else "audio_sent (CLAMPED)"
                    item_id = session["last_assistant_item"]
                    log.info(
                        f"Barge-in — truncating item={item_id} at "
                        f"audio_end_ms={audio_end_ms} "
                        f"(elapsed={elapsed_ms} audio_sent={audio_sent_ms} won={winner}); "
                        f"clearing Twilio buffer"
                    )
                    if item_id:
                        await openai_ws.send(json.dumps({
                            "type": "conversation.item.truncate",
                            "item_id": item_id,
                            "content_index": 0,
                            "audio_end_ms": audio_end_ms,
                        }))
                    await twilio_ws.send_json({
                        "event": "clear",
                        "streamSid": session["stream_sid"],
                    })
                    # Reset playback tracking. The next response's first
                    # delta will bring a fresh item_id → re-anchor there.
                    session["mark_queue"] = []
                    session["response_start_timestamp_twilio"] = None
                    session["last_assistant_item"] = None
                    session["audio_sent_ms"] = 0.0
                else:
                    # Caller spoke while nothing was being played — nothing to
                    # truncate. Log so the event isn't invisible in debugging.
                    log.info("OpenAI event: input_audio_buffer.speech_started (no audio in flight)")

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

            # --- Selective logging (response.done, session.created, etc.) ---
            elif event_type in LOG_EVENTS:
                log.info(f"OpenAI event: {event_type}")

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        log.error(f"OpenAI→Twilio error: {e}", exc_info=True)
