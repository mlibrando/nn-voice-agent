"""Natural Nutrition voice-agent bridge package.

Structure:
  main.py       — FastAPI app: routes, lifespan
  bridge.py     — Twilio ↔ OpenAI Realtime WebSocket bridge + barge-in
  session.py    — Per-call state factory
  config.py     — Env vars, constants, prompt, session.update payload
  tools/
    client.py       — Shared async HTTP client for the mock backend
    definitions.py  — Realtime tool schemas (Day 3 part 2)

Logging is configured once here so every submodule can just
`log = logging.getLogger(__name__)` and get a namespaced logger.
"""
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s  %(message)s")
