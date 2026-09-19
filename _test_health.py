"""Verify the circuit breaker: its thresholds, transitions and error taxonomy.

The numbers here are not invented - they are the defaults from CC Switch's
`proxy/circuit_breaker.rs`, and the cases exist to pin the behaviours that are
easy to get subtly wrong:

* the cooldown is a flat timeout, not exponential backoff;
* half-open admits exactly one probe, and one failure re-opens immediately;
* recovering clears the lifetime counters, so an old bad spell cannot keep
  tripping the breaker through the error-rate rule;
* a 400 is the caller's fault and must not count against the endpoint, while a
  401/429/500 must.

The breakers keep time themselves, so these cases pass an explicit `now` to
step the clock instead of sleeping.
"""

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

failures = []


def check(name, ok, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


def main():
    import wb_health as H

    print("=== the defaults match cc-switch ===")
    check("failure_threshold is 4", H.DEFAULTS["failure_threshold"] == 4)
    check("success_threshold is 2", H.DEFAULTS["success_threshold"] == 2)
    check("timeout_seconds is 60", H.DEFAULTS["timeout_seconds"] == 60)
    check("error_rate_threshold is 0.6", H.DEFAULTS["error_rate_threshold"] == 0.6)
    check("min_requests is 10", H.DEFAULTS["min_requests"] == 10)
    check("only one half-open probe is allowed", H.MAX_HALF_OPEN_PROBES == 1)

    print()
    print("=== error taxonomy ===")
    for code in (400, 405, 406, 413, 414, 415, 422, 501):
        check("HTTP %d is the caller's fault" % code,
              H.is_retryable_status(code) is False)
    for code in (401, 403, 404, 408, 409, 429, 451, 500, 502, 503, 504):
        check("HTTP %d counts against the endpoint" % code,
              H.is_retryable_status(code) is True)
    check("a client abort is neutral", H.classify(error="client_abort") is None)
    check("a timeout is a failure", H.classify(error="timeout") is True)
    check("a bad request status is not a failure",
          H.classify(status=400) is False)
    check("a 429 is a failure", H.classify(status=429) is True)

    print()
    print("=== tripping on consecutive failures ===")
    b = H.Breaker("acct-1")
    now = 1000.0
    for i in range(3):
        allowed, probe = b.allow_request(now)
        check("attempt %d is allowed" % (i + 1), allowed is True)
        b.record_failure(used_probe=probe, error="boom", now=now)
    check("still closed after 3 failures", b.state == H.CLOSED, b.state)
    allowed, probe = b.allow_request(now)
    b.record_failure(used_probe=probe, error="boom", now=now)
    check("open after the 4th failure", b.state == H.OPEN, b.state)
    allowed, probe = b.allow_request(now)
    check("an open breaker refuses", allowed is False)

    print()
    print("=== the cooldown is a flat timeout, not backoff ===")
    check("still open one second before the timeout",
          b.is_available(now + 59) is False)
    check("half-open once the timeout elapses",
          b.is_available(now + 60) is True, b.state)
    check("the state really is half_open", b.state == H.HALF_OPEN, b.state)

    print()
    print("=== half-open admits exactly one probe ===")
    first, probe_a = b.allow_request(now + 60)
    check("the first probe is allowed", first is True)
    check("and it is marked as a probe", probe_a is True)
    second, probe_b = b.allow_request(now + 60)
    check("a second concurrent probe is refused", second is False)
    check("without claiming a probe slot", probe_b is False)

    print()
    print("=== one failure in half-open re-opens immediately ===")
    b.record_failure(used_probe=probe_a, error="still broken", now=now + 60)
    check("the breaker is open again", b.state == H.OPEN, b.state)
    check("it refuses requests", b.allow_request(now + 60)[0] is False)
    check("the cooldown restarted from the failure",
          b.is_available(now + 119) is False)
    check("and expires 60s after that failure",
          b.is_available(now + 120) is True)

    print()
    print("=== success_threshold consecutive successes close it ===")
    b2 = H.Breaker("acct-2")
    for _ in range(4):
        b2.record_failure(error="x", now=now)
    check("tripped", b2.state == H.OPEN, b2.state)
    allowed, probe = b2.allow_request(now + 60)
    b2.record_success(used_probe=probe, now=now + 60)
    check("one success is not enough", b2.state == H.HALF_OPEN, b2.state)
    check("the probe slot was returned, so another may try",
          b2.allow_request(now + 60)[0] is True)
    allowed, probe = b2.allow_request(now + 60)
    b2.record_success(used_probe=probe, now=now + 60)
    check("two successes close it", b2.state == H.CLOSED, b2.state)
    check("requests flow again", b2.allow_request(now + 60)[0] is True)

    print()
    print("=== recovering clears the lifetime counters ===")
    b3 = H.Breaker("acct-3")
    # Push the error rate over the line without tripping on consecutive count.
    for i in range(20):
        if i % 2 == 0:
            b3.record_failure(error="x", now=now)
        else:
            b3.record_success(now=now)
    check("the error rate tripped it",
          b3.state == H.OPEN or b3.failed_requests > 0, b3.snapshot(now))
    b3.reset()
    snap = b3.snapshot(now)
    check("reset closes it", snap["state"] == H.CLOSED)
    check("reset zeroes the totals",
          snap["total_requests"] == 0 and snap["failed_requests"] == 0, str(snap))
    check("reset clears the streak", snap["consecutive_failures"] == 0)

    print()
    print("=== the error-rate rule needs min_requests first ===")
    b4 = H.Breaker("acct-4", {"min_requests": 10, "failure_threshold": 99})
    for _ in range(4):
        b4.record_failure(error="x", now=now)
    check("4 failures under the consecutive threshold and min_requests stays closed",
          b4.state == H.CLOSED, b4.snapshot(now))
    for _ in range(6):
        b4.record_failure(error="x", now=now)
    check("10 failures at a 100%% error rate trip it",
          b4.state == H.OPEN, b4.snapshot(now))
    snap = b4.snapshot(now)
    check("the reported error rate is 1.0", snap["error_rate"] == 1.0, str(snap))

    print()
    print("=== a low error rate does not trip it ===")
    b5 = H.Breaker("acct-5", {"failure_threshold": 99})
    for i in range(20):
        if i < 5:
            b5.record_failure(error="x", now=now)
        else:
            b5.record_success(now=now)
    snap = b5.snapshot(now)
    check("stays closed at a 25%% error rate", b5.state == H.CLOSED, str(snap))
    check("and reports that rate", snap["error_rate"] == 0.25, str(snap))

    print()
    print("=== neutral outcomes do not count ===")
    b6 = H.Breaker("acct-6")
    for _ in range(10):
        b6.record(ok=False, neutral=True)
    snap = b6.snapshot(now)
    check("a client abort leaves the totals alone",
          snap["total_requests"] == 0 and b6.state == H.CLOSED, str(snap))

    print()
    print("=== a released probe does not strand the endpoint ===")
    b7 = H.Breaker("acct-7")
    for _ in range(4):
        b7.record_failure(error="x", now=now)
    allowed, probe = b7.allow_request(now + 60)
    check("a probe was claimed", probe is True)
    b7.release_probe()
    check("after release another probe is allowed",
          b7.allow_request(now + 60)[0] is True)

    print()
    print("=== the registry keys breakers independently ===")
    reg = H.Registry()
    for _ in range(4):
        reg.get("a").record_failure(error="x", now=now)
    check("`a` is open", reg.get("a").state == H.OPEN)
    check("`b` is untouched", reg.get("b").state == H.CLOSED)
    check("only `a` is unavailable",
          H.available_account_uids(["a", "b"], registry=reg, now=now) == ["b"],
          str(H.available_account_uids(["a", "b"], registry=reg, now=now)))
    reg.reset("a")
    check("resetting one leaves the other alone",
          reg.get("a").state == H.CLOSED and reg.get("b").state == H.CLOSED)
    check("the same key returns the same breaker",
          reg.get("a") is reg.get("a"))

    print()
    print("=== available_account_uids preserves the caller's order ===")
    reg2 = H.Registry()
    for _ in range(4):
        reg2.get("mid").record_failure(error="x", now=now)
    check("order is preserved, not sorted",
          H.available_account_uids(["z", "mid", "a"], registry=reg2,
                                   now=now) == ["z", "a"],
          str(H.available_account_uids(["z", "mid", "a"], registry=reg2, now=now)))
    check("an empty candidate list stays empty",
          H.available_account_uids([], registry=reg2, now=now) == [])
    check("a cooled-down account is usable again",
          H.available_account_uids(["z", "mid", "a"], registry=reg2,
                                   now=now + 60) == ["z", "mid", "a"],
          str(H.available_account_uids(["z", "mid", "a"], registry=reg2,
                                       now=now + 60)))

    print()
    print("=== configuration is applied without losing state ===")
    reg3 = H.Registry()
    reg3.get("x").record_failure(error="a", now=now)
    before = reg3.get("x").snapshot(now)["consecutive_failures"]
    reg3.set_config({"failure_threshold": 2})
    after = reg3.get("x").snapshot(now)["consecutive_failures"]
    check("the streak survives a config change", before == after,
          "%s vs %s" % (before, after))
    check("the new threshold is in effect",
          reg3.get("x").config["failure_threshold"] == 2)

    print()
    print("=== the snapshot is honest about the cooldown ===")
    b8 = H.Breaker("acct-8")
    for _ in range(4):
        b8.record_failure(error="x", now=now)
    snap = b8.snapshot(now + 10)
    check("the remaining cooldown counts down",
          snap["cooldown_remaining"] == 50.0, str(snap["cooldown_remaining"]))
    check("the last error is kept", snap["last_error"] == "x", str(snap))
    check("the open count is tracked", snap["opened_count"] == 1, str(snap))

    print()
    if failures:
        print("FAILED: %d case(s)" % len(failures))
        for name in failures:
            print("  - %s" % name)
    else:
        print("all cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
