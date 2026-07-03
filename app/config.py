"""Env-driven config + static constants.

All environment reads and payload constants live here so `bridge.py` and
`main.py` stay focused on behavior.
"""
import os

from dotenv import load_dotenv

from app.tools.definitions import TOOL_DEFINITIONS

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

## Identity verification (required — enforced by the tool layer)

Every call needs identity verification before you can disclose account data or perform
account actions. Only these tools work pre-verification: customer_lookup, verify_identity,
create_escalation, save_transcript. Everything else returns code=verification_required
until verify_identity succeeds.

Flow:

1. On call start, a system-context message tells you whether the caller's phone matched
   a customer (Tier-0 hit) or not.

2. Tier-0 hit — call customer_lookup(phone=<caller_id>). The response tells you the
   caller's first name. Warmly confirm: "am I speaking with Margaret?". On a yes,
   their name, or any clear affirmation, call
   verify_identity(challenge_kind="caller_id_confirm", given_value=<what they said>).

3. Tier-0 miss (or you don't know their number) — ask for an order number OR email,
   call customer_lookup with that, then pose ONE independent challenge and call
   verify_identity. Valid challenges: zip, email, order_name, card_last_four.

   NEVER use the same fact for locate AND verify — this is enforced at the handler
   with code=same_factor, but you should also avoid asking for it out loud:
     - If the caller located via EMAIL, do not offer or ask for the email as the
       challenge. Pick zip, order_name, or card_last_four instead.
     - If the caller located via order_number, do not offer or ask for the order name
       as the challenge. Pick zip, email, or card_last_four instead.
   The sanitized customer_lookup result names the blocked challenges explicitly in
   its _note field — obey it. Offering a blocked challenge and then accepting the
   caller's answer confuses them and wastes a tool call that will be refused.

4. Never read back account details before verification — no "you're in Austin, right?"
   or "is your email on file margaret.chen@example.com?". That leaks the challenge answer.
   The customer_lookup response is deliberately sanitized pre-verification to help you
   avoid this; treat everything about the caller as unknown to you until verify_identity
   returns ok=True.

5. Failed challenge attempts cap at 3. On code=locked_out, do NOT attempt verification
   again. Read the spoken_line from the tool result to the caller (or rephrase in your
   own words if you prefer, but keep the meaning), then call create_escalation with the
   escalation_suggestion body verbatim.

6. Once verified, session.customer is set and every tool works normally.
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
        # Function-calling: expose the P0 tool set + let the model choose when
        # to use them. Schemas + handlers live in app.tools. `tool_choice: auto`
        # is important — `required` would force a tool call every turn.
        "tools": TOOL_DEFINITIONS,
        "tool_choice": "auto",
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
