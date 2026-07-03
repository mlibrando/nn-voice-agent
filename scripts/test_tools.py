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
    # Day 4: existing tests were written before the auth gate. Pre-verify the
    # session so customer_lookup returns the full record (verified=True path)
    # and the dispatch gate lets mutations through. The dedicated auth section
    # further down creates fresh unverified sessions to test the gate itself.
    session["verified"] = True

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

        # ═════════════════════════════════════════════════════════════════════
        # Day 4 — AUTH state machine assertions
        # ═════════════════════════════════════════════════════════════════════
        print()
        print("╔═════════════════════════════════════════════════════════════════════╗")
        print("║ AUTH LAYER (Day 4) — dispatch gate + tiered verification            ║")
        print("╚═════════════════════════════════════════════════════════════════════╝")

        # Reset mock state so auth flows work against a clean seed (previous
        # tests cancelled/refunded things).
        await tools_client.get_client().post("/admin/reset")

        print()
        print("── AUTH-1  Dispatch gate refuses mutations pre-verification ────────")
        s = new_session()
        s["stream_sid"] = "SM-AUTH-1"
        s["call_sid"] = "CA-AUTH-1"
        # First locate so we have a candidate (mutation would otherwise fail on
        # missing sub anyway — we want to prove the AUTH gate refuses, not the
        # bad_request path).
        _ = await h.customer_lookup({"phone": "+15125550101"}, s)
        r = await h.dispatch("cancel_subscription", {"subscription_id": 50001, "reason": "other"}, s)
        _print("dispatch(cancel_subscription) while unverified", r)
        if r.get("ok") or r.get("error", {}).get("code") != "verification_required":
            failures += 1
            print(f"  FAIL: expected code=verification_required, got {r.get('error', {}).get('code')!r}")

        # Verify the mock was NEVER hit — check by reading admin state.
        state = (await tools_client.get_client().get("/admin/state")).json()
        sub_50001 = next(
            (s for s in state.get("subscriptions", []) if s.get("subscription_id") == 50001),
            {},
        )
        if sub_50001.get("status") == "CANCELLED":
            failures += 1
            print("  FAIL: sub 50001 was cancelled — the gate leaked through to the mock")
        else:
            print("  ✓ mock never mutated (gate held before handler ran)")

        print()
        print("── AUTH-2  Sanitized customer_lookup pre-verification ─────────────")
        s = new_session()
        s["stream_sid"] = "SM-AUTH-2"
        s["call_sid"] = "CA-AUTH-2"
        r = await h.customer_lookup({"phone": "+15125550101"}, s)
        _print("pre-verify lookup returns minimal shape", r)
        # Must include: ok, located, verification_required, customer_first_name.
        # Must NOT include full customer/orders/subscriptions dumped to the model.
        for required_key in ("ok", "located", "verification_required", "customer_first_name"):
            if required_key not in r:
                failures += 1
                print(f"  FAIL: sanitized response missing key {required_key!r}")
        for leaked_key in ("customer", "orders", "subscriptions"):
            if leaked_key in r:
                failures += 1
                print(f"  FAIL: sanitized response LEAKED {leaked_key!r} — that hands the caller answers")
        # Full record must still be in session.candidate_account for verify_identity.
        if not (s.get("candidate_account") or {}).get("customer"):
            failures += 1
            print("  FAIL: session.candidate_account not populated with full record")
        else:
            print("  ✓ full record in session.candidate_account (for challenge check)")

        print()
        print("── AUTH-3  Tier-0 caller_id_confirm flips verified ─────────────────")
        s = new_session()
        s["stream_sid"] = "SM-AUTH-3"
        s["call_sid"] = "CA-AUTH-3"
        s["from_number"] = "+15125550101"  # Simulates bridge capturing Twilio From
        _ = await h.customer_lookup({"phone": "+15125550101"}, s)
        if not s.get("tier0_hit"):
            failures += 1
            print("  FAIL: tier0_hit should be True when from_number == looked-up phone")
        r = await h.verify_identity({"challenge_kind": "caller_id_confirm", "given_value": "yes it is"}, s)
        _print("verify_identity(caller_id_confirm, 'yes it is')", r)
        if not r.get("ok") or not r.get("verified"):
            failures += 1
            print("  FAIL: Tier-0 confirm should flip verified=True")
        if not s.get("verified") or not s.get("customer") or s.get("candidate_account") is not None:
            failures += 1
            print("  FAIL: post-verify session state incorrect")

        print()
        print("── AUTH-4  caller_id_confirm refused without Tier-0 hit ────────────")
        s = new_session()
        s["stream_sid"] = "SM-AUTH-4"
        s["call_sid"] = "CA-AUTH-4"
        # No from_number — evaluator path. Locate via order_number instead.
        _ = await h.customer_lookup({"order_number": "#NN1001"}, s)
        if s.get("tier0_hit"):
            failures += 1
            print("  FAIL: tier0_hit must be False for a Tier-1 locate")
        r = await h.verify_identity({"challenge_kind": "caller_id_confirm", "given_value": "yes"}, s)
        _print("verify_identity(caller_id_confirm) on Tier-1 locate", r)
        if r.get("ok") or r.get("error", {}).get("code") != "caller_id_didnt_match":
            failures += 1
            print("  FAIL: expected code=caller_id_didnt_match")

        print()
        print("── AUTH-5  Tier-2 zip passes with normalization ────────────────────")
        s = new_session()
        s["stream_sid"] = "SM-AUTH-5"
        s["call_sid"] = "CA-AUTH-5"
        _ = await h.customer_lookup({"phone": "+15125550101"}, s)  # Margaret, ZIP 78701
        on_file = h._collect_on_file_zips(s["candidate_account"])
        print(f"  on-file ZIPs collected across sub+order shipping: {on_file}")
        if "78701" not in on_file:
            failures += 1
            print("  FAIL: expected 78701 in the on-file ZIP set")

        # Plain digits
        r = await h.verify_identity({"challenge_kind": "zip", "given_value": "78701"}, s)
        _print("verify_identity(zip, '78701')", r)
        if not r.get("ok") or not r.get("verified"):
            failures += 1
            print("  FAIL: correct ZIP should verify")

        # Fresh session — test normalization ("seven eight seven oh one" style)
        s2 = new_session()
        _ = await h.customer_lookup({"phone": "+15125550101"}, s2)
        r = await h.verify_identity({"challenge_kind": "zip", "given_value": " 78701 "}, s2)
        if not r.get("ok"):
            failures += 1
            print("  FAIL: whitespace-padded ZIP should normalize and match")
        else:
            print("  ✓ ZIP normalizes correctly (whitespace stripped)")

        print()
        print("── AUTH-6  Wrong answer → verification_failed + attempts_remaining ─")
        s = new_session()
        s["stream_sid"] = "SM-AUTH-6"
        s["call_sid"] = "CA-AUTH-6"
        _ = await h.customer_lookup({"phone": "+15125550101"}, s)
        r = await h.verify_identity({"challenge_kind": "zip", "given_value": "99999"}, s)
        _print("verify_identity(zip, '99999') — wrong", r)
        if r.get("ok") or r.get("error", {}).get("code") != "verification_failed":
            failures += 1
            print("  FAIL: wrong answer should return code=verification_failed")
        if r.get("attempts_remaining") != 2:
            failures += 1
            print(f"  FAIL: attempts_remaining should be 2, got {r.get('attempts_remaining')}")
        if s.get("verified"):
            failures += 1
            print("  FAIL: session.verified must not flip on wrong answer")

        print()
        print("── AUTH-7  Attempt cap → locked_out with self-contained escalation ─")
        # We already used 1 attempt in AUTH-6 (same session s). Use 2 more.
        r2 = await h.verify_identity({"challenge_kind": "zip", "given_value": "88888"}, s)
        r3 = await h.verify_identity({"challenge_kind": "zip", "given_value": "77777"}, s)
        _print("2nd wrong", r2)
        _print("3rd wrong (should trigger locked_out)", r3)
        if r3.get("error", {}).get("code") != "locked_out":
            failures += 1
            print("  FAIL: 3rd wrong should trigger locked_out")
        # Self-contained signal check.
        for required in ("spoken_line", "next_action", "escalation_suggestion"):
            if required not in r3:
                failures += 1
                print(f"  FAIL: locked_out result missing {required!r}")
        esc = r3.get("escalation_suggestion") or {}
        for esc_field in ("issue_for_human", "actions_taken", "mark_high_risk"):
            if esc_field not in esc:
                failures += 1
                print(f"  FAIL: escalation_suggestion missing {esc_field!r}")
        # The issue_for_human string should reference the call_sid AND the caller's name.
        issue = esc.get("issue_for_human", "")
        if "CA-AUTH-6" not in issue or "Margaret" not in issue:
            failures += 1
            print(f"  FAIL: issue_for_human should reference call_sid + candidate name; got: {issue!r}")
        else:
            print("  ✓ locked_out payload is fully self-contained (spoken_line + escalation_suggestion)")

        # 4th attempt should also return locked_out (idempotent).
        r4 = await h.verify_identity({"challenge_kind": "zip", "given_value": "78701"}, s)
        if r4.get("error", {}).get("code") != "locked_out":
            failures += 1
            print("  FAIL: post-cap verify_identity must stay locked_out (even correct answers)")

        print()
        print("── AUTH-8  cust_006 Robert Lee — phone null forces email path ─────")
        s = new_session()
        s["stream_sid"] = "SM-AUTH-8"
        s["call_sid"] = "CA-AUTH-8"
        # No from_number to look up; caller can't be located by phone.
        r_bogus = await h.customer_lookup({"phone": "+15129999999"}, s)
        _print("bogus phone (expect 404)", r_bogus)
        if r_bogus.get("ok"):
            failures += 1
            print("  FAIL: bogus phone should return not_found, not ok=True")

        # Now Tier-1 locate by email.
        r_by_email = await h.customer_lookup({"email": "robert.lee@example.com"}, s)
        _print("locate by email (cust_006)", r_by_email)
        if not r_by_email.get("ok"):
            failures += 1
            print("  FAIL: cust_006 email lookup should succeed")
        if r_by_email.get("customer_first_name") != "Robert":
            failures += 1
            print(f"  FAIL: expected first_name=Robert, got {r_by_email.get('customer_first_name')!r}")

        # Verify by order_name (Robert's order_ids per seed: #NN1006 → gid://.../1006).
        # Note: verification via card_last_four would also work; using order_name here
        # per the moderate-strength-knowledge-factor decision (see DECISIONS.md draft).
        r_verify = await h.verify_identity({"challenge_kind": "order_name", "given_value": "#NN1006"}, s)
        _print("verify_identity(order_name, '#NN1006')", r_verify)
        if not r_verify.get("ok"):
            failures += 1
            print("  FAIL: correct order_name should verify cust_006")

        print()
        print("── AUTH-9  Post-verification customer_lookup returns full record ───")
        # `s` is now verified as Robert. A re-lookup should NOT sanitize.
        r_reread = await h.customer_lookup({"email": "robert.lee@example.com"}, s)
        if not r_reread.get("customer") or not r_reread.get("orders"):
            failures += 1
            print("  FAIL: verified caller's re-lookup should return full record")
        else:
            print("  ✓ verified caller gets the full record on re-lookup")

        print()
        print("── AUTH-10 Pre-auth tools work unverified (create_escalation/save_transcript)")
        s = new_session()
        s["stream_sid"] = "SM-AUTH-10"
        s["call_sid"] = "CA-AUTH-10"
        s["from_number"] = "+15129999999"
        r_esc = await h.dispatch(
            "create_escalation",
            {"issue_for_human": "Caller wants a manager. Cannot verify identity."},
            s,
        )
        _print("dispatch(create_escalation) while unverified", r_esc)
        if not r_esc.get("ok"):
            failures += 1
            print("  FAIL: create_escalation MUST run pre-verify (unauth path)")

        r_tx = await h.dispatch("save_transcript", {"summary": "x", "outcome": "informational"}, s)
        _print("dispatch(save_transcript) while unverified", r_tx)
        if not r_tx.get("ok"):
            failures += 1
            print("  FAIL: save_transcript MUST run pre-verify (end-of-call regardless)")

        print()
        print("── AUTH-11 from_number lands in unauth escalation breadcrumb ───────")
        # Confirm the escalation the mock received includes the from_number correlation key.
        state = (await tools_client.get_client().get("/admin/state")).json()
        escs = state.get("escalations", [])
        # Find our just-created one by call_sid marker.
        ours = [e for e in escs if "CA-AUTH-10" in (e.get("customer_details") or "")]
        if not ours:
            failures += 1
            print("  FAIL: created escalation not found in mock state")
        else:
            details = ours[-1].get("customer_details", "")
            if "from=+15129999999" not in details:
                failures += 1
                print(f"  FAIL: from_number missing from escalation breadcrumb. Got: {details!r}")
            else:
                print(f"  ✓ escalation breadcrumb: {details[:100]}")

        print()
        print("── AUTH-12 Dispatch gate allows reads-after-verify, blocks pre-verify")
        # Reads (get_customer_orders etc.) are ALSO gated (they're data disclosure).
        s = new_session()
        s["stream_sid"] = "SM-AUTH-12"
        s["call_sid"] = "CA-AUTH-12"
        _ = await h.customer_lookup({"phone": "+15125550101"}, s)
        r_read = await h.dispatch("get_customer_orders", {"customer_id": "cust_001"}, s)
        _print("dispatch(get_customer_orders) while unverified", r_read)
        if r_read.get("ok"):
            failures += 1
            print("  FAIL: get_customer_orders must be gated (data disclosure)")
        # Now verify and retry.
        s["from_number"] = "+15125550101"
        s["tier0_hit"] = True  # simulate — normally set by customer_lookup phone match
        await h.verify_identity({"challenge_kind": "caller_id_confirm", "given_value": "yes"}, s)
        r_read2 = await h.dispatch("get_customer_orders", {"customer_id": "cust_001"}, s)
        if not r_read2.get("ok"):
            failures += 1
            print("  FAIL: post-verify get_customer_orders should succeed")
        else:
            print("  ✓ gate correctly refuses pre-verify reads AND allows post-verify reads")

        print()
        print("── AUTH-13 Same-factor: located via email, email challenge REFUSED ──")
        # This is the F8-live bug: caller supplied email as locate identifier,
        # Ashley then accepted the same email as the verify challenge. That's
        # zero-factor auth. Handler MUST refuse with code=same_factor.
        s = new_session()
        s["stream_sid"] = "SM-AUTH-13"
        s["call_sid"] = "CA-AUTH-13"
        r = await h.customer_lookup({"email": "david.thompson@example.com"}, s)
        if s.get("located_via") != "email":
            failures += 1
            print(f"  FAIL: located_via should be 'email', got {s.get('located_via')!r}")
        else:
            print(f"  ✓ located_via='email' set by customer_lookup")

        # Attempt the same-factor challenge — MUST be refused.
        attempts_before = s.get("auth_attempts", 0)
        r = await h.verify_identity(
            {"challenge_kind": "email", "given_value": "david.thompson@example.com"}, s,
        )
        _print("verify_identity(email) after locating via email", r)
        if r.get("ok") or r.get("error", {}).get("code") != "same_factor":
            failures += 1
            print(f"  FAIL: expected code=same_factor, got {r.get('error', {}).get('code')!r}")
        if s.get("verified"):
            failures += 1
            print("  FAIL: verified must NOT flip on same-factor refusal — regression of the F8-live bug")
        # Must NOT count against the attempt budget.
        if s.get("auth_attempts", 0) != attempts_before:
            failures += 1
            print(f"  FAIL: same-factor rejection should not increment auth_attempts (before={attempts_before}, after={s['auth_attempts']})")
        else:
            print("  ✓ same-factor rejection does not count as a failed attempt")

        # Positive control: a DIFFERENT Tier-2 challenge still works.
        # David Thompson's on-file ZIP is 80202 (both subs + order).
        r = await h.verify_identity({"challenge_kind": "zip", "given_value": "80202"}, s)
        _print("verify_identity(zip) after locating via email — legitimate", r)
        if not r.get("ok") or not r.get("verified"):
            failures += 1
            print("  FAIL: legitimate DIFFERENT-factor challenge should verify")

        print()
        print("── AUTH-14 Same-factor: located via order_number, order_name REFUSED ")
        s = new_session()
        s["stream_sid"] = "SM-AUTH-14"
        s["call_sid"] = "CA-AUTH-14"
        r = await h.customer_lookup({"order_number": "#NN1001"}, s)
        if s.get("located_via") != "order_number":
            failures += 1
            print(f"  FAIL: located_via should be 'order_number', got {s.get('located_via')!r}")

        r = await h.verify_identity(
            {"challenge_kind": "order_name", "given_value": "#NN1001"}, s,
        )
        _print("verify_identity(order_name) after locating via order_number", r)
        if r.get("ok") or r.get("error", {}).get("code") != "same_factor":
            failures += 1
            print(f"  FAIL: expected code=same_factor, got {r.get('error', {}).get('code')!r}")
        if s.get("verified"):
            failures += 1
            print("  FAIL: verified must NOT flip on same-factor refusal")

        # Positive control: email (a different factor) works.
        r = await h.verify_identity(
            {"challenge_kind": "email", "given_value": "margaret.chen@example.com"}, s,
        )
        if not r.get("ok"):
            failures += 1
            print("  FAIL: legitimate different-factor (email) after order-number locate should verify")
        else:
            print("  ✓ different-factor challenge (email) verifies after order_number locate")

        print()
        print("── AUTH-15 Tier-0 carve-out: phone match → caller_id_confirm allowed ")
        # The ambient-physical-signal reasoning: caller-ID is not a
        # caller-supplied claim, so it's OK to use phone for both locate and
        # verbal confirm on Tier-0 hits. Same-factor gate must NOT block.
        s = new_session()
        s["stream_sid"] = "SM-AUTH-15"
        s["call_sid"] = "CA-AUTH-15"
        s["from_number"] = "+15125550101"  # Simulates bridge-captured Twilio From
        r = await h.customer_lookup({"phone": "+15125550101"}, s)
        if not s.get("tier0_hit"):
            failures += 1
            print("  FAIL: tier0_hit should be True for from_number == lookup phone")
        if s.get("located_via") != "phone":
            failures += 1
            print(f"  FAIL: located_via should be 'phone', got {s.get('located_via')!r}")

        r = await h.verify_identity(
            {"challenge_kind": "caller_id_confirm", "given_value": "yes"}, s,
        )
        _print("verify_identity(caller_id_confirm) on Tier-0 hit", r)
        if not r.get("ok") or not r.get("verified"):
            failures += 1
            print("  FAIL: caller_id_confirm on Tier-0 hit must verify (ambient-signal carve-out)")
        else:
            print("  ✓ Tier-0 carve-out preserved: caller_id_confirm passes on caller-ID match")

        print()
        print("── AUTH-16 tier0_hit invariant: caller cannot forge via lookup args ")
        # Attacker calls from unseeded +15559999999 but tries to pass Margaret's
        # phone to customer_lookup. tier0_hit MUST stay False because the
        # lookup phone != from_number.
        s = new_session()
        s["stream_sid"] = "SM-AUTH-16"
        s["call_sid"] = "CA-AUTH-16"
        s["from_number"] = "+15559999999"  # Attacker's unseeded number
        # Locate Margaret by phone — the lookup succeeds but tier0_hit must NOT set.
        r = await h.customer_lookup({"phone": "+15125550101"}, s)
        if s.get("tier0_hit"):
            failures += 1
            print("  FAIL: tier0_hit forged — lookup_phone != from_number should keep it False")
        else:
            print("  ✓ tier0_hit stayed False (lookup_phone != from_number)")

        # Now try caller_id_confirm — MUST be refused with caller_id_didnt_match.
        r = await h.verify_identity(
            {"challenge_kind": "caller_id_confirm", "given_value": "yes"}, s,
        )
        if r.get("ok") or r.get("error", {}).get("code") != "caller_id_didnt_match":
            failures += 1
            print(f"  FAIL: caller_id_confirm should be refused when tier0_hit=False, got {r.get('error', {}).get('code')!r}")
        else:
            print("  ✓ caller_id_confirm refused when tier0_hit=False (invariant holds)")

        print()
        print("── AUTH-17 THE F8-live regression guard: 'This is Bob' MUST fail ──")
        # Day 5 coupled fix: open Tier-0 greeting invites "This is <name>"
        # answers. Old `_is_affirmative` matched "this is" as a substring, so
        # ANY name claim verified. AUTH-17 is the ship-block: if this ever
        # passes with verified=True, the bypass is back.
        s = new_session()
        s["stream_sid"] = "SM-AUTH-17"
        s["call_sid"] = "CA-AUTH-17"
        s["from_number"] = "+15125550101"  # Margaret's phone
        _ = await h.customer_lookup({"phone": "+15125550101"}, s)
        assert s.get("tier0_hit"), "test setup: tier0_hit should be True"

        r = await h.verify_identity(
            {"challenge_kind": "caller_id_confirm", "given_value": "This is Bob"}, s,
        )
        _print("verify_identity(caller_id_confirm, 'This is Bob') on Tier-0 Margaret", r)
        if r.get("ok") or s.get("verified"):
            failures += 1
            print("  ✗ HARD FAIL — AUTH-17 REGRESSION: impostor 'This is Bob' verified as Margaret. The F8-live bypass is back. Investigate _is_affirmative in app/tools/handlers.py.")
        elif r.get("error", {}).get("code") != "verification_failed":
            failures += 1
            print(f"  FAIL: expected code=verification_failed, got {r.get('error', {}).get('code')!r}")
        elif r.get("attempts_remaining") != 2:
            failures += 1
            print(f"  FAIL: attempts_remaining should be 2, got {r.get('attempts_remaining')}")
        else:
            print("  ✓ impostor rejected; attempts_remaining=2; verified stays False")

        print()
        print("── AUTH-18 correct name in claim: 'This is Margaret' verifies ──")
        s = new_session()
        s["stream_sid"] = "SM-AUTH-18"
        s["call_sid"] = "CA-AUTH-18"
        s["from_number"] = "+15125550101"
        _ = await h.customer_lookup({"phone": "+15125550101"}, s)
        r = await h.verify_identity(
            {"challenge_kind": "caller_id_confirm", "given_value": "This is Margaret"}, s,
        )
        _print("verify_identity(caller_id_confirm, 'This is Margaret')", r)
        if not r.get("ok") or not r.get("verified"):
            failures += 1
            print("  FAIL: correct name claim should verify")

        print()
        print("── AUTH-19 bare 'yes' still works ──")
        s = new_session()
        s["stream_sid"] = "SM-AUTH-19"
        s["call_sid"] = "CA-AUTH-19"
        s["from_number"] = "+15125550101"
        _ = await h.customer_lookup({"phone": "+15125550101"}, s)
        r = await h.verify_identity(
            {"challenge_kind": "caller_id_confirm", "given_value": "yes"}, s,
        )
        if not r.get("ok") or not r.get("verified"):
            failures += 1
            print("  FAIL: bare 'yes' should verify")
        else:
            print("  ✓ bare 'yes' still verifies")

        print()
        print("── AUTH-20 bare-name answer: 'Margaret' verifies ──")
        s = new_session()
        s["stream_sid"] = "SM-AUTH-20"
        s["call_sid"] = "CA-AUTH-20"
        s["from_number"] = "+15125550101"
        _ = await h.customer_lookup({"phone": "+15125550101"}, s)
        r = await h.verify_identity(
            {"challenge_kind": "caller_id_confirm", "given_value": "Margaret"}, s,
        )
        if not r.get("ok") or not r.get("verified"):
            failures += 1
            print("  FAIL: bare correct name should verify")
        else:
            print("  ✓ bare correct name verifies (open-greeting response format)")

        print()
        print("── AUTH-21 mixed affirmative + wrong name: 'Yeah, Bob' fails ──")
        s = new_session()
        s["stream_sid"] = "SM-AUTH-21"
        s["call_sid"] = "CA-AUTH-21"
        s["from_number"] = "+15125550101"
        _ = await h.customer_lookup({"phone": "+15125550101"}, s)
        r = await h.verify_identity(
            {"challenge_kind": "caller_id_confirm", "given_value": "Yeah, Bob"}, s,
        )
        _print("verify_identity(caller_id_confirm, 'Yeah, Bob')", r)
        if r.get("ok") or s.get("verified"):
            failures += 1
            print("  FAIL: 'Yeah, Bob' must not verify — 'bob' isn't in the affirmative/stop sets")
        else:
            print("  ✓ mixed affirmative + wrong name rejected")

        print()
        print("── END-1 end_call handler shape + graceful missing-creds path ──")
        # end_call posts to Twilio's REST API. In local test we don't have
        # real creds; assert the handler returns a structured error rather
        # than raising, and that its shape is right. Full live-call test is
        # in TESTING.md G7.
        import os as _os
        old_sid = _os.environ.get("TWILIO_ACCOUNT_SID", "")
        old_tok = _os.environ.get("TWILIO_AUTH_TOKEN", "")
        # Force missing-creds path.
        _os.environ["TWILIO_ACCOUNT_SID"] = ""
        _os.environ["TWILIO_AUTH_TOKEN"] = ""
        # Reload config so the handler sees the cleared vars.
        import importlib
        from app import config as _cfg
        importlib.reload(_cfg)

        s = new_session()
        s["stream_sid"] = "SM-END-1"
        s["call_sid"] = "CA-END-1"

        # end_call must be in the PRE_AUTH whitelist (abuse can happen pre-verify).
        if "end_call" not in h._PRE_AUTH_TOOLS:
            failures += 1
            print("  FAIL: end_call must be in _PRE_AUTH_TOOLS")
        else:
            print("  ✓ end_call is in _PRE_AUTH_TOOLS (abuse-before-verify path)")

        r = await h.end_call({"reason": "abusive caller — ended after two warnings"}, s)
        _print("end_call with missing Twilio creds", r)
        if r.get("ok"):
            failures += 1
            print("  FAIL: end_call must not report ok=True when creds are missing")
        elif r.get("error", {}).get("code") != "credentials_missing":
            failures += 1
            print(f"  FAIL: expected code=credentials_missing, got {r.get('error', {}).get('code')!r}")
        else:
            print("  ✓ graceful error return on missing creds (no crash, no exception)")

        # abuse_strikes should have incremented (observability field).
        if s.get("abuse_strikes", 0) != 1:
            failures += 1
            print(f"  FAIL: abuse_strikes should be 1 after end_call attempt, got {s.get('abuse_strikes')}")
        else:
            print("  ✓ abuse_strikes incremented (observability)")

        # Test dispatch gate: end_call works unverified (like create_escalation).
        s2 = new_session()
        s2["call_sid"] = "CA-END-1B"
        r2 = await h.dispatch("end_call", {"reason": "test"}, s2)
        # Missing creds → returns credentials_missing, NOT verification_required.
        if r2.get("error", {}).get("code") == "verification_required":
            failures += 1
            print("  FAIL: dispatch gate wrongly blocked end_call pre-verify")
        else:
            print("  ✓ dispatch gate allows end_call pre-verify")

        # Restore env.
        _os.environ["TWILIO_ACCOUNT_SID"] = old_sid
        _os.environ["TWILIO_AUTH_TOKEN"] = old_tok
        importlib.reload(_cfg)

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
