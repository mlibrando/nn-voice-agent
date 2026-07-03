# DECISIONS.md

> **Constraint (DOC-1).** This document must be self-written — the entries
> below are *drafts to review, edit, and reword in your own voice* before
> Day 10 hand-in. Anything Claude writes here should be treated as
> scaffolding, not final copy.

---

## Discount lifecycle — one per subscription, no stacking

> 🚧 **DRAFT — REVIEW AND REWORD** before this becomes canonical.

**Decision.** `apply_subscription_discount` enforces *at most one discount per
subscription, ever*. No re-application, no compounding — across calls or within
one.

**Why.** The tool is destructive and non-idempotent: the mock recomputes
`total_value *= (1 - pct/100)` on every call. Two calls at 20% land at 36%
off, not 20%. The failure mode isn't a crash — it's a silent price change of
the wrong amount, with no in-call audit trail. That makes this a correctness
gate, not a nicety.

**Implementation, two layers (belt-and-suspenders).**

1. **Cross-call stacking guard — persisted-state read.**
   Before applying, the handler checks the subscription's `discount_percentage`
   from `session.subscriptions_by_id` (populated by `customer_lookup` /
   `get_customer_subscriptions`, refreshed after mutations). If it's already
   set, the handler returns `code=discount_already_active` with the existing
   percentage — Ashley speaks the current discount back rather than silently
   stacking.

2. **Within-call re-pitch guard — session-side.**
   Once a discount succeeds in this call, `session.applied_discounts` records
   the subscription_id. Any further call for the same sub returns
   `code=already_applied`. Blocks retry loops after transient errors and the
   "retention" prompt tempting the model to re-offer after a caller "no".

In practice, layer 1 fires first — a successful apply updates the cache, so
subsequent calls in the same session are also caught by the state-based
guard. Layer 2 is the fallback for cases where the cache didn't update
(handler bug, race with a background mutation).

**Why not "one per intent".** "Intent" is fuzzy and not enforceable from
tool-call boundaries. "One per subscription" is concrete: the mock's
persisted state is the single source of truth, and both guards check against
it (cache for cross-call, session set for within-call). If a caller
*explicitly* asks for a second discount later, that's a manager escalation
via `create_escalation`, not a tool retry.

**Trade-off — cache staleness.** If a caller's subscription state was mutated
by a different channel (web dashboard, previous phone call whose `customer_lookup`
hasn't run yet) *after* the current lookup, the cache could be stale. This
would let a stacking call through. The alternative — a fresh `/subscriptions`
read on every discount — costs +1200ms per attempt (that's the endpoint's
slow tax) and isn't worth eating for a rare race. Documented, not fixed.

**Interaction with Day 4 auth.** The cross-call guard depends on
`customer_lookup` running before a discount is offered. Post-auth, lookup is
part of the Tier-0/Tier-1 flow — the cache is populated before Ashley reaches
any discount decision. Pre-auth, the tool layer is gated anyway (mutations
aren't offered to unverified callers), so the guard doesn't need to catch that
case.

---

## Located vs. verified — and why the dispatch layer is the gate

> 🚧 **DRAFT — REVIEW AND REWORD** before this becomes canonical.

**Decision.** `session["verified"]` is the single source of truth for identity,
enforced at the tool dispatch layer in `handlers.dispatch()`. Locating an account
(finding a match on caller-ID or on an order#/email lookup) is separate from
verifying identity, and only verified callers get account-data disclosure or
mutation execution. Every other pathway refuses with a structured
`code=verification_required` result the model speaks from — not the prompt.

**Why.** The mock has no verification. It returns the full customer record for
any identifier passed. If the gate lived only in the system prompt
(*"Ashley, don't disclose without verification"*), any prompt-injection or
chatty caller could talk around it — SAFE-2 is explicitly in scope. Putting the
gate at the handler-dispatch layer means the answer is a plain Python check:

```python
if name not in _PRE_AUTH_TOOLS and not session["verified"]:
    return _error("verification_required", "…")
```

The prompt cannot flip that. That's the whole point.

**Located vs. verified — why the distinction matters.** Caller-ID matching
identifies a *candidate* — a hypothesis that the person calling is the person
who owns the number. It is not proof. Phones get lent, spoofed, or found. Tier 0
adds a single confirmation ("am I speaking with Margaret?") to convert candidate
→ verified. Tier 1 (order# / email) is the same shape without the ambient
signal, so it needs a Tier 2 challenge (a fact the caller must independently
know). The `candidate_account` slot carries the "we found someone, they haven't
proven it" state — it is the reason the two are separate session fields.

**Pre-auth-allowed tools (whitelist).**

- `customer_lookup` — this is how we locate; must be pre-auth. But its response
  is sanitized when `verified=False` so the model can't leak the challenge
  answer to the caller.
- `verify_identity` — the tool that runs the check; obviously pre-auth.
- `create_escalation` — has an unauth breadcrumb path so legitimate callers
  who can't verify still get to a human. Escalation body includes call_sid +
  Twilio `from_number` as correlation keys.
- `save_transcript` — end-of-call persistence; runs regardless of auth outcome
  because we always want the record for ops.

Everything else runs post-verification only. Reads (`get_customer_orders`,
`get_customer_subscriptions`) are gated too — data disclosure is treated the
same as mutation.

**Locked-out degradation is self-contained.** After 3 failed challenge
attempts, `verify_identity` refuses further tries with `code=locked_out`. The
result is not just a code — it carries a caller-facing `spoken_line` plus a
prefilled `escalation_suggestion` (issue_for_human, actions_taken,
mark_high_risk). The model relays these into a `create_escalation` call
without needing prompt help. A legitimate caller on a bad line lands in the
escalation path even if the prompt is stripped or drifts.

**Tier-0 greeting phrasing: open confirmation, never name-disclosure.** On a
Tier-0 hit, Ashley greets openly ("who am I speaking with?" or "am I
speaking with the account holder?") and lets the caller state their name
first. She does NOT greet by name off caller-ID alone.

Rationale, three parts:
1. **Privacy.** Anyone can pick up Margaret's phone — spouse, kid, thief.
   Naming Margaret in the greeting tells them we have Margaret Chen as
   the account holder. That is a mini information disclosure before the
   caller has said anything.
2. **UX.** "Hi Margaret!" off nothing but caller-ID reads as surveillant.
   Callers don't expect to be identified before they've spoken.
3. **Auth strength.** "Am I speaking with Margaret?" invites a reflexive
   "yes." "Who am I speaking with?" forces the caller to *actively claim*
   an identity we can then verify against the located record — a stronger
   signal than passive confirmation.

The security model doesn't change: `session["verified"]` still flips only
via `verify_identity`; the `caller_id_confirm` challenge still accepts the
caller's name (matched via `_is_affirmative`) or any clear affirmation. Only
the phrasing shifts — Ashley never volunteers the name.

**Implementation note (open follow-up):** the current bridge greeting
instructions in `app/bridge.py` still tell Ashley to greet by first name on
Tier-0 hit. That contradicts this decision and needs to be updated as part
of Day 5 CX prompt work. When we do, tighten `_is_affirmative` at the same
time: today it matches any of `AFFIRMATIVE_WORDS` including "this is", so
"This is Bob" (a wrong name) would still verify against a `first_name` of
"Margaret". That's an existing latent bug that the open-greeting pattern
would surface more often; the fix is to make the name-in-string branch
authoritative and require it to actually equal the located first name when
present, falling back to bare-affirmative words only when no name-shaped
token is in the answer.

---

## Order number / order name as a moderate-strength knowledge factor

> 🚧 **DRAFT — REVIEW AND REWORD** before this becomes canonical.

**Decision.** Order number (`order_name` like `#NN1001`) is accepted as a
*locate* identifier in Tier 1 (`customer_lookup(order_number=…)`) and as a
*verify* challenge in Tier 2 (`verify_identity(challenge_kind="order_name", …)`).
It is treated as a knowledge factor of moderate strength — not a secret.

**Why the moderate framing.** Order numbers are visible on packing slips, email
receipts, shipping notifications, and the customer's account page. Anyone with
physical access to a caller's mail or their inbox has it. It is not sensitive
the way a card last-4 or an unpublished ZIP is. But it is also not zero — a
random attacker who doesn't know the caller has to guess a ~4-digit number
space, and the mock's chaos + our attempt-cap (3 fails → lockout) make random
guessing expensive.

**Practical rule.** Order name is fine as *one of two independent factors* —
locating on the order number is not itself verification. If the caller locates
via order number, the Tier-2 challenge must be something *different*: ZIP,
email, or card last-4. Do not accept the same fact twice.

**Correction to an earlier draft: this does NOT fall out naturally from the
challenge-kind design — it is enforced by an explicit state check.** An earlier
version of this entry claimed the model would pick a different challenge
because `caller_id_confirm` is Tier-0-only, so a Tier-1 locate "forces" the
model into the Tier-2 set. That's wrong: the Tier-2 set includes `email` and
`order_name`, so a caller who located via email could be re-verified by
confirming that same email — and in a live test, that is exactly what
happened. Ashley located David Thompson by email, then accepted the same
email as the challenge and flipped `verified=True`. Zero-factor
authentication via a prompt-guidance-only rule.

The fix: `verify_identity` reads `session["located_via"]` (recorded by
`customer_lookup` based on which identifier the caller supplied) and refuses
same-factor challenges at the handler with `code=same_factor`, before
evaluating the answer. `located_via == "email"` + `challenge_kind == "email"`
→ refused. `located_via == "order_number"` + `challenge_kind == "order_name"`
→ refused. The refusal does NOT count against the 3-attempt cap — the caller
didn't give a wrong answer; the model picked the wrong tool. The prompt still
reinforces the rule (belt), but the handler is the enforcement (suspenders),
consistent with the top-level rule that verification-as-state beats
verification-as-prompt.

**Tier-0 carve-out on same-factor: caller-ID is exempt.** The same-factor rule
does NOT apply to `caller_id_confirm` on a Tier-0 hit. Rationale: caller-ID
is an *ambient physical signal* delivered by Twilio's carrier layer, not a
caller-supplied claim. When `session["tier0_hit"] == True` (the caller's
inbound `From` matched a customer's on-file phone), combining that ambient
signal with a verbal "yes" is two factors, not one — physical channel plus
verbal channel. Compare to Tier-1: a caller who says "my email is X" and
then says "yes my email is X" is one caller-supplied fact confirming itself,
because the caller controls both statements.

The tier0_hit signal is safe as an ambient factor because the write path is
tightly controlled:
- `session["from_number"]` is set only by the bridge, from the Twilio
  Media Streams `start` event's `customParameters["from"]`, which the
  bridge populates from `/incoming-call`'s POST-form `From` field. The
  caller cannot inject at any layer above Twilio's carrier signaling.
- `session["tier0_hit"]` becomes True only when the phone passed to
  `customer_lookup` equals `session["from_number"]` AND the lookup
  succeeded. The model can control the lookup phone arg, but it must
  equal the bridge-captured `from_number` for the flag to fire, and the
  matching customer must actually exist on file.

If Twilio-level caller-ID spoofing ever becomes a real concern, we tighten
Tier-0 to require a Tier-2 challenge even on a caller-ID match. Documented
so we don't lose the thread.

**Line-item title deliberately excluded.** The product catalog is small (~5
SKUs) and loose substring matching makes it low-entropy — an attacker guessing
"was it the magnesium?" wins too often. Not a standalone verifier. It could be
reintroduced later as a secondary factor after a primary passes, but that's
not the current model. Kept as an intentional non-decision, documented so it
doesn't accidentally come back as a "why not add this too?" contribution.

**When we would raise the bar.** If real usage shows attackers social-engineering
order numbers off email screenshots or unboxing videos, we tighten Tier 2 to
require `card_last_four` or an address element, and drop `order_name` as a
challenge. Documented so we don't need to rediscover it under incident.

---

*(Additional entries land here as decisions are made. Stack rationale, prompt-
for-voice adaptation, latency math, retro — all Day 10 material.)*
