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
        # ─── Barge-in / playback tracking (Twilio Realtime idiom) ───
        # OpenAI's `response.done` fires when generation completes, but Twilio
        # keeps *playing* buffered audio for another 1–3s. So "response
        # in-flight" and "audio in-flight to caller" are different questions,
        # and only the second one matters for barge-in. We track playback state
        # using Twilio's own audio clock:
        #
        #   `latest_media_timestamp` — Twilio's stream position, updated on
        #     every incoming `media` event. Monotonic, ~real-time.
        #   `response_start_timestamp_twilio` — value of the above at the
        #     moment we sent the first audio delta of the CURRENT assistant
        #     item (keyed on `last_assistant_item`). Re-anchored whenever a
        #     new `item_id` appears — critically, this means the anchor is
        #     fresh at every new response's first delta, not just after a
        #     barge-in. The earlier "reset only on barge-in" scheme let the
        #     anchor go stale across normal turn completions and produced
        #     wildly over-estimated `audio_end_ms` on later barge-ins.
        #   `last_assistant_item` — item_id of the current assistant audio
        #     item, needed for `conversation.item.truncate` and for gating
        #     the anchor re-set.
        #   `audio_sent_ms` — cumulative ms of audio we've forwarded to
        #     Twilio for the current item. Used as a safety-net clamp on
        #     `audio_end_ms` so a bogus anchor can never make us ask OpenAI
        #     to truncate past the audio we actually generated.
        #   `mark_queue` — Twilio marks we've sent that haven't been echoed
        #     back yet. Non-empty ⇔ audio is buffered/playing on Twilio.
        #     This is the barge-in gate: empty queue means nothing to cancel.
        #
        # On `input_audio_buffer.speech_started`, if `mark_queue` is non-empty:
        #   audio_end_ms = max(0, min(latest - anchor, audio_sent_ms))
        #   send `conversation.item.truncate` at that offset, plus a Twilio
        #   `clear` to drain the buffer.
        "latest_media_timestamp": 0,
        "response_start_timestamp_twilio": None,
        "last_assistant_item": None,
        "audio_sent_ms": 0.0,
        "mark_queue": [],
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
        # ─── Auth state machine (Day 4) ───
        # `verified` is the single source of truth. Enforcement lives at
        # `handlers.dispatch()` — see DECISIONS.md draft "Located vs. verified".
        #
        # State layering:
        #   NEW                 → verified=False, candidate_account=None
        #   LOCATED_UNVERIFIED  → verified=False, candidate_account != None
        #   VERIFIED            → verified=True,  customer != None
        #   LOCKED_OUT          → verified=False, auth_attempts >= 3
        "verified": False,
        "customer": None,           # dict from /customers/lookup once verified
        "candidate_account": None,  # located-but-not-verified account (full record)
        "from_number": None,        # Twilio caller `From`, captured via TwiML <Parameter> on start
        "auth_attempts": 0,         # Failed verify_identity attempts; cap = 3
        "tier0_hit": False,         # True if from_number matched a customer via Tier-0 lookup
                                    # (gates caller_id_confirm challenge — Tier-1 locate cannot use it)
        "located_via": None,        # "phone" | "email" | "order_number" — which identifier
                                    # the caller supplied to customer_lookup. Read by
                                    # verify_identity to refuse same-factor challenges:
                                    # you cannot verify with the same fact you located with.
                                    # See DECISIONS.md draft "Located vs. verified" (updated
                                    # entry — the state check IS the enforcement; prompt hints
                                    # are not).
    }
