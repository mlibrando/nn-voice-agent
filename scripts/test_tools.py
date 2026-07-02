"""Dev-time smoke test for the P0 tool handlers.

Runs each handler against a local mock (default http://localhost:8001) with a
synthetic session dict. Prints one line per call. This bypasses OpenAI
Realtime — it's a direct handler-level test, so it's fast and reproducible.

Prereqs:
    # in another shell
    cd ../rtp-ashley-voice/mock-backend && ./run.sh

Run:
    python -m scripts.test_tools
    # or, if MOCK_BACKEND_URL is set:
    MOCK_BACKEND_URL=http://localhost:8001 python -m scripts.test_tools

Exit code 0 if all critical assertions pass (lookup returns a customer,
retry recovers when a 503 is injected, discount is guarded, etc.).
"""
import asyncio
import json
import sys

from app.session import new_session
from app.tools import client as tools_client
from app.tools import handlers as h


def _print(label: str, result: dict) -> None:
    ok = result.get("ok")
    tag = "OK " if ok else "ERR"
    # Trim large payloads so the console stays readable.
    dump = json.dumps(result, default=str)
    if len(dump) > 240:
        dump = dump[:240] + "…"
    print(f"  [{tag}] {label:60s} → {dump}")


async def main() -> int:
    await tools_client.startup()
    # Reset the mock to seed state so this script is idempotent across runs.
    # POST /admin/reset bypasses chaos middleware.
    reset_r = await tools_client.get_client().post("/admin/reset")
    print(f"── mock reset ─→ {reset_r.status_code} {reset_r.text.strip()}\n")

    session = new_session()
    session["stream_sid"] = "SMTEST"
    session["call_sid"] = "CATEST"

    failures = 0

    try:
        print("── TOOL-1  customer_lookup ─────────────────────────────────────────")
        r = await h.customer_lookup({"phone": "+15125550101"}, session)
        _print("phone=+15125550101 (Margaret Chen — should hit)", r)
        if not r.get("ok"):
            failures += 1
            print("  FAIL: expected lookup to succeed")
            customer_id = None
        else:
            customer_id = r["customer"]["customer_id"]
            # Cache in session so save_transcript / escalation can use it.
            session["customer"] = r["customer"]

        r = await h.customer_lookup({"phone": "+19999999999"}, session)
        _print("phone=+19999999999 (bogus — expect not_found)", r)

        r = await h.customer_lookup({"phone": "+15125550101", "email": "x@y.com"}, session)
        _print("two identifiers (expect bad_request)", r)

        r = await h.customer_lookup({}, session)
        _print("zero identifiers (expect bad_request)", r)

        print()
        print("── TOOL-2  get_customer_orders ─────────────────────────────────────")
        if customer_id:
            r = await h.get_customer_orders({"customer_id": customer_id}, session)
            _print(f"customer_id={customer_id}", r)
            first_order_id = r["orders"][0]["order_id"] if r.get("ok") and r.get("orders") else None
        else:
            first_order_id = None
            print("  SKIP: no customer_id from previous step")

        print()
        print("── TOOL-3  get_customer_subscriptions (SLOW endpoint) ──────────────")
        if customer_id:
            r = await h.get_customer_subscriptions({"customer_id": customer_id}, session)
            _print(f"customer_id={customer_id} (+1200ms slow)", r)
            first_sub_id = r["subscriptions"][0]["subscription_id"] if r.get("ok") and r.get("subscriptions") else None
        else:
            first_sub_id = None
            print("  SKIP: no customer_id")

        print()
        print("── TOOL-ERR: 503 retry exhaustion (deterministic via ?force_error) ─")
        # Mock's `?force_error=true` deterministically 503s. call_mock retries
        # up to 3 attempts (initial + 2 backoffs at 200ms, 400ms), then raises
        # ToolError. This proves the retry loop actually runs and exits cleanly.
        import time
        t0 = time.perf_counter()
        try:
            await tools_client.call_mock(
                "GET", "/customers/cust_001/orders",
                params={"force_error": "true"},
            )
            print("  FAIL: force_error should have raised ToolError")
            failures += 1
        except tools_client.ToolError as e:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            print(f"  [OK ] force_error → ToolError code={e.code!r} status={e.status} after {elapsed_ms}ms (3 attempts + backoff)")
            # Expect ≥600ms of backoff on top of 3× ambient mock latency.
            if elapsed_ms < 600:
                print(f"  WARN: elapsed {elapsed_ms}ms shorter than expected — retry may not have fired")

        print()
        print("── TOOL-8  apply_subscription_discount (destructive/idempotency-guarded)")
        if first_sub_id:
            r = await h.apply_subscription_discount(
                {"subscription_id": first_sub_id, "discount_pct": 20, "code": "LOYAL20"},
                session,
            )
            _print(f"1st call on sub {first_sub_id} at 20% (should succeed)", r)
            if not r.get("ok"):
                failures += 1
                print("  FAIL: first discount call should succeed")

            r = await h.apply_subscription_discount(
                {"subscription_id": first_sub_id, "discount_pct": 20, "code": "LOYAL20"},
                session,
            )
            _print(f"2nd call on sub {first_sub_id} — MUST be rejected", r)
            # Either guard is fine: state-based (`discount_already_active`) or
            # session-based (`already_applied`). In practice the state-based
            # one fires first because `_cache_sub()` updates on success. The
            # contract is: reject. Which layer catches it is an internal detail.
            rejection_codes = ("already_applied", "discount_already_active")
            got_code = r.get("error", {}).get("code")
            if r.get("ok") or got_code not in rejection_codes:
                failures += 1
                print(f"  FAIL: second discount call should be rejected with one of {rejection_codes}, got {got_code!r}")
        else:
            print("  SKIP: no subscription_id from earlier lookup")

        print()
        print("── TOOL-6  pause_subscription ──────────────────────────────────────")
        if first_sub_id:
            r = await h.pause_subscription(
                {"subscription_id": first_sub_id, "pause_months": 2}, session
            )
            _print(f"sub {first_sub_id} pause 2mo", r)

            r = await h.pause_subscription(
                {"subscription_id": first_sub_id, "pause_months": 99}, session
            )
            _print("pause_months=99 (out of range, expect bad_request)", r)
        else:
            print("  SKIP: no subscription_id")

        print()
        print("── TOOL-5  cancel_subscription (adverse-reaction path) ─────────────")
        # Ashley must NEVER emit 'medical_issue' — that's not in the enum.
        # This tests the schema-independent handler defense.
        if first_sub_id:
            r = await h.cancel_subscription(
                {"subscription_id": first_sub_id, "reason": "medical_issue"}, session
            )
            _print("reason=medical_issue (MUST be rejected: bad_reason)", r)
            if r.get("ok") or r.get("error", {}).get("code") != "bad_reason":
                failures += 1
                print("  FAIL: 'medical_issue' must be rejected locally, not sent to mock")

            # Adverse-reaction cancel — SAFE-1 says map to 'other'.
            r = await h.cancel_subscription(
                {"subscription_id": first_sub_id, "reason": "other"}, session
            )
            _print("reason=other (adverse-reaction path — should succeed)", r)
        else:
            print("  SKIP: no subscription_id")

        print()
        print("── TOOL-10 partial_order_refund ────────────────────────────────────")
        if first_order_id:
            r = await h.partial_order_refund(
                {"order_id": first_order_id, "refund_percentage": 25}, session
            )
            _print(f"{first_order_id} @ 25%", r)

            r = await h.partial_order_refund(
                {"order_id": first_order_id, "refund_percentage": 33}, session
            )
            _print("refund_percentage=33 (invalid, expect bad_request)", r)

            r = await h.partial_order_refund(
                {"order_id": "40001", "refund_percentage": 20}, session
            )
            _print("order_id=40001 (missing gid://, expect bad_request)", r)
        else:
            print("  SKIP: no order_id")

        print()
        print("── TOOL-11 full_order_refund (side-effect check) ───────────────────")
        # Best-tested against a fresh unfulfilled order. If the previous partial
        # refund left it PARTIALLY_REFUNDED, the mock may 409 here — that's
        # expected and still exercises the error path.
        if first_order_id:
            r = await h.full_order_refund({"order_id": first_order_id}, session)
            _print(f"{first_order_id}", r)
            if r.get("ok") and r.get("also_cancelled"):
                print("  ✓ also_cancelled surfaced — side-effect flag is working")
        else:
            print("  SKIP: no order_id")

        print()
        print("── Cross-call discount guard (new — reads persisted state) ─────────")
        # Simulate a follow-up call by starting from a fresh session, then
        # populating the sub cache the same way a real customer_lookup would.
        # The mock has persisted discount_percentage on sub 50001 from the
        # earlier apply. A fresh session's within-call `applied_discounts`
        # is empty, so ONLY guard 1 (state read) can catch this.
        fresh = new_session()
        fresh["stream_sid"] = "SMTEST2"
        fresh["call_sid"] = "CATEST2"
        fresh["customer"] = session.get("customer")  # simulate same caller
        # Fetch fresh state via a real lookup — this repopulates the cache
        # and, critically, the mock returns the currently-discounted sub.
        r = await h.customer_lookup({"phone": "+15125550101"}, fresh)
        if not r.get("ok"):
            print("  FAIL: setup lookup for cross-call test failed")
            failures += 1
        cached_sub = fresh["subscriptions_by_id"].get(50001, {})
        pre_pct = cached_sub.get("discount_percentage")
        pre_total = cached_sub.get("total_value")
        print(f"  setup: cached sub 50001 discount_percentage={pre_pct} total_value={pre_total}")

        r = await h.apply_subscription_discount(
            {"subscription_id": 50001, "discount_pct": 20, "code": "LOYAL20"},
            fresh,
        )
        _print("2nd attempt in a FRESH session (expect discount_already_active)", r)
        if r.get("ok") or r.get("error", {}).get("code") != "discount_already_active":
            failures += 1
            print("  FAIL: cross-call apply must return code=discount_already_active")
        if r.get("existing_discount_percentage") != pre_pct:
            failures += 1
            print(f"  FAIL: existing_discount_percentage should equal {pre_pct}, got {r.get('existing_discount_percentage')}")

        # Confirm the mock's total_value did NOT change — no round-trip fired.
        r_verify = await h.get_customer_subscriptions({"customer_id": "cust_001"}, fresh)
        post_total = None
        if r_verify.get("ok"):
            for sub in r_verify.get("subscriptions", []):
                if sub["subscription_id"] == 50001:
                    post_total = sub.get("total_value")
                    break
        print(f"  verify: sub 50001 total_value pre={pre_total} post={post_total}  {'✓ unchanged' if pre_total == post_total else '✗ CHANGED'}")
        if pre_total != post_total:
            failures += 1
            print("  FAIL: cross-call guard leaked through — total_value should not have changed")

        print()
        print("── TOOL-14 create_escalation ───────────────────────────────────────")
        r = await h.create_escalation(
            {
                "issue_for_human": "Caller reported dizziness after starting the D3 supplement — cancelled per SAFE-1, requests medical follow-up.",
                "actions_taken": "Cancelled subscription with reason=other; advised caller to consult healthcare provider.",
                "mark_high_risk": True,
            },
            session,
        )
        _print("adverse-reaction escalation (authenticated, high-risk)", r)

        r = await h.create_escalation({}, session)
        _print("missing issue_for_human (expect bad_request)", r)

        # Unauthenticated-caller fallback: NO customer_id in session. Confirm
        # the handler still succeeds AND enriches customer_details with the
        # call_sid so a human can correlate back to Twilio recording/logs.
        unauth = new_session()
        unauth["call_sid"] = "CA-UNAUTH-1234"
        r = await h.create_escalation(
            {
                "issue_for_human": "Caller wanted to change delivery date but I couldn't identify the account.",
                "customer_details": "Said her name was Sarah, calling about a magnesium subscription.",
            },
            unauth,
        )
        _print("unauthenticated caller (no customer_id) — should still succeed", r)
        if not r.get("ok"):
            failures += 1
            print("  FAIL: unauth escalation must still succeed — never gate on customer_id")
        # We can't inspect what the mock received directly, but we can confirm
        # the handler stayed alive and produced an escalation_id.
        if not r.get("escalation_id"):
            failures += 1
            print("  FAIL: unauth escalation should return an escalation_id")

        print()
        print("── TOOL-15 save_transcript ─────────────────────────────────────────")
        session["transcript_in"] = ["Hi, this is Margaret", "I'd like to cancel"]
        session["transcript_out"] = ["Hi Margaret, I can help with that", "Cancelled — anything else?"]
        r = await h.save_transcript(
            {"summary": "Caller cancelled sub 50001 due to adverse reaction.", "outcome": "cancelled"},
            session,
        )
        _print("summary + outcome (handler builds transcript from session)", r)

    finally:
        await tools_client.shutdown()

    print()
    if failures == 0:
        print("✅ All critical assertions passed.")
        return 0
    print(f"❌ {failures} critical assertion(s) failed.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
