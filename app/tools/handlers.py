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
    return {"ok": True, **data}


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
        # Unauthenticated-caller fallback path.
        #
        # Until Day 4 auth populates session.customer, we don't have a
        # customer_id — but the human ops team still needs something to act
        # on. Prepend the call_sid to customer_details as a correlation key
        # (they can pull the Twilio recording + our transcript from it).
        # Post Day 4, once we start capturing the Twilio `From` number on the
        # session, add it here too — for now call_sid is the strongest
        # identifier we have for an unverified caller.
        supplied = body.get("customer_details", "") or ""
        call_sid = session.get("call_sid") or "unknown"
        prefix = f"[unverified caller — call_sid={call_sid}]"
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


async def dispatch(name: str, args: dict, session: dict) -> dict:
    """Route a Realtime function call to its handler.

    Unknown tool names and handler exceptions never propagate — they become
    structured errors. If a raw exception leaked to the Realtime API, the model
    would either retry forever or silently give up mid-call; a structured
    error the model can talk around is always better.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        log.warning(f"Dispatch: unknown tool '{name}'")
        return _error("unknown_tool", f"No handler wired for tool '{name}'.")
    try:
        return await handler(args, session)
    except Exception as e:
        log.exception(f"Handler '{name}' raised")
        return _error("handler_exception", f"{e.__class__.__name__}: {e}")
