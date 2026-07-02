# CLAUDE.md — Natural Nutrition Voice Agent

## Project overview
Building a customer support **voice agent** ("Ashley") for Natural Nutrition, a health supplement subscription company. This is a paid trial project with a **July 13, 10:00 AM ET deadline** (10 working days from June 29). The goal is to demonstrate technical expertise, product sense, and attention to detail.

The agent mirrors an existing text-based support agent ("text-Ashley") but adapted for voice — short turns, conversational pacing, no markdown/bullets.

## Architecture (Path 1 — locked in)
**Twilio Programmable Voice + OpenAI Realtime API (speech-to-speech).**

```
Caller ──PSTN──► Twilio Number (956-906-8451)
                  │  <Connect><Stream> bidirectional Media Streams, μ-law 8kHz
                  ▼
      Orchestrator (Python / FastAPI + websockets)
        • Twilio media-stream WS  ◄──►  OpenAI Realtime API WS (gpt-realtime)
        • Per-call session state: {call_id, customer, verified, candidate_account}
        • Tool dispatch: Realtime function_call → Tool Layer → Mock backend
        • Recording + transcript capture
                  │
                  ├──► Tool Layer (httpx async) ──► Mock backend (localhost:8001)
                  └──► Call store (sqlite or files) ◄── Call-review UI
```

**Why Path 1 over Path 3 (Pipecat):** Higher delivery floor in 10 days. Native streaming + barge-in + reuse reference agent's tool defs directly. Tradeoff: less provider flexibility, less barge-in control. Documented in DECISIONS.md.

**Language:** Python/FastAPI. Stays close to reference agent's Python FunctionTool shapes.

## Mock backend (source of truth: INTERFACE.md in project knowledge)
Base URL: `http://localhost:8001` (or deployed URL).

### Critical naming traps — get these right every time
- Lookup param is **`order_number`**; order mutations use **`order_id`** (full Shopify GID: `gid://shopify/Order/<n>`)
- `order_id` goes in the **body** for all order mutations (GID contains slashes, can't be in path)
- Discount **body** field is **`discount_pct`**; the Subscription **response** field is `discount_percentage` — don't cross them
- `refund_percentage` must be one of: `[10, 20, 25, 30, 35, 40, 50, 60]`; for 100% use `/orders/refund/full`
- Address bodies have **no `name`/`phone`** fields — those are preserved server-side
- `cancel_subscription` reason must be exact enum: `too_much_product`, `cant_afford_the_product`, `didnt_want_a_subscription`, `didnt_like_the_product`, `found_a_better_alternative`, `going_on_a_trip`, `dont_need_the_product_anymore`, `other`

### Endpoints

**Reads:**
- `GET /customers/lookup?phone=X` or `?email=X` or `?order_number=X` → `{customer, orders, subscriptions}` — exactly ONE identifier, 400 if zero or multiple
- `GET /customers/{customer_id}/orders` → `{orders: Order[]}`
- `GET /customers/{customer_id}/subscriptions` → `{subscriptions: Subscription[]}` — **+1200ms slow endpoint**
- `GET /products/{sku}` → Product; `GET /products` → `{products: Product[]}`

**Subscription mutations** (subscription_id int in path, return full Subscription):
- `POST /subscriptions/{id}/cancel` — body: `{"reason": "<enum>"}` (default "other")
- `POST /subscriptions/{id}/pause` — body: `{"pause_months": <1-6>}` (default 1)
- `POST /subscriptions/{id}/reactivate` — no body
- `POST /subscriptions/{id}/discount` — body: `{"discount_pct": <1-99>, "code": "<str>"}` — **DESTRUCTIVE/NON-IDEMPOTENT** (compounds on re-call)
- `POST /subscriptions/{id}/address` — body: `{"address1", "address2"?, "city", "province", "country"?, "zip"}`

**Order mutations** (order_id GID in body, return full Order):
- `POST /orders/refund` — body: `{"order_id": "gid://...", "refund_percentage": <allowed>}`
- `POST /orders/refund/full` — body: `{"order_id": "gid://..."}` — **side effect: also cancels unfulfilled orders**
- `POST /orders/cancel` — body: `{"order_id": "gid://..."}` — 409 if not UNFULFILLED
- `POST /orders/address` — body: `{"order_id": "gid://...", "address1", "city", "province", "zip", ...}`

**Side-effect tools:**
- `POST /escalations` — body: `{"issue_for_human" (required), "customer_id"?, "customer_details"?, "actions_taken"?, "mark_high_risk"?}` → `{"escalation_id", "status": "queued"}`
- `POST /transcripts` — body: `{"call_id" (required), "transcript" (required), "customer_id"?, "caller_phone"?, "summary"?, "outcome"?, "recording_url"?}` → `{"transcript_id", "saved": true}`

### Latency / chaos (default on)
- Ambient per request: `uniform(300, 1500)` ms
- Paths ending `/subscriptions`: **+1200 ms** additional
- `MOCK_ERROR_RATE = 0.07` → 7% of requests return 503
- **Free win:** `/customers/lookup` returns subs bundled and its path ends in `/lookup` (NOT `/subscriptions`), so it dodges the +1200ms penalty. Seed sub data from lookup; only hit the slow endpoint for post-mutation fresh reads.

### Error envelope
`{"error": {"code": "<code>", "message": "..."}}`

## Auth / verification flow
The mock trusts any identifier — **all verification must live in the agent/orchestrator**.

**Tier 0 — Caller-ID (happy path):** Twilio `From` number → `GET /customers/lookup?phone=<from>`. On hit, light confirmation ("Am I speaking with Margaret?") → `verified = true`.

**Tier 1 — Locate when caller-ID misses:** Ask for order number or email → look up with that. This is the evaluators' primary path (they call from one repeated non-seeded number).

**Tier 2 — Verify:** After locating, challenge with a **second, independent** fact checked against the retrieved record:
- `shipping_address.zip`
- `Customer.email` (if located by order #)
- `order_name` / `order_id`
- `created_at` / `line_items[].title`
- `transactions[].card_last_four` — **SALE txn only** (refund txns have null card)

**Gate all mutations and data disclosure behind `verified == true` in orchestration state, not just the prompt.**

### Auth edge cases
- `cust_006` Robert Lee: `phone: null` → forces email/order lookup
- `cust_004` David Thompson: two active subs (50004, 50005) → must disambiguate (CONV-3)
- Order `shipping_address.phone` is NOT a caller-ID signal — don't authenticate off it
- Evaluators call from one repeated non-seeded number — Tier 1/2 must be solid

## Filler-phrase system (CONV-2) — still P0 but re-scoped
With Realtime, conversational turns (no tool) are ~0.5–0.8s — under the ~1s bar. Dead air lives in tool-call windows:
- Normal endpoint: mean ~1.45s silence → **over threshold**
- `/subscriptions` read: mean ~2.65s → **way over**
- 503 retry: ~2.4–5s+ → **way over**

**Implementation:** Deterministic filler in the model's own voice on tool dispatch ("Let me pull that up…"). NOT pre-recorded WAVs. Prompt-level "narrate before tools" is a complement, not the guarantee.

## Key constraints and conventions
- **Brand name:** Always "Natural Nutrition" — never use reference code's real brand names or promo codes
- **Voice style:** Short turns, conversational, no paragraphs. Take an action or ask a question.
- **Prompt source:** `variant_a` from the reference agent, adapted for voice (strip markdown/bullets)
- **DECISIONS.md:** Must be self-written, not generated — documents stack choice, tradeoffs, what was cut, prompt adaptation, latency math, auth design, retro
- **Time-awareness:** Anchored to 2026 seed era for any date-relative copy
- **Tool errors:** Bounded retry with backoff on 503; NEVER improvise an outcome
- **Discount non-idempotency:** Call `apply_subscription_discount` once-only per intent; confirm before re-calling
- **Full refund side effect:** Silently cancels unfulfilled orders — surface both effects to caller
- **Cancel-order 409:** Don't promise cancel before checking fulfillment_status; fall back to refund

## Current state (as of end of Day 2 — fully self-standing on Fly)
- ✅ Twilio ↔ OpenAI Realtime bridge working end-to-end
- ✅ GA schema (nested audio config, updated event names)
- ✅ Caller can hear agent, have a conversation
- ✅ Transcript capture (input + output) logging to console
- ✅ Barge-in works on quiet-room calls (noise case still open — see Known issues)
- ✅ Bridge deployed: Fly.io app `nn-voice-agent`, `iad`, single-stage Dockerfile, no scale-to-zero, 2 machines warm
- ✅ Twilio number repointed from ngrok → deployed `https://nn-voice-agent.fly.dev/incoming-call`
- ✅ Real-phone RTT baseline captured on deployed `iad` host — conversational (no-tool) turns ~0.4–2.0s, matching PLAN §3 estimate
- ✅ **Mock backend deployed and co-located: Fly.io app `nn-mock-backend`, `iad`, private-only (no public IPs). Bridge reaches it over 6PN at `http://nn-mock-backend.internal:8001` via `MOCK_BACKEND_URL` env var. Chaos defaults (300–1500ms + 1200ms on `/subscriptions` + 7% 503s) intentionally left on — that's the contract, not a bug.**
- ✅ **INFRA-2 satisfied — full system is cold-callable without my laptop.** Bridge startup probe against the mock passes: `Mock backend reachable at http://nn-mock-backend.internal:8001/health — 200 {"ok":true}`.
- ⬜ Audio recording (OBS-1) + persisted transcripts (OBS-2) beyond console logging
- ✅ **Bridge module refactor complete (Day 3 part 1)** — `app/{main,bridge,session,config,tools/{client,definitions,handlers}}.py`
- ✅ **P0 tool layer complete (Day 3 part 2)** — 10 tools wired (TOOL-1,2,3,5,6,8,10,11,14,15) against the shared httpx client. `TOOL-ERR` bounded-retry (3 attempts, 200/400ms backoff on 503+network). Discount once-per-intent guard blocks compounding. Full-refund side-effect surfaced via `also_cancelled`. `medical_issue` rejected at handler before it reaches the mock. Realtime function-call dispatch loop live in `bridge.py`.
- ⬜ Auth state machine (Day 4) — session placeholders `verified`/`customer`/`candidate_account` declared, unused
- ⬜ P1 tools TOOL-4/7/9/12/13 (Day 6), TOOL-END (Day 5)
- ⬜ Filler on tool dispatch (Day 6 — `TODO Day 6` marker in `bridge.py`)
- ⬜ CX prompt content, product knowledge block, health guardrail (Day 5)

## Known issues / observations (from Day 2 live test call + deploy)
- **False barge-ins under background noise.** In a live test taken during heavy rain, the VAD fired `Barge-in detected` repeatedly on non-speech, causing the agent to cut itself off mid-word and re-greet in a loop. Root cause not yet confirmed — could be environmental noise (rain, HVAC), the mic sensitivity of the caller's phone, or the `threshold: 0.5` being too permissive. **Needs a quiet-room retest before tuning** so we don't overfit the threshold to one bad recording. Current VAD settings in `main.py`: `threshold: 0.5`, `prefix_padding_ms: 300`, `silence_duration_ms: 500`. Tuning tracked to Day 6/9.
- **`response_cancel_not_active` error spam.** Every `speech_started` sends `response.cancel` unconditionally, even when there's no in-flight response. OpenAI returns `response_cancel_not_active` every time, filling the logs with noise. This is a code bug independent of the noise question. **Fix (deferred to Day 6/9 with the VAD tuning):** track an `active_response` flag in the session dict — set on `response.created`, cleared on `response.done` / `response.output_audio.done` — and only emit `response.cancel` when it's true. Same logic should also gate the `clear` message sent to Twilio.
- **Conversational latency baseline is healthy.** No-tool turns measured **~0.4–2.0s** from `speech_stopped` to `response.output_audio.done` on the deployed `iad` host. That's consistent with the PLAN §3 estimate (~0.5–0.8s typical, worst case ~0.8s pre-network) plus real-world telephony jitter — under the ~1s perceived bar in the common case. **Filler is not needed on conversational turns.** The dead-air problem will only materialize once the tool layer adds the mock's injected 300–1500ms per call (and +1200ms on `/subscriptions`); re-measure then and confirm the §3 filler trigger is still the right cover.
- **Mock backend state is ephemeral — never redeploy right before a demo.** `store.py` holds all state in memory, seeded from `seed_data.json` on boot. Any redeploy (or Fly machine restart, however triggered) wipes mutation state back to seed. If an evaluator has been mid-call editing subs/orders, their world will reset. **Rule of thumb:** don't `fly deploy` the mock in the 30 min before a scheduled evaluator/demo window. If you MUST push a mock fix right before a call, understand you're resetting the caller's account state too.
- **OBS-2 (transcript persistence) must not depend on the mock's `transcripts.log`.** The mock appends every `POST /transcripts` to a local `transcripts.log` file next to `app.py` on the container's overlay FS — that file vanishes on any machine restart (see above). **Production transcript path is the bridge-side call store** (Day 7 UI reads from there, not from the mock's log). The mock's `save_transcript` endpoint stays useful for the *demo contract* (proving the tool works), but the durable source-of-truth is bridge-owned. This shapes the Day 7 call-review UI design: it reads bridge-side persistence, not the mock, and the mock's `transcripts.log` is diagnostic-only.

## File structure
```
nn-voice-agent/                      # bridge (this repo), Fly app: nn-voice-agent
├── app/                             # (Day 3 part 1 refactor — was single main.py)
│   ├── __init__.py                  # logging setup
│   ├── main.py                      # FastAPI app: routes + lifespan (health probe, tool-client startup/shutdown)
│   ├── bridge.py                    # Twilio ↔ OpenAI WebSocket bridge + barge-in (with active_response guard)
│   ├── session.py                   # Per-call state factory (incl. Day-4 auth placeholders: verified/customer/candidate_account)
│   ├── config.py                    # Env vars, VOICE, system prompt, SESSION_UPDATE_PAYLOAD, LOG_EVENTS
│   └── tools/
│       ├── __init__.py
│       ├── client.py                # Shared httpx.AsyncClient + call_mock retry/backoff + ToolError + health_probe
│       ├── definitions.py           # OpenAI Realtime tool schemas (P0 × 10) + enum constants
│       └── handlers.py              # Async handler per tool + dispatch(); once-per-intent guards; medical_issue reject
├── scripts/
│   └── test_tools.py                # Dev-time smoke test: all 10 handlers + retry exhaustion + guards. Resets mock at start.
├── requirements.txt                 # fastapi, uvicorn, websockets, httpx, dotenv, certifi
├── Dockerfile                       # single-stage python:3.13-slim, uvicorn app.main:app on 0.0.0.0:${PORT} (8080)
├── .dockerignore                    # excludes .env, .venv, __pycache__, .git, docs
├── fly.toml                         # Fly.io: iad, port 8080, no auto-stop, WS-friendly concurrency
├── DEPLOY.md                        # flyctl runbook + Twilio repoint + mock co-location notes
├── .env                             # OPENAI_API_KEY, TWILIO_*, MOCK_BACKEND_URL, PORT (local-dev only)
├── env.example
├── .gitignore
├── PLAN.md                          # requirements matrix + 10-day plan
├── INTERFACE.md                     # mock backend API contract (source of truth)
└── CLAUDE.md                        # This file

../rtp-ashley-voice/mock-backend/    # mock backend (sibling repo), Fly app: nn-mock-backend
├── app.py, models.py, store.py      # FastAPI + in-memory seed store (unmodified)
├── seed_data.json                   # seeded customers/orders/subs (unmodified)
├── requirements.txt                 # fastapi, uvicorn, pydantic (unmodified)
├── run.sh                           # local dev only: uvicorn on :8001
├── Dockerfile                       # NEW: single-stage 3.13-slim, uvicorn --host :: :8001
├── .dockerignore                    # NEW: excludes .venv, .git, run.sh, README, transcripts.log
└── fly.toml                         # NEW: iad, internal_port=8001, http_service, no HTTP check
                                     #      (see fly.toml comment — asyncio v6-only quirk)
```

## Commands

### Deployed services (Fly.io, region `iad`)
| App | Public? | Address the bridge/dev uses |
|---|---|---|
| `nn-voice-agent` (this repo) | ✅ HTTPS + WS | `https://nn-voice-agent.fly.dev` (Twilio webhook + WS) |
| `nn-mock-backend` (sibling `rtp-ashley-voice/mock-backend/`) | ❌ private-only, no public IPs | `http://nn-mock-backend.internal:8001` (6PN, IPv6) |

### Env var that connects them
- `MOCK_BACKEND_URL` — the bridge reads this at startup and threads it into the Day 3 tool layer. Local default: `http://localhost:8001`. Fly (bridge app) secret: `http://nn-mock-backend.internal:8001`. Set via `fly secrets set MOCK_BACKEND_URL=... -a nn-voice-agent`.

```bash
# Local dev — bridge (post-refactor entrypoint is `app.main:app`)
uvicorn app.main:app --reload --port 8080          # reads .env, defaults MOCK_BACKEND_URL to http://localhost:8001
# (or: python -m app.main)

# Local dev — mock backend (sibling repo)
cd ../rtp-ashley-voice/mock-backend && ./run.sh    # listens on :8001

# Local tunnel (only if pointing Twilio at localhost)
ngrok http 8080

# Deployed ops
fly logs -a nn-voice-agent                          # bridge logs
fly logs -a nn-mock-backend                         # mock logs
fly ssh console -a nn-voice-agent                   # shell into the bridge
fly ssh console -a nn-voice-agent -C \
  "python -c 'import httpx; print(httpx.get(\"http://nn-mock-backend.internal:8001/health\").text)'"
# ↑ verifies bridge→mock 6PN connectivity

# Test the mock directly (only from another Fly app in the same org — it's private)
# From your laptop, you cannot curl the mock; that's intentional.
```

## Requirements IDs (for traceability)
Reference PLAN.md for the full requirements matrix with source traceability. Key P0 IDs: VOICE-1, CONV-1/2/3/4/5, AUTH-1/2/3, TOOL-1/2/3/5/6/8/10/11/14/15, TOOL-ERR, SAFE-1, DATA-1, INFRA-1/2, OBS-1/2/3, DOC-1/2/3.
