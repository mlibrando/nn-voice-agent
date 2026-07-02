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

*(Additional entries land here as decisions are made. Stack rationale, prompt-
for-voice adaptation, latency math, auth design, retro — all Day 10 material.)*
