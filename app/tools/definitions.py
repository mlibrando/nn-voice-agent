"""Realtime function-calling tool schemas — P0 tools only (Day 3 part 2).

Every schema mirrors INTERFACE.md exactly. Naming traps are baked into
descriptions AND enum/range constraints so the model cannot emit a value the
mock will 400 on:

- `cancel_subscription.reason` enum — 8 values, none of them medical.
  The AIA showcase (conv 09) logs `medical_issue` for an adverse-reaction
  cancel, which is NOT a valid reason and would 400. Adverse-reaction cancels
  MUST map to `"other"`. Encoded here in the enum + description; the prompt
  (Day 5) will teach Ashley to explain "cancelled due to your reaction" while
  the tool call uses `"other"`. See PLAN.md Risk #16 / SAFE-1.

- `partial_order_refund.refund_percentage` enum — only [10,20,25,30,35,40,50,60]
  are valid. 100% must use `full_order_refund`.

- Order mutations take `order_id` in the BODY (full Shopify GID), not the path.
  Handlers validate the GID prefix so a stray `40001` int can't slip through.

- `apply_subscription_discount` uses `discount_pct` (body) — do NOT confuse
  with the response field `discount_percentage`. See TOOL-8 / Risk #2.

The schemas below are just data. Handlers in `handlers.py` do the calling,
input validation, and result shaping.
"""

# ---------------------------------------------------------------------------
# Enum constants — exported so handlers can defensively re-validate.
# ---------------------------------------------------------------------------

# Exact enum from INTERFACE.md §2.6. `medical_issue` is deliberately absent.
CANCEL_REASONS: list[str] = [
    "too_much_product",
    "cant_afford_the_product",
    "didnt_want_a_subscription",
    "didnt_like_the_product",
    "found_a_better_alternative",
    "going_on_a_trip",
    "dont_need_the_product_anymore",
    "other",
]

# Allowed refund percentages for /orders/refund. Anything else 400s.
REFUND_PERCENTAGES: list[int] = [10, 20, 25, 30, 35, 40, 50, 60]


# ---------------------------------------------------------------------------
# Tool schemas for `session.update.tools`.
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict] = [
    # ------------------------------------------------------------- TOOL-1 --
    {
        "type": "function",
        "name": "customer_lookup",
        "description": (
            "Look up the customer's account by EXACTLY ONE of: phone, email, or "
            "order_number. Zero or multiple identifiers → 400. Returns "
            "{customer, orders, subscriptions} in one round-trip — prefer this "
            "over calling get_customer_subscriptions separately (the latter is slow). "
            "PRE-VERIFICATION SANITIZATION: when session.verified is False, the "
            "response is minimized to {ok, located, verification_required, "
            "customer_first_name, _note}. The full record lives in orchestration "
            "state and is used to check verify_identity answers — you (Ashley) "
            "must NOT read back address, email, order details, or subscription "
            "details before verification. Doing so hands the caller the answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "phone": {
                    "type": "string",
                    "description": "E.164 phone number, e.g. '+15125550101'.",
                },
                "email": {
                    "type": "string",
                    "description": "Case-insensitive email address on file.",
                },
                "order_number": {
                    "type": "string",
                    "description": "Public order name like '#1001'.",
                },
            },
            "additionalProperties": False,
        },
    },
    # -------------------------------------------------- TOOL-AUTH (Day 4) --
    {
        "type": "function",
        "name": "verify_identity",
        "description": (
            "Check a caller-provided answer against the located account. Call "
            "this AFTER customer_lookup located an account AND AFTER you asked "
            "the caller ONE challenge question. Never guess an answer.\n\n"
            "Challenge kinds:\n"
            "  - caller_id_confirm — only valid when the caller's phone matched a "
            "    customer via caller-ID (Tier-0). Loose affirmative match: 'yes', "
            "    'that's me', or their first name all count. If Tier-0 did not "
            "    hit, this is REFUSED (you get code=caller_id_didnt_match).\n"
            "  - zip — any shipping ZIP on the located record (sub OR order). "
            "    Digits-only compare, any single match passes.\n"
            "  - email — case-insensitive Customer.email match.\n"
            "  - order_name — the most recent order's name. Digits-only "
            "    compare: '#NN1001' / 'NN1001' / '1001' all match.\n"
            "  - card_last_four — last-4 from a SALE transaction only (refund "
            "    transactions have null card fields — do not use them).\n\n"
            "SAME-FACTOR RULE (enforced by the handler, not just guidance):\n"
            "  - Located via email → `email` challenge is REFUSED with code=same_factor.\n"
            "  - Located via order_number → `order_name` challenge is REFUSED.\n"
            "  - Located via phone (Tier-0 caller-ID match) → `caller_id_confirm`\n"
            "    is ALLOWED as a carve-out: the caller-ID match is an ambient\n"
            "    physical signal from Twilio, not a caller-supplied claim, so\n"
            "    combining it with a verbal confirm is two factors.\n"
            "The sanitized customer_lookup response tells you which challenges\n"
            "are blocked for the current locate path — pick one that isn't blocked.\n\n"
            "Failed attempts cap at 3. On code=locked_out the result carries a "
            "spoken_line (caller-facing verbiage) and an escalation_suggestion "
            "(prefilled create_escalation args); read the spoken_line to the "
            "caller and pass escalation_suggestion into create_escalation. Do "
            "not attempt verification again after locked_out."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "challenge_kind": {
                    "type": "string",
                    "enum": ["caller_id_confirm", "zip", "email", "order_name", "card_last_four"],
                    "description": "Which challenge you asked the caller.",
                },
                "given_value": {
                    "type": "string",
                    "description": "The caller's verbatim spoken answer. Normalization happens server-side.",
                },
            },
            "required": ["challenge_kind", "given_value"],
            "additionalProperties": False,
        },
    },
    # ------------------------------------------------------------- TOOL-2 --
    {
        "type": "function",
        "name": "get_customer_orders",
        "description": (
            "Fetch the customer's orders (newest first). Use `customer_id` from "
            "customer_lookup — never guess."
        ),
        "parameters": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
            "additionalProperties": False,
        },
    },
    # ------------------------------------------------------------- TOOL-3 --
    {
        "type": "function",
        "name": "get_customer_subscriptions",
        "description": (
            "Fetch the customer's subscriptions. SLOW ENDPOINT (~+1200ms on top "
            "of ambient latency). Prefer the `subscriptions` array from "
            "customer_lookup; only call this after a mutation when you need a "
            "fresh read."
        ),
        "parameters": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
            "additionalProperties": False,
        },
    },
    # ------------------------------------------------------------- TOOL-5 --
    {
        "type": "function",
        "name": "cancel_subscription",
        "description": (
            "Cancel a subscription. `reason` MUST be one of the 8 enum values — "
            "there is NO health/medical value. For adverse reactions or health "
            "issues, use 'other'. The prompt tells Ashley how to explain that "
            "to the caller; the tool call MUST use 'other', not 'medical_issue'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subscription_id": {"type": "integer"},
                "reason": {"type": "string", "enum": CANCEL_REASONS},
            },
            "required": ["subscription_id", "reason"],
            "additionalProperties": False,
        },
    },
    # ------------------------------------------------------------- TOOL-6 --
    {
        "type": "function",
        "name": "pause_subscription",
        "description": "Pause a subscription for 1–6 months. Returns the full updated Subscription.",
        "parameters": {
            "type": "object",
            "properties": {
                "subscription_id": {"type": "integer"},
                "pause_months": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 6,
                },
            },
            "required": ["subscription_id", "pause_months"],
            "additionalProperties": False,
        },
    },
    # ------------------------------------------------------------- TOOL-8 --
    {
        "type": "function",
        "name": "apply_subscription_discount",
        "description": (
            "Apply a percentage discount to a subscription. DESTRUCTIVE and "
            "NON-IDEMPOTENT — a second call compounds the discount. Confirm "
            "with the caller before invoking. The handler enforces a hard "
            "once-per-subscription-per-call guard; a re-pitch after decline "
            "will be REJECTED — do not attempt it. Field is `discount_pct` "
            "(request), NOT `discount_percentage` (that's the response field)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subscription_id": {"type": "integer"},
                "discount_pct": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 99,
                    "description": "Percentage 1-99 (mock validates 0 < pct < 100).",
                },
                "code": {
                    "type": "string",
                    "description": "Promo code label, e.g. 'LOYAL20'.",
                },
            },
            "required": ["subscription_id", "discount_pct", "code"],
            "additionalProperties": False,
        },
    },
    # ------------------------------------------------------------- TOOL-10 -
    {
        "type": "function",
        "name": "partial_order_refund",
        "description": (
            "Refund an order at one of the allowed percentages: 10, 20, 25, 30, "
            "35, 40, 50, 60. Any other value fails. For 100%, use "
            "full_order_refund. `order_id` is the full Shopify GID like "
            "'gid://shopify/Order/40001' and goes in the body, not the URL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Full Shopify GID (e.g. 'gid://shopify/Order/40001').",
                },
                "refund_percentage": {
                    "type": "integer",
                    "enum": REFUND_PERCENTAGES,
                },
            },
            "required": ["order_id", "refund_percentage"],
            "additionalProperties": False,
        },
    },
    # ------------------------------------------------------------- TOOL-11 -
    {
        "type": "function",
        "name": "full_order_refund",
        "description": (
            "Refund an order in full (100%). SIDE EFFECT: if the order was "
            "UNFULFILLED, this also cancels it. When the returned result "
            "contains `also_cancelled: true`, surface BOTH effects to the "
            "caller: refund issued AND order cancelled — never say only one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Full Shopify GID (e.g. 'gid://shopify/Order/40001').",
                },
            },
            "required": ["order_id"],
            "additionalProperties": False,
        },
    },
    # ------------------------------------------------------------- TOOL-14 -
    {
        "type": "function",
        "name": "create_escalation",
        "description": (
            "Queue a human follow-up. Use for: adverse reactions (with "
            "mark_high_risk=true), complex billing disputes, angry callers you "
            "can't de-escalate, anything out of the tool set. `issue_for_human` "
            "is required — one paragraph, plain language."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "issue_for_human": {
                    "type": "string",
                    "description": "One-paragraph handoff summary for the human agent.",
                },
                "customer_details": {"type": "string"},
                "actions_taken": {
                    "type": "string",
                    "description": "What Ashley did in this call before escalating.",
                },
                "mark_high_risk": {"type": "boolean"},
            },
            "required": ["issue_for_human"],
            "additionalProperties": False,
        },
    },
    # ------------------------------------------------------------- TOOL-15 -
    {
        "type": "function",
        "name": "save_transcript",
        "description": (
            "Persist the call transcript at end of call. The handler fills "
            "call_id + the raw transcript from bridge-side session state — you "
            "don't and shouldn't provide those. Give a short `summary` (1–2 "
            "sentences) and a one-word `outcome` like 'cancelled', 'refunded', "
            "'paused', 'escalated', 'informational'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "outcome": {"type": "string"},
            },
            "required": ["summary", "outcome"],
            "additionalProperties": False,
        },
    },
]
