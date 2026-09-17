"""Verify the two fixes for the reported problems.

1. A transient TLS fault on the chat path is retried on the same account and
   does not cool that account down for 60 seconds. Previously one
   "violation of protocol (_ssl.c:1082)" produced a 502 for the client and
   parked a healthy account.

2. The panel stops polling when its session is rejected, so an expired
   session cannot fill the log with 401s.

Both are exercised without touching the real upstream: the chat path is driven
against a local stub that fails the first N attempts, and the dashboard change
is checked by parsing the shipped file.
"""

import json
import os
import re
import socket
import ssl
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

failures = []


def check(name, ok, detail=""):
    print("  %-56s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


print("=== 1. transient-fault classification ===")
import wb_proxy as P

# The exact error class from the report.
ssl_err = ssl.SSLError(1, "violation of protocol (_ssl.c:1082)")
check("SSL protocol violation is transient", P._is_transient_network_error(ssl_err))
check("SSL unwrapped from URLError is transient",
      P._is_transient_network_error(
          __import__("urllib.error", fromlist=["URLError"]).URLError(ssl_err)))
check("ConnectionResetError is transient",
      P._is_transient_network_error(ConnectionResetError("reset")))
check("TimeoutError is transient",
      P._is_transient_network_error(TimeoutError("timed out")))
check("a ValueError is NOT transient",
      not P._is_transient_network_error(ValueError("bad input")))
check("a KeyError is NOT transient",
      not P._is_transient_network_error(KeyError("missing")))

print()
print("=== 2. chat path retries a flaky upstream, then succeeds ===")
# A stub that aborts the TLS handshake for the first N connections, then
# serves a valid response. Standing in for the CDN edge dropping handshakes.
import http.server

ATTEMPTS = {"n": 0}
FAIL_FIRST = 2


class StubHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        ATTEMPTS["n"] += 1
        body = json.dumps({"ok": True, "attempt": ATTEMPTS["n"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# Instead of a real TLS server, drive the retry loop directly: patch urlopen to
# raise a TLS error for the first FAIL_FIRST calls, then return a response.
class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


real_urlopen = P.urllib.request.urlopen
calls = {"n": 0}


def flaky_urlopen(req, timeout=None):
    calls["n"] += 1
    if calls["n"] <= FAIL_FIRST:
        raise ssl.SSLError(1, "violation of protocol (_ssl.c:1082)")
    return FakeResponse({"ok": True, "attempt": calls["n"]})


class FakeAccount:
    uid = "testacct0001"
    realm = "intl"
    access_token = "a.b.c"
    cooldown_until = 0
    last_error = ""
    nickname = "test"
    domain = "www.workbuddy.ai"
    credits = None
    enabled = True
    path = None
    added_at = time.time()
    refresh_token = ""
    expires_at = int(time.time()) + 86400
    source = "test"

    def headers(self, purpose="chat"):
        return {"Content-Type": "application/json"}

    def clear_error(self):
        self.last_error = ""
        self.cooldown_until = 0

    def note_error(self, message, cooldown=60, single_account=False):
        self.last_error = str(message)[:200]
        self.cooldown_until = time.time() + (3 if single_account else cooldown)

    def ready(self):
        return True


class FakePool:
    def __init__(self, account):
        self.account = account
        self.affinity = type("A", (), {"unbind": staticmethod(lambda k: None)})()

    def count_ready(self, realm=None):
        return 1

    def pick_for_session(self, realm=None, session_key=None, exclude=None):
        if exclude and self.account.uid in exclude:
            return None
        return self.account


account = FakeAccount()
P.POOL = FakePool(account)
P.urllib.request.urlopen = flaky_urlopen
# Backoff would make the test slow; keep the retry count, drop the sleep.
real_sleep = P.time.sleep
P.time.sleep = lambda s: None

try:
    resp, used = P.open_upstream(
        {"model": "hy4-preview", "messages": [{"role": "user", "content": "hi"}]})
    body = json.loads(resp.read().decode())
    check("request eventually succeeded", body.get("ok") is True, str(body))
    check("took %d attempts (2 failures + success)" % calls["n"],
          calls["n"] == FAIL_FIRST + 1, "calls=%d" % calls["n"])
    check("returned the account", used is account)
    check("account was NOT cooled down", account.cooldown_until == 0,
          "cooldown_until=%s last_error=%r" % (account.cooldown_until,
                                               account.last_error))
finally:
    P.urllib.request.urlopen = real_urlopen
    P.time.sleep = real_sleep

print()
print("=== 3. persistent TLS failure rotates without a long cooldown ===")
calls["n"] = 0
always_fail_calls = {"n": 0}


def always_fail(req, timeout=None):
    always_fail_calls["n"] += 1
    raise ssl.SSLError(1, "violation of protocol (_ssl.c:1082)")


account2 = FakeAccount()
P.POOL = FakePool(account2)
P.urllib.request.urlopen = always_fail
P.time.sleep = lambda s: None
try:
    try:
        P.open_upstream({"model": "hy4-preview",
                         "messages": [{"role": "user", "content": "hi"}]})
        check("raised after exhausting retries", False, "no exception")
    except Exception as exc:
        check("raised after exhausting retries", isinstance(exc, ssl.SSLError),
              type(exc).__name__)
    check("retried %d times" % always_fail_calls["n"],
          always_fail_calls["n"] == P.CHAT_NETWORK_RETRIES,
          "calls=%d" % always_fail_calls["n"])
    remaining = account2.cooldown_until - time.time()
    check("cooldown is short (<=5s), not 60s", remaining <= 5.0,
          "%.1fs" % remaining)
    check("error recorded as a network fault",
          account2.last_error.startswith("network:"), account2.last_error)
finally:
    P.urllib.request.urlopen = real_urlopen
    P.time.sleep = real_sleep

print()
print("=== 4. dashboard stops polling when the session is rejected ===")
html = open(os.path.join(HERE, "dashboard.html"), encoding="utf-8").read()
check("panelNeedsLogin clears the timers",
      "panelStopPolling();" in html.split("function panelNeedsLogin")[1][:900],
      "call not found inside panelNeedsLogin")
check("panelStopPolling clears PANEL_TIMERS",
      "PANEL_TIMERS.forEach(t => clearInterval(t));"
      in html.split("function panelStopPolling")[1][:700])
check("panelStopPolling drops the token",
      "PANEL_TOKEN = '';" in html.split("function panelStopPolling")[1][:700])
check("panelStopPolling stops the log poller",
      "clearInterval(logPollTimer)" in html.split("function panelStopPolling")[1][:700])
check("getJSON still routes 401 to the gate",
      "panelNeedsLogin(); throw new Error('unauthorized')" in html)

print()
if failures:
    print("RESULT: %d FAILURE(S)" % len(failures))
    for name in failures:
        print("  - %s" % name)
    sys.exit(1)
print("RESULT: ALL FIX CHECKS PASSED")
