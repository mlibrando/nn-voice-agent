"""Per-call session state factory.

Kept as a plain dict for now — `bridge.py` accesses fields via string keys, so
migrating to a dataclass later is a mechanical change. Day 4 auth will
populate `verified`, `customer`, `candidate_account`; they're declared here as
empty placeholders so the shape is stable before that work lands.
"""
from datetime import datetime, timezone


def new_session() -> dict:
    return {
        # Twilio identifiers
        "stream_sid": None,
        "call_sid": None,
        # Timestamps
        "started_at": datetime.now(timezone.utc).isoformat(),
        # Transcripts (in = caller, out = agent)
        "transcript_in": [],
        "transcript_out": [],
        # OpenAI response tracking — True while a response is being generated.
        # Gates barge-in's `response.cancel` + Twilio `clear` so we don't emit
        # `response_cancel_not_active` errors when nothing's in flight.
        "active_response": False,
        # Destructive-tool guard — subscription_ids where
        # `apply_subscription_discount` already ran. Blocks the re-pitch/retry
        # compounding case at the handler level. See Risk #2/#18 and
        # handlers.apply_subscription_discount.
        "applied_discounts": set(),
        # Subscription-state cache — populated by customer_lookup /
        # get_customer_subscriptions and updated after mutations that return
        # the full Subscription. Read by apply_subscription_discount to detect
        # a persisted (cross-call) discount without paying the +1200ms slow-
        # endpoint round-trip. Keyed by subscription_id.
        "subscriptions_by_id": {},
        # Auth state — populated by Day 4's state machine. Declared here so
        # the session shape is stable.
        "verified": False,
        "customer": None,           # dict from /customers/lookup once verified
        "candidate_account": None,  # located-but-not-verified account
    }
