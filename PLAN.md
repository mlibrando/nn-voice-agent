# Natural Nutrition Voice Agent — Build Plan (rev. Path 1)

**Path:** Twilio Programmable Voice + OpenAI Realtime API (speech-to-speech).
**Source of truth for tools/data:** `INTERFACE.md`. Every field name below is taken verbatim from it.
**Deadline:** Mon Jul 13, 10:00 AM ET. **Working days:** Jun 29 – Jul 10 (10 days), Jul 11–12 buffer.

---

## 1. Requirements Matrix

Each requirement has an ID, a source for traceability, and a priority. Sources:
`BRIEF` = presentation deck, `KICK` = kickoff transcript, `README` = repo README, `IFACE` = INTERFACE.md.

| ID | Requirement | Source | Priority |
|----|-------------|--------|----------|
| VOICE-1 | Warm, human-sounding voice with varied prosody — not a flat "AI voice". First few seconds should feel possibly-human. | BRIEF (Non-negotiables #1), KICK | P0 |
| CONV-1 | Barge-in: when the caller speaks, the agent stops and listens; never talks over them. | BRIEF #2 | P0 |
| CONV-2 | No dead air: a natural acknowledgment covers any "thinking"/tool-call gap. | BRIEF #3, KICK (~1s threshold) | P0 (scoped — see §3) |
| CONV-3 | Ask when unsure: disambiguate multiple orders/subscriptions instead of guessing. | BRIEF #4, KICK, IFACE (cust_004) | P0 |
| CONV-4 | Doesn't ramble: short turns — take an action or ask a question, no paragraphs. | BRIEF #5 | P0 |
| CONV-5 | Conversational latency ~1s; pace of a normal human call; verified on a real phone. | BRIEF #6, KICK | P0 |
| AUTH-1 | Caller-ID happy path: locate account from the inbound Twilio `From` number. | KICK | P0 |
| AUTH-2 | Fallback identification when caller-ID doesn't match: order number / email / phone-on-file. | KICK | P0 |
| AUTH-3 | Identity-verification gate before disclosing data or taking account actions (mock trusts caller-ID; the agent must enforce verification). | IFACE §6, KICK (guardrails) | P0 |
| TOOL-1 | `customer_lookup` — locate account + hydrate orders/subs. | README, IFACE 2.2 | P0 |
| TOOL-2 | `get_customer_orders`. | README, IFACE 2.3 | P0 |
| TOOL-3 | `get_customer_subscriptions` (slow endpoint). | README, IFACE 2.4 | P0 |
| TOOL-4 | `get_product` / `list_products`. | IFACE 2.5 | P1 |
| TOOL-5 | `cancel_subscription`. | README, IFACE 2.6 | P0 |
| TOOL-6 | `pause_subscription`. | README, IFACE 2.6 | P0 |
| TOOL-7 | `reactivate_subscription`. | IFACE 2.6 | P1 |
| TOOL-8 | `apply_subscription_discount`. | README, IFACE 2.6 | P0 |
| TOOL-9 | `update_subscription_address`. | README, IFACE 2.6 | P1 |
| TOOL-10 | `partial_order_refund`. | README, IFACE 2.7 | P0 |
| TOOL-11 | `full_order_refund`. | README, IFACE 2.7 | P0 |
| TOOL-12 | `cancel_order`. | IFACE 2.7 | P1 |
| TOOL-13 | `update_order_address`. | README, IFACE 2.7 | P1 |
| TOOL-14 | `create_escalation`. | README, IFACE 2.8 | P0 |
| TOOL-15 | `save_transcript`. | README, IFACE 2.9 | P0 |
| TOOL-ERR | Graceful handling of injected latency + 7% `503`s: bounded retry/backoff, never improvise an outcome. | BRIEF, IFACE §5 | P0 |
| RETN-1 | Retention sequence mirrors Ashley: capture reason, offer discount/pause before cancel where appropriate. | BRIEF (Ashley-in-action), `variant_a` | P1 |
| CX-1 | **Empathy sandwich** on any negative-affect turn: open with *specific, restated* empathy → info/resolution → close with empathy. "Sorry your refund hasn't arrived yet," not "sorry that happened." | Playbook Tip 1 | P0 |
| CX-2 | **Emotion handling: mirror vs. reframe.** Match positive energy; for negative affect, stay calm, acknowledge, then reframe toward a productive frame (frustration→anticipation of resolution). Never match anger. | Emotions resource, Playbook Tip 1 | P0 |
| CX-3 | **Verbal-Aikido cadence: yield → advance.** Acknowledge emotion first to disarm, *then* present logic/solution. Repeat the emotion→logic cycle as needed. When company at fault → lead empathy; when customer at fault → lead gentle logic, then empathize. | Resource 2, Resource 1 | P0 |
| CX-4 | **Cue-to-switch detection.** The moment a caller stops venting and starts asking questions, stop apologizing and shift fully to logic/resolution. Over-apologizing past this point invites more demands. | Resource 2, Resource 1 (4-step) | P1 |
| CX-5 | **Power phrases + name usage.** Use the caller's name mid-conversation (not only in greeting); deploy confidence phrases where truthful ("this is really the best I can do," concession framing). **Guardrail:** never fabricate a manager conversation or VIP status the system can't back — truthfulness gate on every power phrase. | Playbook Tip 2 | P1 |
| CX-6 | **Confidence-over-speed pacing.** Do not name the caller or confirm a write instantly even when technically possible. Confirm identity before using the name; on a write, verbally look up → state what will change → execute → read back. A deliberate beat *raises* trust. | Speed/Verification resource | P0 |
| CX-7 | **Abusive-caller boundary ladder.** Irate-but-civil → 4-step de-escalation (listen, empathize/assure, state actions, repeat). Abusive (slurs, targeted attacks, persistent profanity) → warn, re-warn, then end the call. Requires a real hangup capability (see TOOL-END). | Resource 1 | P1 |
| TOOL-END | **End-call capability** (Twilio hangup / `<Hangup>` or clearing the Media Stream) so Ashley can terminate after the abuse ladder or a completed call. Not in the mock tool set — this is a telephony action, not a backend tool. | Resource 1 (derived), INFRA | P1 |
| SAFE-1 | Health/adverse-reaction protocol: cancel immediately, advise medical follow-up, skip retention. | BRIEF (Ashley-in-action #2) | P0 |
| SAFE-2 | Guardrails against prompt-injection / out-of-scope abuse. | KICK (Chipotle example) | P1 |
| DATA-1 | Brand is **Natural Nutrition** as source of truth; reference code's real brand names / promo codes are not literal values. | README | P0 |
| DATA-2 | Time-awareness anchored to the 2026 seed era for any "shipped X days ago" copy. | IFACE (gotcha #16) | P2 |
| DATA-3 | Document any added mock customers/scenarios. | BRIEF (deliverable #6), README | P1 |
| INFRA-1 | Phone-callable via Twilio Programmable Voice (Media Streams). | BRIEF, README | P0 |
| INFRA-2 | Deploy agent **and** mock backend to a reachable host; one clone + a few steps; a non-technical tester can call cold. | KICK (Sean) | P0 |
| OBS-1 | Record each call (audio). | BRIEF (deliverable #5) | P0 |
| OBS-2 | Transcribe + persist transcripts (mock `/transcripts` and/or file). | BRIEF #5, README | P0 |
| OBS-3 | Call-review UI: list, audio player, transcript, basic metadata. | BRIEF #5, KICK | P0 |
| DOC-1 | `DECISIONS.md` — self-written: stack + tradeoffs, what was cut, prompt-for-voice adaptation, latency/confirmation/ambiguity handling, + retro. | BRIEF #4, KICK | P0 |
| DOC-2 | `README`/`TESTING.md` — install/run, keys, point a Twilio number, recommended scenarios, known limits. | BRIEF #3 | P0 |
| DOC-3 | 5-min Loom: 2–3 scenarios end-to-end + stack rationale. | BRIEF #2 | P0 |

---

## 2. Architecture (Path 1)

```
 Caller ──PSTN──► Twilio Number
                    │  (Programmable Voice, <Connect><Stream> bidirectional Media Streams, μ-law 8k)
                    ▼
        Orchestrator (Python / FastAPI + websockets)
          • Twilio media-stream WS  ◄────────────►  OpenAI Realtime API WS (speech-to-speech)
          • Per-call session state: {call_id, customer, verified: bool, candidate_account}
          • Tool dispatch: Realtime function_call ─► Tool Layer ─► Natural Nutrition mock
          • Deterministic filler on tool dispatch (see §3)
          • Recording + transcript capture (OBS-1/2)
                    │
                    ├──► Tool Layer (httpx async client) ──► Mock backend (localhost / deployed)
                    │
                    └──► Call store (sqlite or files) ◄── Call-review UI (read-only)
```

**Language:** Python/FastAPI. Justification for `DECISIONS.md`: stays adjacent to the reference
agent's Python `FunctionTool` shapes (direct reuse of arg schemas), and the Realtime transport is a
plain websocket so Node offers no transport advantage here.

**Why Path 1 over Path 3 (for `DECISIONS.md`):** higher delivery floor inside 10 days — native
streaming + native barge-in + the reference agent's tool definitions reused directly, versus the
latency-tuning and component-assembly cost of a composed Pipecat/LiveKit pipeline. Tradeoff accepted
and documented: Path 3 offers finer barge-in control and provider flexibility (swap STT/TTS/LLM
independently); Path 1 couples us to OpenAI's stack and gives less low-level control over endpointing.
We choose certainty of a polished ship.

---

## 3. Filler-phrase reassessment (CONV-2): **still P0, but re-scoped**

### Verdict
**Still required, downgraded in scope.** It is no longer a broad "cover every turn" subsystem; with
the Realtime API, conversational turns are handled natively and stay under the bar. It becomes a
**targeted tool-call-latency cover**. Still P0 because the most common and worst-case tool reads blow
past the ~1s threshold, and "no awkward silence" is an explicit non-negotiable.

### The threshold
Kickoff: ~1s perceived gap is the goal; faster is better. So anything that risks >~1s of silence
needs a cover.

### Where the silence actually is
Realtime is speech-to-speech, so a **reply with no tool call** has only the model's
endpointing + first-audio latency (~0.5–0.8s typical) → **under threshold, no filler needed.**
Dead air lives in the **tool-call window**:

```
silence(tool) ≈ model_emit_function_call + mock_backend_round_trip + model_resume_with_result
```

The model overhead on each side (emit the call, then start speaking once the result returns) is
roughly **0.4–0.7s combined**. The variable that dominates is the mock backend, and INTERFACE.md
gives the exact numbers.

### The latency math (from IFACE §5, default chaos on)
- Ambient per request: `uniform(300, 1500)` ms → mean ~900, worst 1500.
- Paths ending `/subscriptions`: **+1200 ms** on top → range **1500–2700** ms, mean ~2100.
- `MOCK_ERROR_RATE = 0.07` → 7% of requests `503`, forcing a retry (~doubles that request).

Tool-call silence windows (mock RTT + ~0.55s model overhead, midpoint):

| Call type | Mock RTT (range / mean) | + model ~0.55s → silence (mean) | Worst case | vs 1s bar |
|-----------|-------------------------|----------------------------------|-----------|-----------|
| Conversational (no tool) | — | ~0.5–0.8s | ~0.8s | ✅ under |
| Normal endpoint (lookup, orders, products, all mutations) | 300–1500 / ~900 | **~1.45s** | ~2.05s | ❌ over |
| `/subscriptions` read | 1500–2700 / ~2100 | **~2.65s** | ~3.25s | ❌ way over |
| Any call that hits a 503 retry (7%) | ~2× above | **~2.4s–5s+** | 5s+ | ❌ way over |

**Conclusion:** every tool call exceeds the 1s bar in the common case; the subscriptions read and the
retry path are guaranteed dead air. Filler stays P0 — but as a tool-call cover, not a per-turn system.

### Implementation change (driven by Path 1)
Do **not** use pre-recorded WAV clips — a second voice over the Realtime model's voice is jarring.
Use a **deterministic, application-level trigger**: when the orchestrator sees a function call being
dispatched, immediately issue a short spoken acknowledgment **in the model's own voice** ("Let me pull
that up…"), then send the real `response.create` once the tool result returns. Prompt-level "narrate
naturally before tools" is a *complement*, not the guarantee — model behavior on speak-then-call is
inconsistent, so the deterministic dispatch hook is what actually closes the gap.

### Free latency win to flag (IFACE)
`/customers/lookup` returns `{customer, orders, subscriptions}` in one round-trip and its path ends in
`/lookup`, **not** `/subscriptions` — so it does **not** incur the +1200ms penalty. Seed subscription
data from the lookup; only call the slow `/customers/{id}/subscriptions` when you need a fresh read
after a mutation. This removes the slow endpoint from the most common path entirely.

---

## 4. Auth / verification flow (AUTH-1/2/3) — rebuilt on real mock data

**Core fact (IFACE §6):** the mock trusts whatever identifier you pass — no challenge, no
verification. If the agent sends `phone=+15125550101` it gets Margaret Chen's full record. So **all
identity confirmation must live in the agent**, and it must gate *tools*, not just live in the prompt
(a prompt can be talked around; prompt-injection is explicitly in scope — SAFE-2).

**Identifiers the lookup accepts (exactly one):** `phone`, `email`, `order_number`.
Passing two or more → `400 validation`. So: look up with one, **verify with a different one**.

### Tier 0 — Caller-ID (happy path, AUTH-1)
Twilio gives the inbound `From`. Normalize to digits and `GET /customers/lookup?phone=<from>`.
On a hit, mark the account as *located*. The brief says callers from the on-file number "don't need
much authentication," so a single light confirmation ("Am I speaking with Margaret?") is enough to
flip `verified = true`.

### Tier 1 — Locate when caller-ID misses (AUTH-2)
This is the evaluators' main path (they call from the same non-seeded number repeatedly) and the
`cust_006` Robert Lee path (`phone: null`). Ask for **one** identifier the lookup accepts — **order
number** ("what's your most recent order number?") or **email** — and look up with that.

### Tier 2 — Verify (AUTH-3, the actual challenge)
Because the mock won't verify, after locating the account confirm a **second, independent** fact the
agent checks against the retrieved record (normalized compare, in agent code). All backed by real
fields:

| Challenge question | Field checked (IFACE) |
|---|---|
| "What's the ZIP on file?" | `shipping_address.zip` (order or sub) |
| "What's the email on the account?" (if located by order #) | `Customer.email` (case-insensitive) |
| "Your most recent order number?" | `order_name` / `order_id` |
| "Roughly when was your last order / what was in it?" | `created_at` / `line_items[].title` |
| "Last four of the card on file?" | `transactions[].card_last_four` — **from the SALE txn only** |

Gate every account-data disclosure and every mutation tool behind `verified == true`.

### Edges to handle (all from real seed data)
- **`cust_006` Robert Lee** `phone: null` → cannot match by phone; forces email/order lookup. Direct test of Tier 1.
- **`cust_004` David Thompson** two active subs (`50004`, `50005`) → must disambiguate before any sub action (CONV-3).
- **Order `shipping_address.phone` is not a caller-ID signal** — Robert's order ships to `+15125550106`, which matches no customer (IFACE gotcha #17). Do not "authenticate" off an order's shipping phone.
- **Refund transactions carry `null` card** (IFACE gotcha #7) — only the SALE txn has `card_last_four`; never use a refund row for the last-four challenge.

---

## 4.5. Conversation & emotional-handling model (CX-1..7) — *the "delight" layer*

> **Why this is its own section, not Day-9 polish.** Sean's "good vs. great" bar is entirely here:
> the customer walks away *delighted*, not just resolved. The four CX resources describe **testable,
> promptable behaviors** that must be designed into the voice prompt (and, for CX-6/CX-7, into
> orchestration) from the start. Retro-fitting empathy on Day 9 produces exactly the "AI slop" Ryan
> warned against. These behaviors are graded on the week-2 cold call, so they are P0-adjacent, not cosmetic.

### The core loop: **Yield → Advance** (CX-3)
Every emotionally-charged turn follows the Verbal-Aikido rhythm: **acknowledge the emotion first
(yield), then present the fact/solution (advance)** — and repeat the cycle. This disarms the power
struggle before logic lands. Fault direction sets the opening move:
- **Company at fault** (late package, billing error) → lead with empathy, then solution.
- **Customer at fault / misunderstanding** → lead with gentle logic to educate, then an empathetic
  transition to the fix. Don't over-apologize for something that isn't a company failure.

### The **cue to switch** (CX-4) — the single most testable behavior
The moment the caller **stops venting and starts asking questions**, stop apologizing and shift fully
to resolution. Continuing to apologize past this cue reads as weak and invites escalation. In a voice
context this cue is detectable (interrogatives, "so what can you do," a drop in affect) and should
flip Ashley's register from empathy-weighted to action-weighted.

### **Empathy sandwich** on every negative turn (CX-1)
Structure: *specific restated empathy → info + resolution → empathy close.* Specificity is the whole
game — "I'm sorry your D3 order hasn't shipped yet" beats "sorry about that." Reuse the caller's own
words where possible (the deck's retention save does exactly this: "when you've got a supply built up…").

### Mirror vs. reframe (CX-2)
Mirror **positive** energy (share the excitement). For **negative** energy, do **not** mirror —
stay calm, acknowledge, and **reframe** toward a productive emotion (frustration → anticipation:
"understandably frustrated, and — good news — about to have this sorted"). Matching anger escalates.

### Power phrases & name usage (CX-5) — **behind a truthfulness gate**
Deploy the caller's name mid-conversation ("I really want to get this sorted for you, Margaret"),
and confidence phrases where they're *true*: "this is really the best I can do" to close a
negotiation loop; effort/concession framing on a genuine goodwill gesture. **Hard guardrail:** never
fabricate "I spoke with my manager" or "VIP customer" when no such thing happened — a voice agent
inventing a manager is both dishonest and a demo-killer if a caller probes it. Power phrases are a
persuasion tool, not a license to lie; every one must map to something real in the system/policy.

### Confidence-over-speed pacing (CX-6) — *partially inverts the latency instinct*
Counterintuitive but load-bearing: **instant competence reads as untrustworthy.** Three rules:
1. **Confirm before you personalize** — don't greet "Hi Margaret!" off a caller-ID match; confirm
   identity first ("am I speaking with Margaret?"), *then* use the name. Instant name-use feels
   surveillant. (This is the same instinct as the auth confirm-step in §4.)
2. **Narrate the work on writes** — even though a mutation returns in <1s, verbally "look it up,"
   **state what will change**, execute, then **read back the result**. "Let me pull that order up…
   okay, I've got the D3, shipping to Austin — I'm going to cancel that now… done, that's cancelled,
   no further charges."
3. **A deliberate beat is a feature.** Some of the tool-call latency §3 treats as a problem to *cover*
   is, on writes, latency the customer *wants to feel*. Filler covers dead air; it should **not**
   race to make a destructive action feel instantaneous. Cover reads for speed; pace writes for trust.

> **Interaction with §3 (important):** §3 optimizes to *minimize* perceived latency. CX-6 says on
> **writes**, don't minimize below the "felt-processed" threshold. These aren't in conflict once
> separated by call type: **reads → cover and speed up; writes → confirm, narrate, and let a beat land.**

### Abusive-caller boundary ladder (CX-7) — the path the plan was missing
SAFE-2 covered prompt-injection; it did **not** cover a caller who is simply abusive. Rico will test this.
- **Irate but civil:** run the 4-step loop — *listen without interrupting* (let them vent; barge-in
  tuning must not cut a venting caller off mid-sentence), *empathize/assure*, *state specific actions*,
  *repeat as needed*. Positive scripting: hand them small bits of progress to lift the register.
- **Abusive** (slurs, targeted personal attacks, persistent profanity after a chance to reset):
  **warn once** ("I want to help, and I can't do that while being spoken to this way"), give a path back,
  **re-warn**, then **end the call** cleanly. This requires a real hangup (**TOOL-END**) — a telephony
  action, not a mock backend tool. Design the state machine so "abuse strikes" is tracked in
  orchestration state, not left to the model's discretion.

### Voice-stack caveat — emotion delivery on Realtime (not ElevenLabs)
The Emotions resource frames emotional delivery around **ElevenLabs V3 emotion tags** (`[sad]`, etc.).
**Path 1 uses OpenAI Realtime, which has no equivalent tag API.** Emotional prosody must be steered via
**persona prompting and per-`response` `instructions`** (e.g., instructing a warmer, slower register on
a health/adverse-reaction turn), not tags. Flag in DECISIONS.md as a concrete tradeoff of the Path-1
voice choice: less granular emotion control than a TTS-tag pipeline, mitigated by persona + response-level
steering. (See Risks #9.)

---

## 5. Tool definitions — exact, from INTERFACE.md

Base URL `http://localhost:8001` (deployed URL in prod). All bodies JSON. Error envelope:
`{ "error": { "code": "<code>", "message": "..." } }`.

> **Naming traps to encode once and not slip on:**
> - Lookup param is **`order_number`**; order mutations use **`order_id`** (full GID).
> - `order_id` goes in the **body** for all four order mutations (GID contains slashes).
> - Discount **body** field is **`discount_pct`**; the Subscription **response** field is `discount_percentage`. Don't cross them.
> - `refund_percentage` must be one of **`[10, 20, 25, 30, 35, 40, 50, 60]`**; 100% → `/orders/refund/full`.
> - Address bodies have **no `name`/`phone`** — those are preserved server-side; you can't change recipient name.

### Reads

**TOOL-1 `customer_lookup`** → `GET /customers/lookup`
Query (exactly one): `phone` | `email` | `order_number`. Returns `{customer, orders, subscriptions}`.
404 on miss; 400 if zero or multiple identifiers.

**TOOL-2 `get_customer_orders`** → `GET /customers/{customer_id}/orders` → `{orders: Order[]}` (newest-first). 404 if unknown.

**TOOL-3 `get_customer_subscriptions`** → `GET /customers/{customer_id}/subscriptions` → `{subscriptions: Subscription[]}`. **+1200ms slow endpoint.** Prefer lookup's bundled subs (§3).

**TOOL-4 `get_product` / `list_products`** → `GET /products/{sku}` → `Product`; `GET /products` → `{products: Product[]}`. 404 on unknown sku.

### Subscription mutations — `subscription_id` (int) in the path; all return the full `Subscription`

**TOOL-5 `cancel_subscription`** → `POST /subscriptions/{subscription_id}/cancel`
Body: `{ "reason": <enum> }` (default `"other"`). Enum (exact): `too_much_product`,
`cant_afford_the_product`, `didnt_want_a_subscription`, `didnt_like_the_product`,
`found_a_better_alternative`, `going_on_a_trip`, `dont_need_the_product_anymore`, `other`.
Errors: 404; 400 bad reason; 409 if already CANCELLED.

**TOOL-6 `pause_subscription`** → `POST /subscriptions/{subscription_id}/pause`
Body: `{ "pause_months": <int 1–6> }` (default 1). Errors: 404; 400 out of range; 409 if CANCELLED.

**TOOL-7 `reactivate_subscription`** → `POST /subscriptions/{subscription_id}/reactivate`
No body. Errors: 404; 409 if already ACTIVE.

**TOOL-8 `apply_subscription_discount`** → `POST /subscriptions/{subscription_id}/discount`
Body: `{ "discount_pct": <int 0<pct<100>, "code": <str> }` (defaults `20` / `"LOYAL20"`).
**Destructive / non-idempotent:** recomputes `total_value *= (1 - pct/100)`; calling twice compounds.
Errors: 404; 409 if CANCELLED; 400 if pct ∉ (0,100).

**TOOL-9 `update_subscription_address`** → `POST /subscriptions/{subscription_id}/address`
Body (`AddressBody`): `{ "address1", "address2"="", "city", "province", "country"="United States", "zip" }`.
No `name`/`phone`. Errors: 404; 409 if CANCELLED.

### Order mutations — `order_id` (GID) in the **body**; all return the full `Order`

**TOOL-10 `partial_order_refund`** → `POST /orders/refund`
Body: `{ "order_id": "gid://shopify/Order/<n>", "refund_percentage": <one of 10,20,25,30,35,40,50,60> }`.
Appends a REFUND txn (card fields null); sets `PARTIALLY_REFUNDED` or `REFUNDED`. Errors: 404; 400 bad pct; 409 if already REFUNDED.

**TOOL-11 `full_order_refund`** → `POST /orders/refund/full`
Body: `{ "order_id": "gid://shopify/Order/<n>" }`. **Side effect:** if the order was UNFULFILLED it
**also sets `cancelled_at`** — surface that combined effect to the caller. Errors: 404; 409 if already REFUNDED.

**TOOL-12 `cancel_order`** → `POST /orders/cancel`
Body: `{ "order_id": "gid://shopify/Order/<n>" }`. Errors: 404; 409 if already cancelled; **409 if not
UNFULFILLED** (message: already shipped — offer a refund instead). The agent must branch on
fulfillment status before promising a cancel.

**TOOL-13 `update_order_address`** → `POST /orders/address`
Body (`OrderAddressBody` = `AddressBody` + `order_id`):
`{ "order_id", "address1", "address2"="", "city", "province", "country"="United States", "zip" }`.
(Per IFACE 2.7, only an unfulfilled, un-cancelled order can have its address changed → 409 otherwise.)

### Side-effect tools

**TOOL-14 `create_escalation`** → `POST /escalations`
Body (`EscalationBody`): `{ "customer_id"?, "customer_details"?, "actions_taken"?, "issue_for_human" (REQUIRED), "mark_high_risk"=false }`.
Returns thin envelope `{ "escalation_id", "status": "queued" }` (not the full record).

**TOOL-15 `save_transcript`** → `POST /transcripts`
Body (`TranscriptBody`): `{ "call_id" (REQUIRED), "customer_id"?, "caller_phone"?, "transcript" (REQUIRED), "summary"?, "outcome"?, "recording_url"? }`.
Returns `{ "transcript_id", "saved": true }`; also appends one JSON line to `transcripts.log`.

---

## 6. 10-day breakdown (mapped to requirement IDs)

> Path-1 savings vs the original plan are reinvested into polish, edge-case coverage, and conversation
> quality. Day-2 deployment is front-loaded so we never debug telephony locally only to find it breaks
> on the host. Tool-signature confirmation against the repo is **already done** (INTERFACE.md is the
> source of truth), so Day 1 spends that time on the voice loop and the verification state machine
> skeleton instead.

### Day 1 — Mon Jun 29 · Voice loop up *(reclaimed from signature-confirmation)*
- Twilio number ↔ Media Streams ↔ OpenAI Realtime bridge working end-to-end in a single `main.py`; warm voice, real phone. → INFRA-1, VOICE-1
- Native barge-in verified over the PSTN (server-VAD `speech_started` → cancel OpenAI response + clear Twilio buffer). → CONV-1
- Transcript capture: caller + agent utterances logged to console — foundation for OBS-2 persistence later. → OBS-2 (foundation)
- Minimal per-call session dict: `{stream_sid, call_sid, started_at, transcript_in, transcript_out}`. **No tool layer yet, no `verified` / `candidate_account` fields yet** — those land in the Day 3 tool-layer scaffold and the Day 4 auth state machine, respectively.
- **Realtime GA schema wired correctly** (see Risk #13): session-config format for `gpt-realtime` is not what most tutorials show; getting this right on Day 1 avoided a full week of confused debugging.

### Day 2 — Tue Jun 30 · Deploy early (front-loaded) + recording
**Target chosen: Fly.io, single machine in `iad` (Ashburn, VA).** Rationale — closest Fly region to Twilio's primary US carrier infra and OpenAI's us-east; internet-exchange density in Ashburn makes peering to both a first-class hop. No scale-to-zero (`auto_stop_machines = "off"`, `min_machines_running = 1`) so an evaluator cold-call never hits a cold start.

**Done (staged for deploy):**
- `Dockerfile` — single-stage `python:3.13-slim`, uvicorn on `0.0.0.0:${PORT}`, `PORT=8080` to match `fly.toml.internal_port`. → INFRA-2 (image)
- `.dockerignore` — excludes `.env`, `.venv`, `__pycache__`, `.git`, docs, agent state; secrets never in the image. → INFRA-2 (secret hygiene)
- `fly.toml` — `iad`, `internal_port=8080`, `force_https=true`, WS-friendly concurrency (`type="connections"`, `soft=25 / hard=50`), `shared-cpu-1x / 512mb`. → INFRA-2 (config)
- `DEPLOY.md` — flyctl steps (create app → `fly secrets set` for `OPENAI_API_KEY` / `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` → `fly deploy`), Twilio webhook repoint instructions, and where the second (mock-backend) Fly app will slot in. → DOC-2 (deploy runbook)

**Done (executed and live):**
- **Bridge deployed.** `fly apps create nn-voice-agent` → `fly secrets set` (`OPENAI_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`) → `fly deploy`. Two machines warm in `iad`, `session.updated` confirmed in `fly logs` on the first live call. → INFRA-2 (bridge)
- **Mock backend co-located on Fly** (Option A). `fly apps create nn-mock-backend` → `fly deploy` from `../rtp-ashley-voice/mock-backend/` (added a minimal single-stage Dockerfile + `fly.toml` + `.dockerignore`; mock app code untouched). Same region `iad`; two machines warm; chaos defaults intentionally left on (300–1500ms + 1200ms on `/subscriptions` + 7% 503s). **Public IPs released** with `fly ips release` after first deploy — service is private-only, reachable only over Fly's 6PN. → INFRA-2 (mock, private co-located)
- **Bridge rewired to internal address.** `main.py` reads `MOCK_BACKEND_URL` (default `http://localhost:8001` for local dev, set to `http://nn-mock-backend.internal:8001` on Fly via `fly secrets set`). At startup the bridge does one disposable `GET /health` against the mock and logs the result — Day 3's tool layer replaces this with the real httpx client. Live confirmation from `fly logs -a nn-voice-agent`: `Mock backend reachable at http://nn-mock-backend.internal:8001/health — 200 {"ok":true}`. → INFRA-2 (wire)
- **INFRA-2 satisfied — full system is cold-callable without the dev laptop.** Both apps warm in `iad`, private 6PN hop between them, Twilio webhook pointed at the deployed bridge, no localhost dependencies.
- Twilio number `+1 956-906-8451` voice webhook: `https://nn-voice-agent.fly.dev/incoming-call`. → INFRA-2
- **Real-phone RTT baseline captured** on the deployed `iad` host: conversational (no-tool) turns measured **~0.4–2.0s** from `speech_stopped` to `response.output_audio.done`. Matches the §3 estimate (~0.5–0.8s typical + real-world telephony jitter) and stays under the ~1s perceived bar in the common case. **Confirms the §3 conclusion:** filler is not needed on conversational turns; the tool-call window is still where filler matters. → CONV-5
- **Live-call observations** — see Risks #14 (noise-driven false barge-ins) and Day 6 addendum (`response.cancel` guard + VAD tuning).

**Day 2 co-location gotcha (documented in `mock-backend/fly.toml`):**
- Uvicorn with `--host ::` on Linux binds **IPv6-only** because asyncio explicitly sets `IPV6_V6ONLY=1` on the socket, even though the kernel default `bindv6only=0` would allow dual-stack. Fly's `[[http_service.checks]]` uses IPv4 loopback and refuses against a v6-only bind, so the mock's HTTP check was dropped. The bridge's startup probe is the real reachability signal; Fly's machine supervisor still restarts crashed processes. **Do not "fix" this by binding `0.0.0.0`** — that will break bridge→mock 6PN traffic (`.internal` DNS is v6-only).

**Still pending on Day 2 (rolls into Day 3 alongside the module refactor + tool layer):**
- Persist transcripts to file/`save_transcript` beyond console logging; wire Twilio Recording for call audio. → OBS-1, OBS-2
- Structured tool-layer scaffolding in the bridge (httpx client keyed on `MOCK_BACKEND_URL`, session-state dict with `verified`/`candidate_account`). The Day 2 startup probe is disposable placeholder code; Day 3 replaces it with the real client.

### Day 3 — Wed Jul 1 · Tool layer (P0 reads + core mutations)
- Implement and unit-test against the mock: `customer_lookup`, `get_customer_orders`, `get_customer_subscriptions`, cancel/pause/discount sub, partial/full refund. → TOOL-1,2,3,5,6,8,10,11
- Bake in the naming traps (`order_id` in body, `discount_pct` vs `discount_percentage`, allowed refund pcts). → TOOL-* correctness
- Wire `TOOL-ERR`: bounded retry w/ backoff on `503`, typed error → spoken-safe messaging; never improvise an outcome. → TOOL-ERR, SAFE-1 adjacency

### Day 4 — Thu Jul 2 · Auth/verification state machine
- Tier 0 caller-ID lookup from Twilio `From`; Tier 1 fallback (order #/email); Tier 2 challenge against real fields; gate all mutations + data disclosure behind `verified`. → AUTH-1,2,3
- Edge tests: `cust_006` (no phone), evaluator-style call from a non-seeded number, ignore order `shipping_address.phone`. → AUTH-2/3 edges
- **Wk-1 checkpoint readiness.** → CONV-5

### Day 5 — Fri Jul 3 · Prompt-for-voice + **CX behavior spec baked into the prompt** + P0 side-effect tools + TOOL-END
- Adapt `variant_a` for voice: strip markdown/bullets, keep persona, retention sequence, escalation rules; short turns. → CONV-4, RETN-1, DATA-1 (Natural Nutrition substituted throughout)
- **Encode the CX layer directly in the prompt (not Day-9):** empathy-sandwich structure on negative turns, yield→advance cadence, mirror-vs-reframe rule, cue-to-switch heuristic, power-phrase list *with the truthfulness gate*, and the confirm-before-name / narrate-the-write pacing rules. → CX-1,2,3,4,5,6
- Write per-`response` `instructions` presets for emotional register (warmer/slower on health + upset turns) since Realtime has no emotion tags. → CX-2, Risk #9
- P0 side-effect tools: escalation, transcript. → TOOL-14, TOOL-15
- **TOOL-END wiring:** implement the Twilio hangup path (TwiML `<Hangup>` via a call-update, or terminating the Media Stream from the orchestrator) so the state machine can actually end an abusive call. Small addition next to the prompt work, but *without it the CX-7 abuse ladder has no teeth* — it's a no-op the model will just talk around. → TOOL-END, CX-7
- Disambiguation behavior for `cust_004`. → CONV-3
- *(P1 tools TOOL-4/7/9/12/13 moved to Day 6 alongside the filler work — Day 5 was overloaded.)*

### Day 6 — Mon Jul 6 · Filler-cover + no-dead-air + P1 tool tail
- Deterministic filler on tool dispatch in the model's own voice; gate to tool calls; verify the subscriptions/retry paths no longer leave silence. → CONV-2
- Apply the `/customers/lookup` bundling win to avoid the slow endpoint on the common path. → CONV-2/5
- Re-measure silence windows on the deployed host against the §3 table. → CONV-5
- **`response.cancel` guard (from Day 2 live-call finding).** Track an `active_response` flag on the session dict — set on `response.created`, cleared on `response.done` / `response.output_audio.done` — and only emit `response.cancel` (and the Twilio `clear`) when it's true. Stops the `response_cancel_not_active` error spam we saw on Day 2. → CONV-1
- **VAD tuning after quiet-room retest (from Day 2 live-call finding).** Baseline settings are `threshold: 0.5`, `prefix_padding_ms: 300`, `silence_duration_ms: 500`. First run a quiet-room retest to isolate whether the false-barge-in loop was noise-driven or the threshold; only then tune (raise threshold, widen `silence_duration_ms`, or switch to `semantic_vad` if `server_vad` proves too permissive). Tuning must not clip a venting-but-civil caller (Risk #12 / CX-7 interaction). → CONV-1, Risk #14
- **P1 tool tail (moved from Day 5):** reactivate, sub/order address, cancel_order, products. Same naming-trap discipline as Day 3 (`order_id` in body, address bodies no name/phone, cancel_order branch on `fulfillment_status`). → TOOL-4, TOOL-7, TOOL-9, TOOL-12, TOOL-13

### Day 7 — Tue Jul 7 · Call-review UI
- Lightweight read-only UI: call list, audio player, transcript, basic metadata (call_id, customer, outcome, duration). → OBS-3
- Persist transcript via `save_transcript` and/or file; link `recording_url`. → OBS-2

### Day 8 — Wed Jul 8 · Scenario hardening
- Drive the full seeded cast: happy subscriber, lost package, retention save, angry/legal-language caller, adverse-reaction (SAFE-1 immediate cancel + medical follow-up, skip retention), already-cancelled/shipped conflicts (409 branches). → SAFE-1, RETN-1, TOOL-12/11 edges, CONV-3
- **CX behavior under pressure:** script and run emotionally-charged role-plays — company-at-fault (lead empathy) vs. customer-at-fault (lead logic); verify the **cue-to-switch** actually flips register; verify empathy is *specific*, not generic. → CX-1,2,3,4
- **Abuse ladder end-to-end:** civil-irate → 4-step de-escalation without cutting off a venting caller; abusive → warn / re-warn / hangup via TOOL-END, with strike-count tracked in orchestration state. → CX-7, TOOL-END
- **Write-pacing check:** confirm Ashley narrates + reads back on mutations rather than firing them instantly. → CX-6
- Failure modes: unknown customer, tool error mid-action, caller silence. → TOOL-ERR
- Prompt-injection / out-of-scope guardrails. → SAFE-2

### Day 9 — Thu Jul 9 · Conversation-quality **tuning** *(behaviors already built Day 5/8 — this is calibration, not first-introduction)*
- Tune prosody, turn length, interruption feel, filler naturalness; remove any "AI slop" phrasing. → VOICE-1, CONV-1/2/4
- Calibrate the empathy/logic balance and power-phrase frequency by listening back to recorded calls; trim anything that sounds scripted or over-apologetic. → CX-1..5
- Verify barge-in thresholds don't clip a venting caller (CX-7 interaction with CONV-1). → CX-7, CONV-1
- Time-aware copy anchored to the 2026 seed era. → DATA-2
- Add + document any extra mock scenarios/customers. → DATA-3

### Day 10 — Fri Jul 10 · Deliverables
- `DECISIONS.md` — **self-written**: Path 1 vs Path 3 tradeoff, what was cut, prompt-for-voice adaptation, latency math (§3), auth design, **the CX/emotional-handling model (§4.5) and its read-vs-write pacing split**, retro. → DOC-1
- `README`/`TESTING.md`: run steps, keys, point a Twilio number, recommended scenarios, known limits. → DOC-2
- Record the 5-min Loom: 2–3 scenarios end-to-end + stack rationale. → DOC-3

### Jul 11–12 — Buffer
- Bug-fix from a full cold-call dry run; submit Mon Jul 13 before 10:00 AM ET.

---

## 7. Risks / things that will bite later

1. **Verification as prompt vs. state.** If the gate lives only in the prompt, prompt-injection or a chatty caller flips it. Gate tools in orchestration state. (AUTH-3, SAFE-2)
2. **Discount non-idempotency.** A retry or a re-offer that re-calls `apply_subscription_discount` compounds the discount. Make the tool call once-only per intent and confirm before re-calling. (TOOL-8)
3. **Full refund silently cancels unfulfilled orders.** The agent may promise "refund only" but also cancel. Detect UNFULFILLED and state both effects. (TOOL-11)
4. **Cancel-order 409 on shipped orders.** Don't promise a cancel before checking `fulfillment_status`; fall back to refund. (TOOL-12)
5. **Slow `/subscriptions` endpoint + 7% errors** are the dead-air and retry hotspots; the lookup-bundling win and the dispatch-time filler are the mitigations. (CONV-2/5, TOOL-ERR)
6. **Evaluators call from one repeated, non-seeded number** — caller-ID will never match for them, so Tier 1/2 must be solid, not an afterthought. (AUTH-2/3)
7. **Reference code has real brand names / promo codes** that aren't this project's values — substitute Natural Nutrition everywhere and pull data only from the mock. (DATA-1)
8. **Localhost-only latency lies.** Real numbers come from the deployed host over a real phone; that's why deploy is Day 2. (CONV-5, INFRA-2)
9. **Realtime has no emotion tags.** The Emotions resource's delivery advice assumes ElevenLabs V3 tags; Path 1 can't use them. Emotional register must come from persona prompting + per-`response` `instructions`. Less granular than a TTS-tag pipeline — document as a Path-1 tradeoff and mitigate with response-level steering. (CX-2, VOICE-1)
10. **Speed/pacing tension between §3 and CX-6.** §3 minimizes perceived latency; CX-6 says a destructive write should *feel* processed, not instant. Resolve by call type: **reads → cover + speed up; writes → confirm, narrate, read back.** If you blindly apply filler to make writes instant, you lose the trust signal. (CX-6, CONV-2)
11. **Power phrases can become lies.** "I spoke with my manager" / "VIP customer" from an AV agent is dishonest if nothing backs it, and a probing evaluator can expose it. Gate every power phrase on a real system/policy fact. (CX-5, SAFE-2)
12. **Abuse ladder needs a real hangup and state, not vibes.** Ending a call requires TOOL-END (Twilio hangup), and strike-counting must live in orchestration state or the model will forgive-and-forget mid-call. Also: barge-in tuning must not clip a *venting-but-civil* caller, or the 4-step de-escalation breaks at step 1 (listen). (CX-7, TOOL-END, CONV-1)
13. **Realtime GA schema ≠ beta schema.** The OpenAI Realtime GA (`gpt-realtime`) uses `session.type: "realtime"`, nested `audio.input.*` / `audio.output.*` config, format as `{"type": "audio/pcmu"}` (not the flat string `"g711_ulaw"`), renamed server events (`response.output_audio.delta`, `response.output_audio_transcript.done`, `response.output_audio.done`), `output_modalities` instead of `modalities`, no `temperature` at the session level, and **no `OpenAI-Beta: realtime=v1` header**. Most tutorials and the older Twilio-Realtime samples still show the beta schema. Reverting any of these — or copy-pasting an older example — silently closes the socket ~2s in with no error surfaced to the client. Already hit and fixed on Day 1; do not regress when adding tool definitions or refactoring `session.update`. (INFRA-1, VOICE-1)
14. **VAD is not noise-robust out of the box.** Day 2's first live call happened during heavy rain and the `server_vad` fired `speech_started` repeatedly on non-speech, driving a cut-off-and-re-greet loop. Evaluators (including Rico's cold-call role-plays) won't always be in quiet rooms — a café, a car, a windy sidewalk are all realistic. **The noise case is the useful stress test:** if barge-in behaves under noise, it behaves anywhere; if it only behaves in a quiet room, it fails the demo in the wrong place. Compounds with Risk #12 / CX-7 — tuning must not clip a venting-but-civil caller. Fix path: quiet-room retest first (isolate noise from threshold), then tune `threshold` / `silence_duration_ms`, or switch to `semantic_vad`. Also fix the unconditional `response.cancel` on every `speech_started` (Day 6 addendum): guard with an `active_response` flag so we don't spam `response_cancel_not_active`. (CONV-1, CX-7)
