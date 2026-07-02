"""Env-driven config + static constants.

All environment reads and payload constants live here so `bridge.py` and
`main.py` stay focused on behavior.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", 8080))

# Mock backend base URL. Local dev defaults to localhost:8001; on Fly, set via
# `fly secrets set MOCK_BACKEND_URL=http://nn-mock-backend.internal:8001` so
# the bridge reaches the mock over 6PN (private, no public exposure).
MOCK_BACKEND_URL = os.getenv("MOCK_BACKEND_URL", "http://localhost:8001").rstrip("/")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is required — add it to .env")


# ---------------------------------------------------------------------------
# OpenAI Realtime — connection + session config
# ---------------------------------------------------------------------------
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime"
VOICE = "marin"  # Options: ash, ballad, coral, sage, verse, marin — test more on Day 9

SYSTEM_MESSAGE = """
You are Ashley, a friendly and efficient customer support agent for Natural Nutrition,
a health supplement subscription company. You're warm, concise, and helpful.

For now, greet the caller, ask how you can help, and have a natural conversation.
Keep your responses short — one or two sentences at a time, like a real phone call.
Don't ramble or give long lists. If you're unsure about something, say so honestly.

Remember: you're on a phone call, not writing an email. Be conversational.
""".strip()

# GA gpt-realtime session config. Nested audio.input/output, format objects,
# output_modalities (not modalities), no beta header. Do not regress — see
# PLAN.md Risk #13.
SESSION_UPDATE_PAYLOAD = {
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
                "transcription": {"model": "gpt-4o-mini-transcribe"},
            },
            "output": {
                "format": {"type": "audio/pcmu"},
                "voice": VOICE,
            },
        },
    },
}

# Events worth logging (keeps the console readable)
LOG_EVENTS = {
    "session.created",
    "session.updated",
    "input_audio_buffer.speech_started",
    "input_audio_buffer.speech_stopped",
    "response.output_audio.done",
    "response.done",
    "error",
}
