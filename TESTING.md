# TESTING.md — Live-call test suite

*Start of the DOC-2 testing deliverable. Focus for now: manual phone-call test
coverage against the deployed Fly.io bridge. Setup/install lives in
[DEPLOY.md](DEPLOY.md).*

## Deployed state under test

- **Day 3 refactor** landed — `app/` package (main, bridge, session, config, tools).
- **P0 tool layer** wired — 10 tools (TOOL-1/2/3/5/6/8/10/11/14/15) via a shared
  httpx client with retry + backoff (`TOOL-ERR`), naming traps baked into
  schemas + handler defenses.
- **Discount lifecycle hardening** — cross-call stacking guard reads persisted
  `discount_percentage` before applying; within-call guard blocks the re-pitch
  case. See [DECISIONS.md](DECISIONS.md).
- **Escalation graceful-degradation** — unauthenticated callers still produce
  a usable escalation with `call_sid` as the correlation key in `customer_details`.
- **Proactive greeting (A1 fix)** — Ashley speaks first on pickup; no more waiting
  for the caller to say "hello" before anything happens.
- **Barge-in during Twilio playback (A2 fix)** — `active_response` replaced with a
  Twilio-timestamp + mark-queue playback tracker; `input_audio_buffer.speech_started`
  now sends `conversation.item.truncate` at the actual audio-out offset so the
  model's history matches what the caller heard.

**Not yet in place (do not test for these):**
- Auth / verification state machine (Day 4). Mutations fire without a
  verification challenge — that's expected here.
- Voice-adapted CX prompt, product knowledge, health guardrail (Day 5).
  Prompt is still the Day-1 placeholder — Ashley won't lead you through a
  script; be direct about what you want.
- Deterministic filler on tool dispatch (Day 6). Some tool-call gaps may
  feel long; that's known.
- P1 tools TOOL-4/7/9/12/13 (Day 6), TOOL-END (Day 5).

---

## Before you start

- [x] **Bridge is up**
  ```bash
  fly status -a nn-voice-agent
  ```
  ≥1 machine `started` in `iad`.

- [x] **Mock state reset to seed**
  ```bash
  fly ssh console -a nn-voice-agent -C \
    "python -c 'import httpx; print(httpx.post(\"http://nn-mock-backend.internal:8001/admin/reset\").text)'"
  ```
  Expect `{"reset":true}`.

- [x] **Logs tailing in a second terminal**
  ```bash
  fly logs -a nn-voice-agent
  ```

- [x] **Twilio number:** +1 956-906-8451

### Seeded identities you can call as

| Handle | Phone | Email | Subs | Orders |
|---|---|---|---|---|
| Margaret Chen | `+15125550101` | `margaret.chen@example.com` | 50001 (Magnesium, **ACTIVE**), 50010 (Vitamin D3 & K2, **PAUSED**) | 1000, 1001 — **both FULFILLED** |
| **Patricia Gomez** | `+15125550108` | `patricia.gomez@example.com` | *(check seed)* | **1008 — the only UNFULFILLED order in the seed. Only caller who can drive C4 (`also_cancelled`).** |
| David Thompson | *(see PLAN §4)* | | 50004, 50005 *(two active — disambiguation later)* | |
| Robert Lee | `null` | *(see PLAN §4)* | | *(forces email/order lookup)* |

> **Margaret has two subscriptions.** Any sub-related request that doesn't
> specify which one triggers CONV-3 disambiguation ("which subscription — the
> Magnesium or the Vitamin D3?") — that's *correct behavior*, not a broken
> tool call. Tests below say the specific sub by name; a separate item deliberately
> exercises the disambiguation path.

---

## A. Infrastructure smoke (fixes from Days 2–3 refactor)

- [x] **A1. Cold-call pickup — proactive greeting (fixed post-Day 3)**
  - **Do:** Dial. **Say nothing.** Wait.
  - **Watch logs for:** `Twilio WebSocket connected` → `Connected to OpenAI Realtime API` → `OpenAI event: session.created` → `session.updated` → `Stream started` → `Agent:  <greeting>`.
  - **Pass:** she greets you within ~2s of the stream starting, **without you saying anything first**. Startup line `Mock backend reachable at http://nn-mock-backend.internal:8001/health — 200 {"ok":true}` is in the log from the last deploy.
  - **Fail:** WS closes silently ~2s in → GA schema regressing (Risk #13). Don't proceed.
  - **Fail (previously seen):** dead silence on pickup, no `Agent:` line until you speak → the proactive `response.create` fix regressed. Check the `elif event == "start":` block in [app/bridge.py](app/bridge.py).

  **Findings (pre-fix)**
  - Ashley never greets UNTIL i say something first — *fixed by sending a `response.create` from the Twilio `start` handler in bridge.py.*

- [x] **A2. Barge-in during Twilio playback — proper truncation (fixed post-Day 3)**
  - **Do:** After A1 pickup, let Ashley start her greeting. **Interrupt her mid-word** (say "wait" or similar while she's still audibly speaking).
  - **Watch logs for:** exactly one `Barge-in — truncating item=<id> at audio_end_ms=<N>; clearing Twilio buffer`. `<N>` is how many ms of audio actually reached you before the interrupt (should be roughly `interrupt_time - greeting_start_time`). No stream of errors. No `response_cancel_not_active` lines.
  - **Pass:** she stops mid-word within ~200ms; the log shows the truncate with a plausible `audio_end_ms` (positive, less than the full greeting duration).
  - **Fail (previously seen):** Ashley keeps talking through the rest of her buffered audio, and no `Barge-in` line appears in the logs → the `mark_queue`-based gate isn't seeing audio in flight. Check `session.mark_queue` handling in `_twilio_to_openai` (the `mark` event branch) and `_openai_to_twilio` (delta handler appends, barge-in branch drains).
  - **Fail (log spam):** repeated `response_cancel_not_active` OpenAI errors → someone reverted to the old unconditional `response.cancel` shortcut. Don't do that.

  **Findings (pre-fix)**
  - Barge-in doesnt seem like it works? — *root cause: `active_response` cleared on `response.done` but Twilio kept playing buffered audio for 1–3s after; caller interrupts hit `active_response=False` and were silently skipped. Fixed by replacing `active_response` with a `mark_queue` + Twilio-timestamp-based playback tracker in session.py + bridge.py, and using `conversation.item.truncate` (with the actual `audio_end_ms` offset) instead of `response.cancel`.*

---

## B. Tool reads (P0)

- [x] **B1. Happy-path lookup — Margaret**
  - **Do:** *"Hi, I'm Margaret Chen, my phone is 512-555-0101."*
  - **Watch logs for:** `Tool call: customer_lookup({"phone":"+15125550101"})` → `Tool result: customer_lookup -> ok=True`.
  - **Pass:** she reads back something real — "you're subscribed to Magnesium Complex" or similar; details match seed data.
  - **Fail:** she names a product not in Margaret's seed (Vitamin D, Omega-3, etc.) → she's improvising. Note wording and hang up.


- [x] **B2. Bogus lookup — must admit ignorance**
  - **Do:** *"Hi, I'm Sarah Johnson, my phone is 999-999-9999."*
  - **Watch logs for:** `Tool call: customer_lookup(...)` → `Tool result: customer_lookup -> ok=False`.
  - **Pass:** she says she can't find the account; asks for another identifier (order number, email). **Must not invent a subscription or order.**
  - **Fail:** she "finds" Sarah's account → the improvise-on-404 failure the error-envelope design is meant to prevent.

- [ ] **B3. Sub read via bundled lookup (§3 optimization) — expect no slow-endpoint fire**
  - **Do (after B1):** *"I'm Margaret Chen, 512-555-0101. What subscriptions do I have?"*
  - **Watch logs for:** `Tool call: customer_lookup(...)` → `Tool result: customer_lookup -> ok=True`. **No `Tool call: get_customer_subscriptions`.**
  - **Pass:** she reads back **both** subs — Magnesium (ACTIVE) and Vitamin D3 & K2 (PAUSED) — from the lookup's bundled `subscriptions[]`. No +1200ms slow round-trip. This is the §3 "prefer bundled subs" optimization working as designed.
  - **Fail:** she names a sub not in Margaret's seed (Omega-3, etc.) → improvising. Names only one when she should name both → misreading the bundled data.

  > **Why `get_customer_subscriptions` won't normally fire.** `customer_lookup` returns `{customer, orders, subscriptions}` in one round-trip; the model uses that. The slow endpoint only earns its keep after a mutation when the cache is known-stale — see the paragraph after C-suite tests below for how to force it deliberately.

---

## C. Mutation happy paths (state changes — order matters)

*Run C1–C3 in the order listed; state carries between them. C4 needs a fresh reset.*

- [x] **C1. Pause — specify the sub by name**
  - **Do:** *"I'm Margaret Chen, 512-555-0101. Please pause my **Magnesium** subscription for two months."*
  - **Watch logs for:** `Tool call: pause_subscription({"subscription_id":50001,"pause_months":2})` → `Tool result: pause_subscription -> ok=True`.
  - **Pass:** she confirms the pause. Log line shows `subscription_id:50001` + `pause_months:2` — exact args.
  - **Fail:** `pause_months` off (0, 12, null) → schema constraint drift. Missing `Magnesium` in your request → she'll ask disambiguation and the tool won't fire.
  - **Note:** naming the sub explicitly skips CONV-3 disambiguation. To exercise the disambiguation path itself, see **C-DISAMBIG** below.

- [x] **C2. Cancel — specify the sub by name**
  - **Do:** *"Actually I want to cancel the **Magnesium** subscription. I'm going on a trip for a while."*
  - **Watch logs for:** `cancel_subscription({"subscription_id":50001,"reason":"going_on_a_trip"})` → `ok=True`.
  - **Pass:** the reason enum lands on `going_on_a_trip` verbatim. Ashley confirms.
  - **Fail:** reason like `traveling`, `trip`, `vacation` → free-form value the mock will 400. Enum should prevent this at the model layer.

- [x] **C-DISAMBIG. Deliberately exercise CONV-3 disambiguation (correct behavior)**
  - **Do:** *"I'm Margaret Chen, 512-555-0101. Please pause my subscription for two months."* (**omit** the Magnesium/Vitamin D name)
  - **Watch logs for:** `Tool call: customer_lookup(...)` → `ok=True`. **No** immediate `pause_subscription` call. Then Ashley's `Agent:` line asks something like "which one — the Magnesium or the Vitamin D3 & K2?".
  - **Pass:** she asks; she does not guess. No mutation fires until you clarify. Answer *"the Magnesium"* → then and only then does `pause_subscription({"subscription_id":50001,...})` fire.
  - **Fail:** she picks one silently → CONV-3 regression (violates *"ask when unsure"*).

- [x] **C3. Partial refund**
  - **Do:** *"Can I get a 25% refund on order NN1001?"* (`#NN1001` maps to `gid://shopify/Order/1001`.)
  - **Watch logs for:** `partial_order_refund({"order_id":"gid://shopify/Order/1001","refund_percentage":25})` → `ok=True`.
  - **Pass:** she confirms. `order_id` in the log is the **full GID** (not just `1001`) — naming trap holding.
  - **Fail:** `"order_id":1001` (int) or `"order_id":"1001"` (bare string). Handler should reject with `bad_request` before dispatch.


- [x] **C4. Full refund with side-effect surfacing — must call as Patricia**
  - **Prep:** Reset the mock first (Prereqs). **Call as Patricia Gomez, not Margaret** — order 1008 is the only UNFULFILLED order in the seed, and that's the only path that triggers the `also_cancelled` side effect.
  - **Do:** *"I'm Patricia Gomez, 512-555-0108. Refund order NN1008 in full — it hasn't shipped yet."*
  - **Watch logs for:** `full_order_refund({"order_id":"gid://shopify/Order/1008"})` → `ok=True`. In the result payload dump, look for `"also_cancelled":true`.
  - **Pass:** Ashley says **both** — "I've refunded it AND the order's been cancelled since it hadn't shipped."
  - **Fail:** she only mentions the refund → side-effect-not-surfaced (Risk #3). Handler flagged it; prompt-side language polish lands Day 5.
  - **Note:** if you call as Margaret (as the pre-fix version of this test said), both her orders (1000, 1001) are already FULFILLED in the seed — Ashley will correctly refuse to fabricate an "unshipped" order, which is right behavior but doesn't exercise the code path.

---

## D. Today's hardening — the whole point of this test sweep

- [x] **D1. Adverse-reaction cancel — must use `reason="other"`**
  - **Prep:** Reset the mock (state must be fresh with sub 50001 ACTIVE).
  - **Do:** *"I'm Margaret Chen, 512-555-0101. I've been dizzy and lightheaded since I started the magnesium last week. I want to cancel it."*
  - **Watch logs for:** `cancel_subscription({"subscription_id":50001,"reason":"other"})` — the reason **must** be `"other"`.
  - **Pass:** exact string `"reason":"other"` in the call log, `ok=True` in the result.
  - **Fail:** `"reason":"medical_issue"` → would 400 at the mock and Ashley would improvise recovery. Both schema enum and handler defense regressed if this fires.
  - **Bonus:** she often follows with `create_escalation({... "mark_high_risk":true})`. Check the log — that's the SAFE-1 hint working even without the Day-5 prompt.

  **Findings**
  - Works but she doesnt create the escalation

- [x] **D2. Within-call discount guard — cannot compound**
  - **Prep:** **Reset the mock** before this test — D1 cancelled sub 50001, so without a reset the ACTIVE Magnesium sub isn't there for the discount to land on and Ashley will correctly refuse ("your Magnesium is cancelled, would you like to reactivate?"). That's a different code path from the guard we want to test.
    ```bash
    fly ssh console -a nn-voice-agent -C "python -c 'import httpx; print(httpx.post(\"http://nn-mock-backend.internal:8001/admin/reset\").text)'"
    ```
  - **Do:** *"I'm Margaret Chen, 512-555-0101. Can I get a discount on my **Magnesium** subscription to keep it?"* → she calls `apply_subscription_discount` once with 20% (default).
  - **Then:** *"Actually, can you make it 30%?"*
  - **Watch logs for:** if she calls it a second time, `Tool result: apply_subscription_discount -> ok=False`. Error code will be `discount_already_active` (or `already_applied` — either is a correct rejection).
  - **Pass:** she does one of:
    - Doesn't call the tool again — reasons about it: "I can't apply another one on top."
    - Calls it, result `ok=False`, reads back "you've got a 20% discount already, it's still in effect."
  - **Fail:** she calls it, result `ok=True` with a NEW discount landed → both guards leaked. Verify by asking "what's my current discount now?" — she must not say 44% (compounded) or 50% (naive sum).


- [x] **D3. Cross-call discount guard — the new test**
  - **Prep:** Complete D2 first so the mock has persisted `discount_percentage:20` on sub 50001.
  - **Do:** Hang up. Redial. *"I'm Margaret Chen, 512-555-0101. Can I get a discount on my subscription?"*
  - **Watch logs for:** `customer_lookup` first (populates the cache). Then if `apply_subscription_discount` is attempted: `ok=False code=discount_already_active existing_discount_percentage=20`.
  - **Pass:** Ashley says something like "you already have a 20% discount on this subscription — it's still active." The number **20** must appear.
  - **Fail (a):** she agrees to apply another discount and result is `ok=True` → cross-call guard broken (this is exactly what today's hardening fixes).
  - **Fail (b):** she calls the tool, gets rejected, but then hallucinates a different existing percentage ("you have a 15% discount") → she's not reading `existing_discount_percentage` from the response.

- [x] **D4. Unauth escalation — must degrade gracefully**
  - **Prep:** Reset the mock.
  - **Do:** Dial in. Say something unfulfillable without lookup: *"I want to complain about a really bad customer service experience I had last week."* Do NOT identify yourself.
  - **Watch logs for:** eventually, `create_escalation({...})` with no `customer_id` provided. Result should be `ok=True escalation_id=...`.
  - **Pass:** she creates the escalation, tells you a human will follow up.
  - **Fail:** she refuses to escalate because she can't identify you → over-gating. The handler explicitly allows this path.
  - **Bonus check — verify the correlation key landed:**
    ```bash
    fly ssh console -a nn-voice-agent -C \
      "python -c 'import httpx; print(httpx.get(\"http://nn-mock-backend.internal:8001/admin/state\").json().get(\"escalations\", []))'"
    ```
    The escalation's `customer_details` should start with `[unverified caller — call_sid=CA...]`.

---

## E. Error path — retry visible in logs (observational)

The mock's 7% ambient 503 rate means retries fire naturally over a few calls. Don't force these — just note them when they happen.

- [x] **E1. Observed retry recovers cleanly**
  - **Watch during A–D for:** log lines like
    ```
    WARNING GET /customers/cust_001/orders attempt 1/3 — 503 forced_error: ...
    WARNING GET /customers/cust_001/orders attempt 2/3 — 503 forced_error: ...
    ```
    followed by a successful `Tool result: ... -> ok=True`.
  - **Pass:** call eventually succeeds; Ashley speaks the real answer. No apology, no dead-air complaint from you.
  - **Fail:** she says "sorry, I'm having trouble" but the log shows `attempt 1/3 — 503` followed by an `ok=True` at attempt 2 — she gave up mid-retry, which shouldn't be possible (retry is client-side and fully synchronous). If seen, something is weird.

- [ ] **E2. Exhausted retry — client-safe messaging (rare)**
  - Hard to trigger over a phone call — needs three consecutive 503s on the same request (~0.03% probability per attempt). Skip unless it happens organically.
  - **Pass:** Ashley says something like "I'm having trouble reaching our system" or offers a human callback. **Must not** invent an answer.

---

## How to deliberately exercise `get_customer_subscriptions` (the slow endpoint)

`get_customer_subscriptions` won't fire on a normal sub question — `customer_lookup` returns `{customer, orders, subscriptions}` bundled in one round-trip and the model uses that. This is the §3 optimization; the slow endpoint's +1200ms tax is the reason.

To see it fire live:

1. **After a mutation, ask for a fresh status.** *"I'm Margaret Chen, 512-555-0101. Please pause my Magnesium subscription."* → wait for `Tool call: pause_subscription -> ok=True` → *"Actually, can you double-check the current status of both my subscriptions?"* → she should now call `get_customer_subscriptions` because her cached view is known-stale post-mutation. Expected +1.5–2.7s log gap between `Tool call:` and `Tool result:` (ambient 300–1500ms + slow 1200ms).
2. **Or exercise it directly via the smoke test:**
   ```bash
   cd ../rtp-ashley-voice/mock-backend && ./run.sh   # separate terminal, local mock
   cd /Volumes/ACERFD/TECH/Raicom/nn-voice-agent && python -m scripts.test_tools
   ```
   The `TOOL-3 get_customer_subscriptions (SLOW endpoint)` case in the script calls it directly, so both the +1.5s round-trip and the handler correctness are proven independently of what the model chooses to do live.

---

## What a green sweep looks like

You should be able to hit **A1–A2, B1–B3, C1–C4, C-DISAMBIG, D1–D4** in ~15 min of calling (skipping E which is observational). If **A1** greets you unprompted, **A2** truncates cleanly mid-playback, and **D1 + D3** both pass — adverse-reaction reason maps to `"other"`, cross-call discount rejected with `discount_already_active` — the four things this rev cared about are proven live.

---

## Cheat sheet — one-liners

```bash
# Reset mock state (run between full test sweeps)
fly ssh console -a nn-voice-agent -C "python -c 'import httpx; print(httpx.post(\"http://nn-mock-backend.internal:8001/admin/reset\").text)'"

# Inspect mock state mid-test (see what actually landed)
fly ssh console -a nn-voice-agent -C "python -c 'import httpx, json; print(json.dumps(httpx.get(\"http://nn-mock-backend.internal:8001/admin/state\").json(), indent=2)[:4000])'"

# Tail bridge logs — the main observability surface
fly logs -a nn-voice-agent

# Filter for tool activity
fly logs -a nn-voice-agent | grep -E "Tool call|Tool result|Barge-in|OpenAI event: response"
```


