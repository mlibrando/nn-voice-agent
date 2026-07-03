# CLAUDE.md — Natural Nutrition Voice Agent

## Project overview
Building a customer support **voice agent** ("Ashley") for Natural Nutrition, a health supplement subscription company. This is a paid trial project with a **July 13, 10:00 AM ET deadline** (10 working days from June 29). The goal is to demonstrate technical expertise, product sense, and attention to detail.

The agent mirrors an existing text-based support agent ("text-Ashley") but adapted for voice — short turns, conversational pacing, no markdown/bullets.

## About me / working preferences
- **Background:** senior full-stack engineer. Strong Python/FastAPI, TypeScript, React, NestJS.
- **When planning:** structured plans with explicit P0/P1/P2 priorities and honest trade-offs — show what's cut and why.
- **When implementing:** production-quality code with real error handling. Flag risks and edge cases proactively; don't wait to be asked.
- **When I correct a recommendation:** incorporate it cleanly and move forward. Don't re-litigate settled choices.

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

## Current state (as of end of Day 5 — full persona live, coupled auth-greeting fix landed, TOOL-END wired)
### Days 1–2 (infra + bridge)
- ✅ Twilio ↔ OpenAI Realtime bridge, GA schema (nested audio config, correct event names)
- ✅ Bridge deployed: Fly.io app `nn-voice-agent`, `iad`, single-stage Dockerfile, no scale-to-zero, 2 machines warm
- ✅ Twilio number pointed at `https://nn-voice-agent.fly.dev/incoming-call`
- ✅ Real-phone RTT baseline on deployed `iad` — ~0.4–2.0s conversational turns, matches §3 estimate
- ✅ Mock backend co-located on Fly (`nn-mock-backend`, `iad`), private-only via 6PN at `http://nn-mock-backend.internal:8001` (`MOCK_BACKEND_URL` secret). Chaos defaults on (300–1500ms + 1200ms on `/subscriptions` + 7% 503s — the contract, not a bug).
- ✅ **INFRA-2 satisfied** — cold-callable without my laptop; bridge startup probe against mock passes
- ✅ Transcript capture (input + output) logging to console

### Day 3 (tool layer + interaction fixes) — closed
- ✅ **Bridge module refactor** — `app/{main,bridge,session,config,tools/{client,definitions,handlers}}.py`
- ✅ **P0 tool layer** — 10 tools wired (TOOL-1,2,3,5,6,8,10,11,14,15) with naming traps baked into schemas + handler defenses
- ✅ **TOOL-ERR retry/backoff** — 3 attempts, 200/400ms backoff on 503 + transient network; 4xx fails immediately
- ✅ **Discount lifecycle guarded end-to-end** — within-call (`session.applied_discounts`) blocks re-pitch compounding; cross-call reads persisted `discount_percentage` from `session.subscriptions_by_id` (populated by `customer_lookup`) so a returning caller can't stack. Policy drafted in `DECISIONS.md`.
- ✅ **Escalation degrades without customer_id** — unauth path prepends `[unverified caller — call_sid=…]` to `customer_details` as ops correlation key
- ✅ **A1 proactive greeting** (live-verified) — Ashley greets on pickup unprompted; no more "she waits until I say something"
- ✅ **A2 barge-in during Twilio playback** (live-verified — real CONV-1 fix) — replaced the broken `active_response` scheme with item-id-keyed anchor + Twilio-timestamp elapsed + `audio_sent_ms` clamp. Six consecutive interrupts in one call: `won=elapsed` every time (clamp is a no-op), no `invalid_value` errors, offsets plausibly per-response (760ms – 7400ms based on how long she was allowed to speak). Supersedes the earlier "acceptable for telephony" state — the truncate now keeps OpenAI's item in sync with what the caller actually heard.

### Day 4 (auth state machine + tool gating) — closed
- ✅ **Twilio `From` capture** — `/incoming-call` embeds `From` via TwiML `<Parameter>`; bridge reads it from `customParameters` on the `start` event into `session["from_number"]`.
- ✅ **Tier-0 auto-lookup** in bridge `start` branch (before greeting): on caller-ID match, `session["tier0_hit"]` flips true. *(Greeting phrasing itself was rewritten in Day 5 — see below.)* On miss, generic greeting + system-context message steers Ashley to Tier-1.
- ✅ **Tier-1 fallback** via `customer_lookup(order_number=…|email=…)` — evaluator path (unseeded number) and `cust_006` (`phone: null`) both work.
- ✅ **`verify_identity` tool** — challenge kinds: `caller_id_confirm` (Tier-0 only, refused otherwise with `code=caller_id_didnt_match`), `zip` (any on-file address — sub OR order — with digits-only normalize), `email` (case-insensitive), `order_name` (digits-only), `card_last_four` (SALE-txn only — REFUND has null card, IFACE gotcha #7). `line_item_title` deliberately excluded (~5 SKU catalog + loose match = too low entropy).
- ✅ **Dispatch-layer gate** — `handlers.dispatch()` refuses everything but `{customer_lookup, verify_identity, create_escalation, save_transcript}` when `session["verified"]` is False. `code=verification_required` returned; the mock is never touched. Reads gated too, not just mutations.
- ✅ **`customer_lookup` sanitizes pre-verify** — response returns `{ok, located, verification_required, customer_first_name, tier0_hit, _note}` only. Full record cached in `session["candidate_account"]` for the code-side challenge check. No address/email/order data leaked to the model before verification.
- ✅ **Attempt cap = 3** with **self-contained graceful lockout** — `verify_identity` on the 4th call returns `code=locked_out` with `spoken_line` (caller-facing verbiage), `next_action: "create_escalation"`, and prefilled `escalation_suggestion` args. Escalation fallback survives even a stripped/drifted prompt.
- ✅ **Order shipping phone never treated as identity signal.** IFACE gotcha #17 respected — no challenge kind reads from `shipping_address.phone`.
- ✅ **Day-3 deferred hook consumed** — `create_escalation` unauth breadcrumb now includes `from=<number>` alongside `call_sid`.
- ✅ **SYSTEM_MESSAGE** — minimal auth-flow steering appended (not persona; Day 5 warms it).
- ✅ **12 auth handler tests** in `scripts/test_tools.py`, all green (proves gate holds before mock is hit, sanitization holds, tier-0 gates caller_id_confirm, cap → locked_out with self-contained payload, cust_006 email/order path works).
- ✅ **TESTING.md Section F (F1–F8)** — live-call test suite added with per-test log signals + SSH bonus-checks.
- ✅ **DECISIONS.md** — two new draft entries: "Located vs. verified + dispatch-gate" and "Order name as moderate-strength knowledge factor" (🚧 DRAFT — REVIEW AND REWORD).

### Day 5 (CX/persona prompt + TOOL-END + coupled auth-greeting fix) — closed
- ✅ **SYSTEM_MESSAGE replaced** — Day-1 placeholder → full structured persona. Sections: persona/voice-format, identity verification (updated for open greeting), CX cadence (empathy sandwich, mirror-vs-reframe, yield-advance, cue-to-switch, power-phrases-with-truthfulness-gate, narrate-the-write), retention micro-sequence (RETN-1/RETN-2 one-and-done), product-knowledge block (KNOW-1) for the 5 seeded SKUs sourced from labels + AIA style (never AIA literal doses), SAFE-3 guardrail with BROAD SAFE-1 trigger (any wellness/health-adjacent phrasing → cancel-with-`reason=other` + medical follow-up + skip retention), billing/refund (EXPL-1), CX-7 abuse ladder, Realtime-emotion-tag caveat.
- ✅ **Truthfulness gate** — SYSTEM_MESSAGE forbids "let me talk to my manager" / "check with my supervisor" / "VIP customer" phrasing. AIA conv 03's fictional-manager framing replaced with honest concession framing ("Here's the best I can do — 20% lifetime discount"). Real outcome, no invented authority.
- ✅ **TOOL-END wired.** New `end_call(reason)` tool. Handler posts to Twilio REST API (Calls/{CallSid}.json?Status=completed) using `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` env. Auto-creates high-risk `create_escalation` server-side before hangup (audit trail). Prompt-gated (SYSTEM_MESSAGE CX-7 3-step ladder), `session["abuse_strikes"]` counter incremented on every attempt for observability. In `_PRE_AUTH_TOOLS` — abuse can happen pre-verify.
- ✅ **Register-shift dispatch** in `bridge.py`. After SAFE-1 cancel (`reason="other"`) or `end_call`, the follow-up `response.create` includes a per-response `instructions` string nudging warmer/slower prosody. Risk #9 workaround for no-emotion-tags.
- ✅ **COUPLED AUTH-GREETING FIX (bundled, non-splittable) — both landed together:**
  - **(1)** Tier-0 greeting is now OPEN regardless of Tier-0 hit. Bridge instructions never volunteer the located name ("who do I have the pleasure of speaking with?" / "am I speaking with the account holder?"). The located `first_name` stays in the system-context message so the model knows who to verify against; it's just not spoken in the greeting. Fixes the F6 name-leak and the "greeting-as-assertion" CX-6 violation.
  - **(2)** `_is_affirmative` tightened. Old substring-match `_AFFIRMATIVE_WORDS` (which contained "this is" and "speaking") replaced with `_AFFIRMATIVE_TOKENS` + `_STOP_TOKENS` + `_MULTI_WORD_AFFIRMATIVES`. New logic: whole-word first-name check OR every token in the affirmative-or-stop union. "This is Bob" against `first_name="Margaret"` now returns `verification_failed`. AUTH-17 is the regression guard. **Shipping (1) without (2) would have opened a Tier-0 auth bypass** — anyone claiming to be the holder ("This is [any name]") would have verified.
- ✅ **6 new AUTH tests** — AUTH-17 (impostor ship-block), AUTH-18 ("This is Margaret" verifies), AUTH-19 (bare "yes"), AUTH-20 (bare name), AUTH-21 ("Yeah, Bob"). All green.
- ✅ **END-1 test** — end_call handler shape, `_PRE_AUTH_TOOLS` inclusion, graceful missing-creds path, dispatch-gate allows pre-verify. All green.
- ✅ **TESTING.md Section G (G1–G7)** — CX/persona live-call suite. G3 is the live-call regression for AUTH-17. G5/G5b/G5c triangulate the SAFE-1 boundary (clear symptom / ambiguous phrasing / pure preference respectively). F6 updated to "landed" status.
- ✅ **DECISIONS.md** — three new 🚧 DRAFT entries: "Truthfulness gate — no fake manager", "SAFE-1 broad-trigger", "`end_call` — prompt-gated for Day 5". Greeting-phrasing paragraph in draft #1 updated to reflect landed.

### Pending / next
- ⬜ **Day 6** — deterministic filler on tool dispatch (`TODO Day 6` marker in `bridge.py`), P1 tools TOOL-4/7/9/12/13, VAD tuning after quiet-room retest, possible handler-side `abuse_strikes` gate on `end_call` if G7 reveals drift
- ⬜ **Day 7** — call-review UI (bridge-side persistence per Day-2 known-issues note)
- ⬜ Audio recording (OBS-1) + persisted transcripts (OBS-2) beyond console logging

## Known issues / observations (from Day 2 live test call + deploy)
- **VAD noise-robustness still untested.** The old `response.cancel` cut-off-and-re-greet loop from the rainy-day test is architecturally resolved by A2 (mark-queue gate + item-id truncate). The remaining open question is whether `server_vad @ threshold: 0.5` fires false `speech_started` events on background noise (rain, café, car, HVAC). Under the new bridge a false trigger during Ashley's playback would still truncate her mid-sentence (bad UX, but no runaway loop). Quiet-room baseline first, then Day 6/9 tuning of `threshold` / `silence_duration_ms` or a switch to `semantic_vad`. Current VAD settings in `app/config.py`: `threshold: 0.5`, `prefix_padding_ms: 300`, `silence_duration_ms: 500`.
- **Conversational latency baseline is healthy.** No-tool turns measured **~0.4–2.0s** from `speech_stopped` to `response.output_audio.done` on the deployed `iad` host. That's consistent with the PLAN §3 estimate (~0.5–0.8s typical, worst case ~0.8s pre-network) plus real-world telephony jitter — under the ~1s perceived bar in the common case. **Filler is not needed on conversational turns.** The dead-air problem will only materialize once the tool layer adds the mock's injected 300–1500ms per call (and +1200ms on `/subscriptions`); re-measure then and confirm the §3 filler trigger is still the right cover.
- **Mock backend state is ephemeral — never redeploy right before a demo.** `store.py` holds all state in memory, seeded from `seed_data.json` on boot. Any redeploy (or Fly machine restart, however triggered) wipes mutation state back to seed. If an evaluator has been mid-call editing subs/orders, their world will reset. **Rule of thumb:** don't `fly deploy` the mock in the 30 min before a scheduled evaluator/demo window. If you MUST push a mock fix right before a call, understand you're resetting the caller's account state too.
- **OBS-2 (transcript persistence) must not depend on the mock's `transcripts.log`.** The mock appends every `POST /transcripts` to a local `transcripts.log` file next to `app.py` on the container's overlay FS — that file vanishes on any machine restart (see above). **Production transcript path is the bridge-side call store** (Day 7 UI reads from there, not from the mock's log). The mock's `save_transcript` endpoint stays useful for the *demo contract* (proving the tool works), but the durable source-of-truth is bridge-owned. This shapes the Day 7 call-review UI design: it reads bridge-side persistence, not the mock, and the mock's `transcripts.log` is diagnostic-only.

## File structure
```
nn-voice-agent/                      # bridge (this repo), Fly app: nn-voice-agent
├── app/                             # (Day 3 part 1 refactor — was single main.py)
│   ├── __init__.py                  # logging setup
│   ├── main.py                      # FastAPI app: routes + lifespan (health probe, tool-client startup/shutdown)
│   ├── bridge.py                    # Twilio ↔ OpenAI WebSocket bridge + item-id-keyed truncate barge-in
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
