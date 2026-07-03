"""Tool handlers — one async function per P0 tool.

Contract every handler follows:
  - takes (args: dict, session: dict)
  - returns a JSON-serializable dict shaped as either:
        {"ok": True,  ...mock data...}
     or {"ok": False, "error": {"code": "...", "message": "..."}}
  - NEVER fabricates data. Any mock failure — 404, 409, exhausted 503, network —
    comes back as `ok: false` with a structured error the prompt tells Ashley
    to admit rather than paper over.
  - Handlers pre-validate inputs where a clean local rejection beats a mock
    round-trip (e.g. `customer_lookup` "exactly one identifier"). Enum/range
    constraints live in `definitions.py`; handlers re-check the ones that
    matter for correctness (refund percentage, discount range).

Destructive/non-idempotent tools have extra guards (see
`apply_subscription_discount`).
"""
import logging
import re

from app.tools.client import ToolError, call_mock
from app.tools.definitions import CANCEL_REASONS, REFUND_PERCENTAGES

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def _from_tool_error(e: ToolError) -> dict:
    return _error(e.code, e.message)


def _cache_subs(session: dict, subs: list) -> None:
    """Update session.subscriptions_by_id from a list of Subscription dicts.

    Called by any handler that reads a Subscription. Lets downstream mutation
    handlers (esp. apply_subscription_discount) inspect current state without
    a fresh +1200ms /subscriptions round-trip.
    """
    cache = session.setdefault("subscriptions_by_id", {})
    for sub in subs or []:
        sid = sub.get("subscription_id")
        if sid is not None:
            cache[sid] = sub


def _cache_sub(session: dict, sub: dict) -> None:
    """Update the cache with a single Subscription (post-mutation refresh)."""
    if not isinstance(sub, dict):
        return
    sid = sub.get("subscription_id")
    if sid is not None:
        session.setdefault("subscriptions_by_id", {})[sid] = sub


# --------------------------------------------------------------------------- #
# Auth normalization helpers
# --------------------------------------------------------------------------- #
_MAX_AUTH_ATTEMPTS = 3

_AFFIRMATIVE_WORDS = {
    "yes", "yeah", "yep", "yup", "sure", "correct", "right", "confirm",
    "confirmed", "affirmative", "that's me", "thats me", "speaking",
    "this is",
}


def _digits(s: str | None) -> str:
    """Strip all non-digit characters — for phone/zip/order-number compares."""
    if not s:
        return ""
    return re.sub(r"\D", "", str(s))


def _norm(s: str | None) -> str:
    """Trim + lowercase — for email and loose affirmative match."""
    if not s:
        return ""
    return str(s).strip().lower()


def _collect_on_file_zips(candidate: dict) -> list[str]:
    """Every non-empty shipping_address.zip on the located record — from
    subscriptions AND orders. Digits-only, deduplicated. Used by the `zip`
    challenge; any single match against any on-file ZIP passes."""
    zips: list[str] = []
    for sub in candidate.get("subscriptions") or []:
        z = (sub.get("shipping_address") or {}).get("zip")
        if z:
            zips.append(_digits(z))
    for order in candidate.get("orders") or []:
        z = (order.get("shipping_address") or {}).get("zip")
        if z:
            zips.append(_digits(z))
    return list({z for z in zips if z})


def _sale_card_last_four(candidate: dict) -> list[str]:
    """Every SALE-txn card_last_four across all orders. REFUND txns explicitly
    excluded — their card fields are null (IFACE gotcha #7)."""
    lasts: list[str] = []
    for order in candidate.get("orders") or []:
        for txn in order.get("transactions") or []:
            if txn.get("kind") == "SALE":
                clf = txn.get("card_last_four")
                if clf:
                    lasts.append(_digits(clf))
    return list({x for x in lasts if x})


def _most_recent_order_name(candidate: dict) -> str | None:
    """Most-recent order's order_name (orders returned newest-first per IFACE)."""
    orders = candidate.get("orders") or []
    if not orders:
        return None
    return orders[0].get("order_name")


def _is_affirmative(given: str, first_name: str) -> bool:
    """Loose match for `caller_id_confirm`: 'yes', 'that's me', or their first
    name mentioned anywhere in the answer counts."""
    g = _norm(given)
    if not g:
        return False
    if first_name and _norm(first_name) in g:
        return True
    for word in _AFFIRMATIVE_WORDS:
        if word in g:
            return True
    return False


# --------------------------------------------------------------------------- #
# TOOL-1 — customer_lookup
# --------------------------------------------------------------------------- #
async def customer_lookup(args: dict, session: dict) -> dict:
    # Enforce "exactly one" locally so the model gets a clear message rather
    # than triggering a mock 400 that costs a retry-inducing round-trip.
    provided = {
        k: v for k, v in args.items()
        if k in ("phone", "email", "order_number") and v
    }
    if len(provided) != 1:
        return _error(
            "bad_request",
            "Provide EXACTLY ONE of phone, email, or order_number.",
        )
    try:
        data = await call_mock("GET", "/customers/lookup", params=provided)
    except ToolError as e:
        return _from_tool_error(e)

    # Cache the bundled subscriptions so downstream mutations can inspect
    # persisted discount state without hitting the slow /subscriptions endpoint.
    _cache_subs(session, data.get("subscriptions", []))

    # --- Day 4 auth wiring ---
    # Stash the full record as the candidate account for verify_identity to
    # check against. A new lookup is a fresh auth attempt window: reset the
    # attempt counter and clear any stale Tier-0/located_via flags.
    session["candidate_account"] = data
    session["auth_attempts"] = 0
    session["tier0_hit"] = False
    session["located_via"] = None

    # Record which identifier the caller used to locate. `verify_identity`
    # reads this to refuse same-factor challenges — a caller who located
    # by email cannot then verify by confirming that same email (that would
    # be zero-factor authentication). See DECISIONS.md draft "Located vs.
    # verified" (updated).
    if provided.get("phone"):
        session["located_via"] = "phone"
    elif provided.get("email"):
        session["located_via"] = "email"
    elif provided.get("order_number"):
        session["located_via"] = "order_number"

    # Tier-0 detection: if we looked up by phone AND that phone matches the
    # caller's inbound `from_number`, this is a caller-ID hit. Only then is
    # the `caller_id_confirm` challenge kind valid.
    #
    # Tier-0 carve-out on same-factor: caller-ID is an *ambient physical
    # signal* delivered by Twilio's carrier layer — not a caller-supplied
    # claim. So confirming identity by voice after a caller-ID match is TWO
    # factors (physical + verbal), not one. That's why the same-factor rule
    # exempts the phone/caller_id_confirm combination on Tier-0. See
    # DECISIONS.md draft's Tier-0 carve-out note.
    from_number = session.get("from_number")
    looked_up_phone = provided.get("phone")
    if (
        looked_up_phone
        and from_number
        and _digits(looked_up_phone) == _digits(from_number)
    ):
        session["tier0_hit"] = True

    if session.get("verified"):
        # Post-verify re-lookup — return the full record (caller has already proven identity).
        return {"ok": True, **data}

    # Pre-verify sanitization. Only the first name goes to the model — enough
    # for "am I speaking with X?" personalization; not enough to leak a
    # challenge answer. Full record stays in session.candidate_account for
    # verify_identity to check against.
    first_name = (data.get("customer") or {}).get("first_name") or ""

    # Build the pre-verify guidance, naming which challenge is blocked because
    # of same-factor. The state check in verify_identity is the enforcement;
    # this note reinforces so the model doesn't waste a tool call attempting
    # an already-blocked challenge.
    if session["tier0_hit"]:
        guidance = (
            "Caller-ID matched — you can confirm via "
            "verify_identity(challenge_kind='caller_id_confirm', "
            "given_value=<what they said>). A 'yes' or their first name is enough."
        )
    else:
        blocked_challenges = []
        valid_challenges = ["zip", "email", "order_name", "card_last_four"]
        if session["located_via"] == "email":
            blocked_challenges.append("email")
            valid_challenges.remove("email")
        elif session["located_via"] == "order_number":
            blocked_challenges.append("order_name")
            valid_challenges.remove("order_name")
        guidance = (
            "Caller-ID did not match. Pose ONE independent challenge — "
            + ", ".join(valid_challenges)
            + " — and call verify_identity with the answer."
        )
        if blocked_challenges:
            guidance += (
                " Do NOT use "
                + ", ".join(blocked_challenges)
                + f" as a challenge — the caller located via {session['located_via']}; "
                "same-factor challenges are refused at the handler."
            )

    return {
        "ok": True,
        "located": True,
        "verification_required": True,
        "customer_first_name": first_name,
        "tier0_hit": session["tier0_hit"],
        "located_via": session["located_via"],
        "_note": (
            "Account located but not verified. Do NOT read back address, "
            "email, order details, or subscription details. "
            + guidance
            + " Failed attempts cap at 3; on locked_out the tool result will "
            "hand you the escalation body verbatim."
        ),
    }


# --------------------------------------------------------------------------- #
# TOOL-AUTH — verify_identity   (Day 4)
# --------------------------------------------------------------------------- #
async def verify_identity(args: dict, session: dict) -> dict:
    """Check a caller-provided answer against the located candidate account.

    See DECISIONS.md draft "Located vs. verified" for the security model.
    Contract: never leak the correct answer; cap failed attempts at 3; on
    locked_out, return a self-contained escalation payload the model relays.
    """
    kind = args.get("challenge_kind")
    given = args.get("given_value") or ""

    candidate = session.get("candidate_account")
    if not candidate:
        return _error(
            "no_candidate",
            "No candidate account located. Call customer_lookup first.",
        )

    # Idempotent success — Ashley can safely re-call after a successful verify.
    if session.get("verified"):
        return {"ok": True, "verified": True, "already_verified": True}

    # Lockout gate. The result is fully self-contained: caller-facing spoken_line,
    # next_action, and prefilled escalation_suggestion so the fallback survives
    # even a stripped or drifted prompt.
    if session.get("auth_attempts", 0) >= _MAX_AUTH_ATTEMPTS:
        return _locked_out_result(session)

    if kind not in ("caller_id_confirm", "zip", "email", "order_name", "card_last_four"):
        return _error(
            "bad_request",
            "challenge_kind must be one of: caller_id_confirm, zip, email, "
            "order_name, card_last_four.",
        )

    # SAME-FACTOR GATE (Day 4 hardening — Risk #1, verification-as-state).
    #
    # A caller who located by email cannot verify by confirming that same
    # email — that would be zero-factor authentication (one identifier used
    # for both locate and verify). Same for order_number → order_name.
    #
    # Tier-0 carve-out: caller-ID matching (session["tier0_hit"]) is an
    # *ambient physical signal* from Twilio's carrier layer, not a
    # caller-supplied claim. `caller_id_confirm` after a Tier-0 hit is
    # therefore TWO factors (physical + verbal), not one. Exempt.
    #
    # NOT counted as a failed attempt: the caller didn't give a wrong answer;
    # the model picked the wrong challenge kind. Incrementing the attempt
    # counter here could lock out a legitimate caller for a model choice
    # they had no influence over. The error tells the model to pick a
    # different kind — it should.
    located_via = session.get("located_via")
    if kind == "email" and located_via == "email":
        return _error(
            "same_factor",
            "Email cannot verify identity — the caller located the account "
            "using email. Pose a DIFFERENT Tier-2 challenge: zip, order_name, "
            "or card_last_four (SALE only). Same-factor challenges are "
            "refused because using the same fact for locate and verify is "
            "zero-factor authentication.",
        )
    if kind == "order_name" and located_via == "order_number":
        return _error(
            "same_factor",
            "Order name cannot verify identity — the caller located the "
            "account using the order number. Pose a DIFFERENT Tier-2 "
            "challenge: zip, email, or card_last_four (SALE only). "
            "Same-factor challenges are refused because using the same fact "
            "for locate and verify is zero-factor authentication.",
        )

    customer = candidate.get("customer") or {}

    # `caller_id_confirm` is Tier-0-only. If from_number didn't match a
    # customer, refuse — a Tier-1-located account cannot be verified by "yes".
    if kind == "caller_id_confirm":
        if not session.get("tier0_hit"):
            return _error(
                "caller_id_didnt_match",
                "Caller-ID did not match this account. Use a Tier-2 challenge "
                "instead: zip, email, order_name, or card_last_four.",
            )
        if _is_affirmative(given, customer.get("first_name", "")):
            return _promote_to_verified(session)
        # Not affirmative — count as a failed attempt.
        return _record_failed_attempt(session)

    # Tier-2 challenges — extract expected value(s), normalized-compare.
    matched = False

    if kind == "zip":
        on_file = _collect_on_file_zips(candidate)
        if not on_file:
            return _error(
                "kind_unavailable",
                "No ZIP on file for this account. Ask a different challenge "
                "(email, order_name, or card_last_four).",
            )
        matched = _digits(given) in on_file

    elif kind == "email":
        expected = customer.get("email")
        if not expected:
            return _error("kind_unavailable", "No email on file. Ask a different challenge.")
        matched = _norm(given) == _norm(expected)

    elif kind == "order_name":
        expected = _most_recent_order_name(candidate)
        if not expected:
            return _error("kind_unavailable", "No orders on file. Ask a different challenge.")
        matched = _digits(given) == _digits(expected)

    elif kind == "card_last_four":
        on_file = _sale_card_last_four(candidate)
        if not on_file:
            return _error(
                "kind_unavailable",
                "No SALE-transaction card on file (refund transactions have "
                "null card fields). Ask a different challenge.",
            )
        # Take the last 4 digits of the given value defensively — caller may
        # say "the ones ending in 4242" or just "4242".
        given_digits = _digits(given)[-4:]
        matched = given_digits in on_file

    if matched:
        return _promote_to_verified(session)
    return _record_failed_attempt(session)


def _promote_to_verified(session: dict) -> dict:
    """Flip candidate → verified. Called only on a confirmed match."""
    candidate = session.get("candidate_account") or {}
    session["verified"] = True
    session["customer"] = candidate.get("customer")
    session["candidate_account"] = None
    session["auth_attempts"] = 0
    log.info(
        f"Auth: verified customer_id="
        f"{(session.get('customer') or {}).get('customer_id')}"
    )
    return {"ok": True, "verified": True}


def _record_failed_attempt(session: dict) -> dict:
    """Increment attempt counter; return locked_out payload on cap."""
    session["auth_attempts"] = session.get("auth_attempts", 0) + 1
    if session["auth_attempts"] >= _MAX_AUTH_ATTEMPTS:
        return _locked_out_result(session)
    remaining = _MAX_AUTH_ATTEMPTS - session["auth_attempts"]
    return {
        "ok": False,
        "error": {
            "code": "verification_failed",
            "message": (
                f"That doesn't match what we have on file. "
                f"{remaining} attempt(s) remaining before I can't verify you "
                "on this call."
            ),
        },
        "attempts_remaining": remaining,
    }


def _locked_out_result(session: dict) -> dict:
    """Self-contained graceful-degradation payload for the model to relay
    without prompt help. Three signals: model directive, caller-facing
    spoken_line, and prefilled create_escalation args."""
    candidate = session.get("candidate_account") or {}
    cust = candidate.get("customer") or {}
    call_sid = session.get("call_sid") or "unknown"
    from_number = session.get("from_number") or "not provided"
    first = cust.get("first_name") or ""
    last = cust.get("last_name") or ""
    cid = cust.get("customer_id") or "unknown"
    name = (f"{first} {last}").strip() or "unknown"

    return {
        "ok": False,
        "error": {
            "code": "locked_out",
            "message": (
                "Verification attempts exhausted (3 of 3). Do not attempt "
                "verification again. Speak the `spoken_line` to the caller, "
                "then immediately call create_escalation using "
                "`escalation_suggestion` as the args."
            ),
        },
        "spoken_line": (
            "I wasn't able to verify your identity on this call — I want to "
            "make sure we protect your account. I can create a note for one "
            "of our team members to follow up with you. Would that be okay?"
        ),
        "next_action": "create_escalation",
        "escalation_suggestion": {
            "issue_for_human": (
                f"Caller unable to verify identity after 3 attempts on call "
                f"{call_sid}. Located candidate: {name} (customer_id={cid}). "
                f"Caller ID: {from_number}. Please call back and try "
                "alternate verification (recent transaction detail, "
                "security question)."
            ),
            "actions_taken": (
                "Located account; posed identity challenges; caller could "
                "not verify."
            ),
            "mark_high_risk": False,
        },
    }


# --------------------------------------------------------------------------- #
# TOOL-2 — get_customer_orders
# --------------------------------------------------------------------------- #
async def get_customer_orders(args: dict, session: dict) -> dict:
    cid = args.get("customer_id")
    if not cid:
        return _error("bad_request", "customer_id is required.")
    try:
        data = await call_mock("GET", f"/customers/{cid}/orders")
    except ToolError as e:
        return _from_tool_error(e)
    return {"ok": True, **data}


# --------------------------------------------------------------------------- #
# TOOL-3 — get_customer_subscriptions (slow endpoint)
# --------------------------------------------------------------------------- #
async def get_customer_subscriptions(args: dict, session: dict) -> dict:
    cid = args.get("customer_id")
    if not cid:
        return _error("bad_request", "customer_id is required.")
    try:
        data = await call_mock("GET", f"/customers/{cid}/subscriptions")
    except ToolError as e:
        return _from_tool_error(e)
    # Refresh the cache with the latest server state.
    _cache_subs(session, data.get("subscriptions", []))
    return {"ok": True, **data}


# --------------------------------------------------------------------------- #
# TOOL-5 — cancel_subscription
# --------------------------------------------------------------------------- #
async def cancel_subscription(args: dict, session: dict) -> dict:
    sid = args.get("subscription_id")
    reason = args.get("reason", "other")

    if not sid:
        return _error("bad_request", "subscription_id is required.")

    # Defense-in-depth: even though the schema enums the value, catch a
    # `medical_issue` slip locally so we return a helpful message rather
    # than a mock 400. AIA conv 09 uses this value in the reference agent —
    # by voice, adverse-reaction cancels must map to "other". (Risk #16)
    if reason not in CANCEL_REASONS:
        return _error(
            "bad_reason",
            f"'{reason}' is not a valid cancel reason. Use one of "
            f"{CANCEL_REASONS}. For adverse reactions use 'other'.",
        )

    try:
        data = await call_mock(
            "POST", f"/subscriptions/{sid}/cancel",
            json_body={"reason": reason},
        )
    except ToolError as e:
        return _from_tool_error(e)
    _cache_sub(session, data)
    return {"ok": True, "subscription": data}


# --------------------------------------------------------------------------- #
# TOOL-6 — pause_subscription
# --------------------------------------------------------------------------- #
async def pause_subscription(args: dict, session: dict) -> dict:
    sid = args.get("subscription_id")
    months = args.get("pause_months", 1)

    if not sid:
        return _error("bad_request", "subscription_id is required.")
    if not isinstance(months, int) or not (1 <= months <= 6):
        return _error("bad_request", "pause_months must be an integer between 1 and 6.")

    try:
        data = await call_mock(
            "POST", f"/subscriptions/{sid}/pause",
            json_body={"pause_months": months},
        )
    except ToolError as e:
        return _from_tool_error(e)
    _cache_sub(session, data)
    return {"ok": True, "subscription": data}


# --------------------------------------------------------------------------- #
# TOOL-8 — apply_subscription_discount   (DESTRUCTIVE / NON-IDEMPOTENT)
# --------------------------------------------------------------------------- #
async def apply_subscription_discount(args: dict, session: dict) -> dict:
    """Apply a discount. Two guards against destructive compounding.

    The mock recomputes `total_value *= (1 - pct/100)` on every call — two
    calls at 20% land at 36% off, not 20%. Silent overcharge/undercharge, no
    call-time audit trail. So this is a correctness gate, not a nicety.

    Guard 1 — CROSS-CALL stacking (persisted state read):
      Read the subscription's current `discount_percentage` from
      `session.subscriptions_by_id` (populated by customer_lookup /
      get_customer_subscriptions). If it's already set, the caller got a
      discount on a previous call — DO NOT stack. Return
      `code=discount_already_active` with the existing pct so Ashley can say
      "you've got a 20% discount on this — it's still in effect" rather than
      silently applying another.

    Guard 2 — WITHIN-CALL re-pitch (session set):
      Once a discount succeeds in this call, further calls for the same sub
      are rejected at the handler with `code=already_applied`. Blocks: retry
      loops after a transient error, and the "retention" instruction tempting
      the model to re-offer after a caller "no".

    Prefer the bundled-lookup cache; do not add a slow /subscriptions round-
    trip just to guard this. If the cache is empty (no lookup ran), the
    within-call guard is the only line of defense — Day 4 auth normally
    ensures lookup has run before any mutation is reached.

    Policy documented in DECISIONS.md (draft).
    """
    sid = args.get("subscription_id")
    pct = args.get("discount_pct")
    code = args.get("code", "LOYAL20")

    if not sid:
        return _error("bad_request", "subscription_id is required.")
    if not isinstance(pct, int) or not (0 < pct < 100):
        return _error("bad_request", "discount_pct must be an integer between 1 and 99.")

    # --- Guard 1: CROSS-CALL stacking. Read persisted state via session cache.
    cached = session.get("subscriptions_by_id", {}).get(sid)
    if cached and cached.get("discount_percentage"):
        existing_pct = cached["discount_percentage"]
        return {
            "ok": False,
            "existing_discount_percentage": existing_pct,
            "error": {
                "code": "discount_already_active",
                "message": (
                    f"Subscription {sid} already has an active "
                    f"{existing_pct}% discount from a previous call — do NOT "
                    "stack a second discount (tool is non-idempotent and would "
                    "compound). Tell the caller their existing discount is "
                    "still in effect."
                ),
            },
        }

    # --- Guard 2: WITHIN-CALL re-pitch. Session-level set.
    applied: set = session.setdefault("applied_discounts", set())
    if sid in applied:
        return _error(
            "already_applied",
            f"A discount was already applied to subscription {sid} in this call. "
            "This tool is once-only per intent to prevent compounding — do not "
            "call again for this subscription. Move on.",
        )

    try:
        data = await call_mock(
            "POST", f"/subscriptions/{sid}/discount",
            json_body={"discount_pct": pct, "code": code},
        )
    except ToolError as e:
        # If the mock rejects (e.g. 409 already CANCELLED), don't mark applied.
        return _from_tool_error(e)

    applied.add(sid)
    _cache_sub(session, data)  # Cache now shows discount_percentage set.
    return {"ok": True, "subscription": data}


# --------------------------------------------------------------------------- #
# TOOL-10 — partial_order_refund
# --------------------------------------------------------------------------- #
async def partial_order_refund(args: dict, session: dict) -> dict:
    order_id = args.get("order_id")
    pct = args.get("refund_percentage")

    if not isinstance(order_id, str) or not order_id.startswith("gid://shopify/Order/"):
        return _error(
            "bad_request",
            "order_id must be a full Shopify GID like 'gid://shopify/Order/40001'.",
        )
    if pct not in REFUND_PERCENTAGES:
        return _error(
            "bad_request",
            f"refund_percentage must be one of {REFUND_PERCENTAGES}. "
            "For 100% use full_order_refund.",
        )

    try:
        data = await call_mock(
            "POST", "/orders/refund",
            json_body={"order_id": order_id, "refund_percentage": pct},
        )
    except ToolError as e:
        return _from_tool_error(e)
    return {"ok": True, "order": data}


# --------------------------------------------------------------------------- #
# TOOL-11 — full_order_refund   (side effect: cancels unfulfilled orders)
# --------------------------------------------------------------------------- #
async def full_order_refund(args: dict, session: dict) -> dict:
    order_id = args.get("order_id")
    if not isinstance(order_id, str) or not order_id.startswith("gid://shopify/Order/"):
        return _error(
            "bad_request",
            "order_id must be a full Shopify GID like 'gid://shopify/Order/40001'.",
        )

    try:
        data = await call_mock(
            "POST", "/orders/refund/full",
            json_body={"order_id": order_id},
        )
    except ToolError as e:
        return _from_tool_error(e)

    result: dict = {"ok": True, "order": data}
    # Explicit "both effects" flag so the model doesn't miss the silent cancel
    # of an unfulfilled order (Risk #3). Prompt teaches Ashley to say both.
    if data.get("cancelled_at"):
        result["also_cancelled"] = True
        result["_note"] = (
            "This order was also cancelled because it was unfulfilled at refund "
            "time. Tell the caller BOTH: refund issued AND order cancelled."
        )
    return result


# --------------------------------------------------------------------------- #
# TOOL-14 — create_escalation
# --------------------------------------------------------------------------- #
async def create_escalation(args: dict, session: dict) -> dict:
    if not args.get("issue_for_human"):
        return _error("bad_request", "issue_for_human is required.")

    body: dict = {"issue_for_human": args["issue_for_human"]}
    for optional in ("customer_details", "actions_taken", "mark_high_risk"):
        if args.get(optional) is not None:
            body[optional] = args[optional]

    customer = session.get("customer") or {}
    if customer.get("customer_id"):
        body.setdefault("customer_id", customer["customer_id"])
    else:
        # Unauthenticated-caller fallback path. Human ops team gets both the
        # call_sid (Twilio recording + our transcript correlation) AND the
        # Twilio `From` number (Day 4 hook consumed) so they can call back
        # even without a verified customer_id.
        supplied = body.get("customer_details", "") or ""
        call_sid = session.get("call_sid") or "unknown"
        from_number = session.get("from_number") or "unknown"
        prefix = f"[unverified caller — call_sid={call_sid} from={from_number}]"
        body["customer_details"] = (
            f"{prefix} {supplied}".strip()
            if supplied
            else f"{prefix} (no additional details captured)"
        )

    try:
        data = await call_mock("POST", "/escalations", json_body=body)
    except ToolError as e:
        return _from_tool_error(e)
    return {"ok": True, **data}


# --------------------------------------------------------------------------- #
# TOOL-15 — save_transcript
# --------------------------------------------------------------------------- #
async def save_transcript(args: dict, session: dict) -> dict:
    """Persist the transcript from session state (not model-generated).

    The mock requires `call_id` + `transcript`; the model only supplies short
    `summary` + `outcome`. We build the interleaved transcript from
    `session.transcript_in / transcript_out` so what's persisted is what was
    actually said, not what the model summarizes.
    """
    lines: list[str] = []
    for line in session.get("transcript_in", []):
        lines.append(f"Caller: {line}")
    for line in session.get("transcript_out", []):
        lines.append(f"Ashley: {line}")
    transcript_text = "\n".join(lines) if lines else "(no transcript captured)"

    call_id = session.get("call_sid") or session.get("stream_sid") or "unknown"

    body: dict = {
        "call_id": call_id,
        "transcript": transcript_text,
    }
    if args.get("summary"):
        body["summary"] = args["summary"]
    if args.get("outcome"):
        body["outcome"] = args["outcome"]
    customer = session.get("customer") or {}
    if customer.get("customer_id"):
        body["customer_id"] = customer["customer_id"]

    try:
        data = await call_mock("POST", "/transcripts", json_body=body)
    except ToolError as e:
        return _from_tool_error(e)
    return {"ok": True, **data}


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
_HANDLERS = {
    "customer_lookup":              customer_lookup,
    "verify_identity":              verify_identity,
    "get_customer_orders":          get_customer_orders,
    "get_customer_subscriptions":   get_customer_subscriptions,
    "cancel_subscription":          cancel_subscription,
    "pause_subscription":           pause_subscription,
    "apply_subscription_discount":  apply_subscription_discount,
    "partial_order_refund":         partial_order_refund,
    "full_order_refund":            full_order_refund,
    "create_escalation":            create_escalation,
    "save_transcript":              save_transcript,
}

# Tools that may run pre-verification.
#   customer_lookup — the way we locate; its response is sanitized when
#                     session.verified is False (see customer_lookup handler).
#   verify_identity — how identity gets proven.
#   create_escalation — has an unauth-caller fallback path with breadcrumbs.
#   save_transcript — end-of-call persistence, runs regardless of auth outcome.
# Everything else is post-verification only. See DECISIONS.md draft
# "Located vs. verified" for the security rationale.
_PRE_AUTH_TOOLS = {
    "customer_lookup",
    "verify_identity",
    "create_escalation",
    "save_transcript",
}


async def dispatch(name: str, args: dict, session: dict) -> dict:
    """Route a Realtime function call to its handler.

    Enforces the auth gate: mutations and account-data-disclosure reads
    refuse with `code=verification_required` when session.verified is False.
    Only tools in _PRE_AUTH_TOOLS may run pre-verification.

    Unknown tool names and handler exceptions never propagate — they become
    structured errors. If a raw exception leaked to the Realtime API, the model
    would either retry forever or silently give up mid-call; a structured
    error the model can talk around is always better.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        log.warning(f"Dispatch: unknown tool '{name}'")
        return _error("unknown_tool", f"No handler wired for tool '{name}'.")

    # AUTH GATE — the core security enforcement. Prompt cannot bypass this.
    if name not in _PRE_AUTH_TOOLS and not session.get("verified"):
        log.info(f"Auth gate: refused {name} — session not verified")
        return _error(
            "verification_required",
            "This action requires the caller's identity to be verified first. "
            "If you haven't located them yet, call customer_lookup. If you have, "
            "pose ONE challenge (ZIP, email, order name, or card last-4) and "
            "call verify_identity with the answer. Do not attempt this tool "
            "again until verify_identity returns ok=True.",
        )

    try:
        return await handler(args, session)
    except Exception as e:
        log.exception(f"Handler '{name}' raised")
        return _error("handler_exception", f"{e.__class__.__name__}: {e}")
