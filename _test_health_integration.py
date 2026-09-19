"""Integration checks: the circuit breaker wired into the pool + request path.

The unit tests (_test_health.py) prove the breaker's state machine. Here the
question is the wiring: does an OPEN breaker actually keep accounts out of
ready()/count_usable()/open_upstream(), do the recorded outcomes match the
semantics we chose (401/403/429 count, 5xx and network faults are neutral),
and does every path give the half-open probe slot back.

The request path is driven by patching urlopen (same approach as
_test_network_resilience.py) - no real network, no real credentials.
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ROOT = os.path.realpath(tempfile.mkdtemp(prefix="wbhealth_"))
os.environ["WB_DATA_DIR"] = ROOT

import wb_accounts
import wb_health
import wb_proxy

failures = []


def check(name, ok, detail=""):
    print("  %-56s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


def _fake_token(uid):
    head = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().strip("=")
    body = base64.urlsafe_b64encode(json.dumps(
        {"sub": uid, "uid": uid, "exp": int(time.time()) + 3600}).encode()).decode().strip("=")
    return "%s.%s.%s" % (head, body, "sig")


def make_pool(uids):
    """A fresh pool with real Account objects, no network on construction."""
    directory = tempfile.mkdtemp(prefix="accts_", dir=ROOT)
    pool = wb_accounts.AccountPool(directory)
    for uid in uids:
        account = wb_accounts.Account({
            "uid": uid, "nickname": uid, "realm": "cn",
            "accessToken": _fake_token(uid),
            "expiresAt": time.time() + 3600,
        })
        account.save(directory)
        pool.add(account)
    return pool


def trip(uid, n=4, status=429):
    for _ in range(n):
        wb_health.ACCOUNTS.get(uid).record_failure(status=status)


# ---------------------------------------------------------------------------
# 1. Pool gates: ready() / count_usable() / pick() / set_enabled()
# ---------------------------------------------------------------------------

print("== pool gates ==")
pool = make_pool(["acct-a"])
wb_health.ACCOUNTS.reset()
acc = pool.get("acct-a")
check("ready() true when breaker closed", acc.ready() is True)

trip("acct-a")
snap = wb_health.ACCOUNTS.get("acct-a").snapshot()
check("4 failures trip the breaker open", snap["state"] == wb_health.OPEN,
      "state=%s" % snap["state"])
check("ready() false when breaker open", acc.ready() is False)
check("count_usable() excludes open-breaker account",
      pool.count_usable("cn") == 0, "got %d" % pool.count_usable("cn"))
check("pick() skips open-breaker account", pool.pick(realm="cn") is None)

# Cooldown expiry alone moves OPEN -> HALF_OPEN; is_available() says yes again
# (it does not consume the probe slot).
breaker = wb_health.ACCOUNTS.get("acct-a")
breaker.config["timeout_seconds"] = 0.03
time.sleep(0.06)
snap = breaker.snapshot()
check("half_open admits availability check", snap["state"] == wb_health.HALF_OPEN
      and acc.ready() is True,
      "state=%s" % snap["state"])
check("ready() does not consume the probe slot",
      breaker.allow_request() == (True, True),
      "expected a free probe slot")
breaker.release_probe()

pool.set_enabled("acct-a", False)
pool.set_enabled("acct-a", True)   # operator reset path
check("set_enabled(True) resets the breaker",
      wb_health.ACCOUNTS.get("acct-a").snapshot()["state"] == wb_health.CLOSED)

# ---------------------------------------------------------------------------
# 2. open_upstream() outcome semantics (urlopen patched)
# ---------------------------------------------------------------------------

print("== request path ==")


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


MODE = {"v": "ok"}
real_urlopen = wb_proxy.urllib.request.urlopen
# wb_proxy.time IS the global time module, so patching its sleep also mutes
# this test's own sleeps - capture the real one for the waits below.
real_sleep = wb_proxy.time.sleep
wb_proxy.time.sleep = lambda s: None   # keep the retry loop fast


def scripted_urlopen(req, timeout=None):
    mode = MODE["v"]
    if mode == "ok":
        return FakeResponse({"ok": True})
    if mode == "auth":
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    if mode in ("429", "500"):
        raise urllib.error.HTTPError(req.full_url, int(mode), "err", {}, None)
    raise OSError("connection reset by peer")


wb_proxy.urllib.request.urlopen = scripted_urlopen
payload = {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]}


def run_request(pool_, session_key=None):
    """Drive open_upstream once; returns (ok, result, breaker-snapshot uid)."""
    try:
        resp, account = wb_proxy.open_upstream(dict(payload), session_key=session_key)
        return True, account, None
    except Exception as exc:
        return False, None, exc


# -- 401 counts against the breaker -----------------------------------------
pool401 = make_pool(["acct-401"])
wb_proxy.POOL = pool401
wb_health.ACCOUNTS.reset()
MODE["v"] = "auth"
ok, account, exc = run_request(pool401)
snap = wb_health.ACCOUNTS.get("acct-401").snapshot()
check("401 request fails", ok is False, repr(exc))
check("401 recorded as breaker failure", snap["failed_requests"] == 1,
      "failed=%s state=%s" % (snap["failed_requests"], snap["state"]))
check("401 rotates with no probe slot leaked",
      wb_health.ACCOUNTS.get("acct-401").allow_request() == (True, False),
      "probe slot should be free")

# -- 429 counts against the breaker -----------------------------------------
pool429 = make_pool(["acct-429"])
wb_proxy.POOL = pool429
wb_health.ACCOUNTS.reset()
MODE["v"] = "429"
ok, account, exc = run_request(pool429)
snap = wb_health.ACCOUNTS.get("acct-429").snapshot()
check("429 recorded as breaker failure", snap["failed_requests"] == 1,
      "failed=%s state=%s" % (snap["failed_requests"], snap["state"]))

# -- 5xx is neutral ----------------------------------------------------------
pool5xx = make_pool(["acct-5xx"])
wb_proxy.POOL = pool5xx
wb_health.ACCOUNTS.reset()
MODE["v"] = "500"
ok, account, exc = run_request(pool5xx)
snap = wb_health.ACCOUNTS.get("acct-5xx").snapshot()
check("5xx raises to caller", ok is False, repr(exc))
check("5xx neutral for the breaker",
      snap["state"] == wb_health.CLOSED and snap["total_requests"] == 0,
      "state=%s total=%s" % (snap["state"], snap["total_requests"]))
check("5xx leaves no probe slot leaked",
      wb_health.ACCOUNTS.get("acct-5xx").allow_request() == (True, False))

# -- network fault is neutral -------------------------------------------------
poolnet = make_pool(["acct-net"])
wb_proxy.POOL = poolnet
wb_health.ACCOUNTS.reset()
MODE["v"] = "net"
ok, account, exc = run_request(poolnet)
snap = wb_health.ACCOUNTS.get("acct-net").snapshot()
check("network fault raises to caller", ok is False, repr(exc))
check("network fault neutral for the breaker",
      snap["state"] == wb_health.CLOSED and snap["total_requests"] == 0,
      "state=%s total=%s" % (snap["state"], snap["total_requests"]))
check("network fault leaves no probe slot leaked",
      wb_health.ACCOUNTS.get("acct-net").allow_request() == (True, False))

# -- success records success -------------------------------------------------
poolok = make_pool(["acct-ok"])
wb_proxy.POOL = poolok
wb_health.ACCOUNTS.reset()
MODE["v"] = "ok"
ok, account, exc = run_request(poolok)
snap = wb_health.ACCOUNTS.get("acct-ok").snapshot()
check("success request ok", ok is True and account.uid == "acct-ok", repr(exc))
check("success recorded", snap["total_requests"] == 1 and snap["failed_requests"] == 0,
      "total=%s failed=%s" % (snap["total_requests"], snap["failed_requests"]))

# -- breaker-open account is skipped in open_upstream ------------------------
poolskip = make_pool(["acct-open", "acct-fine"])
wb_proxy.POOL = poolskip
wb_health.ACCOUNTS.reset()
trip("acct-open")
check("precondition: acct-open tripped",
      wb_health.ACCOUNTS.get("acct-open").snapshot()["state"] == wb_health.OPEN)
MODE["v"] = "ok"
ok, account, exc = run_request(poolskip)
check("open-breaker account skipped, healthy one served",
      ok is True and account.uid == "acct-fine", "picked %s err=%r" % (account and account.uid, exc))

# ---------------------------------------------------------------------------
# 3. half-open probe handling through open_upstream
# ---------------------------------------------------------------------------

print("== half-open ==")
poolhalf = make_pool(["acct-half"])
wb_proxy.POOL = poolhalf
wb_health.ACCOUNTS.reset()
breaker = wb_health.ACCOUNTS.get("acct-half")
trip("acct-half")
check("precondition: open", breaker.snapshot()["state"] == wb_health.OPEN)
breaker.config["timeout_seconds"] = 0.03
real_sleep(0.06)
check("precondition: half_open", breaker.snapshot()["state"] == wb_health.HALF_OPEN)

# A concurrent claimant takes the probe slot first; open_upstream must then
# refuse to send through this account (the probe is already taken).
first_allowed, _ = breaker.allow_request()
check("precondition: probe slot taken", first_allowed is True)
MODE["v"] = "auth"
ok, account, exc = run_request(poolhalf)
snap = breaker.snapshot()
check("no probe theft from the concurrent claimant",
      snap["state"] == wb_health.HALF_OPEN and breaker.half_open_inflight == 1,
      "state=%s inflight=%s" % (snap["state"], breaker.half_open_inflight))

# Now the real probe path: release the slot, let open_upstream take it itself.
breaker.release_probe()
MODE["v"] = "auth"
ok, account, exc = run_request(poolhalf)
snap = breaker.snapshot()
check("probe failure re-trips the breaker", snap["state"] == wb_health.OPEN,
      "state=%s" % snap["state"])

# Recovery: cooldown passes, two probe successes -> closed.
# The probe failure also put the account itself on a 3s single-account
# cooldown (note_error); drop it so this section isolates the breaker's own
# recovery rather than the account cooldown gate.
poolhalf.get("acct-half").clear_error()
real_sleep(0.06)
MODE["v"] = "ok"
for _ in range(2):
    ok, account, exc = run_request(poolhalf)
snap = breaker.snapshot()
check("two probe successes close the breaker", snap["state"] == wb_health.CLOSED,
      "state=%s" % snap["state"])

# ---------------------------------------------------------------------------
# 4. stale-affinity session does not pin to a tripped account
# ---------------------------------------------------------------------------

print("== session affinity ==")
poolsess = make_pool(["acct-sess"])
wb_proxy.POOL = poolsess
wb_health.ACCOUNTS.reset()
MODE["v"] = "ok"
ok, account, exc = run_request(poolsess, session_key="sess-1")
check("affinity bound to the served account",
      ok is True and account.uid == "acct-sess", repr(exc))

trip("acct-sess")
MODE["v"] = "ok"
ok, account, exc = run_request(poolsess, session_key="sess-1")
check("tripped account not served via affinity",
      ok is False and poolsess.affinity.get("sess-1") is None,
      "ok=%s bound=%s err=%r" % (ok, poolsess.affinity.get("sess-1"), exc))

# ---------------------------------------------------------------------------
wb_proxy.urllib.request.urlopen = real_urlopen
wb_proxy.time.sleep = real_sleep
real_sleep(0)   # sanity: the captured sleep still works
shutil.rmtree(ROOT, ignore_errors=True)
print()
if failures:
    print("FAILED (%d): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("ALL PASS")
