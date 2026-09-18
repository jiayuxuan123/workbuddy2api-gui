"""Verify auto routing over real HTTP: any usable account serves shared models.

The behaviour under test is the reason auto mode exists - a shared model such
as deepseek-v4.1-flash can be served by a domestic OR an international
account, so an idle account on either side still gets used.

Rather than calling the upstream, this starts a gateway whose "accounts" are
stubs that report which one handled the request. That isolates routing from
network behaviour.
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCRATCH = os.path.realpath(tempfile.mkdtemp(prefix="wbauto_"))
os.environ["WB_DATA_DIR"] = SCRATCH

failures = []


def check(name, ok, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


import wb_proxy                                          # noqa: E402


class StubPool(object):
    """A pool that hands back a named account and records the order used."""

    def __init__(self, accounts):
        self._accounts = accounts
        self.picked = []
        self._i = 0
        self.affinity = type("A", (), {"unbind": staticmethod(lambda k: None)})()

    def count_ready(self, realm=None):
        return len([a for a in self._accounts
                    if not realm or a.realm == realm])

    def count_usable(self, realm=None):
        return self.count_ready(realm)

    def get(self, uid):
        for a in self._accounts:
            if a.uid == uid:
                return a
        return None

    def pick_for_session(self, realm=None, session_key=None, exclude=None):
        exclude = exclude or set()
        candidates = [a for a in self._accounts
                      if (not realm or a.realm == realm)
                      and a.uid not in exclude
                      and a.ready()]
        if not candidates:
            return None
        # Round-robin so a shared model alternates between realms.
        account = candidates[self._i % len(candidates)]
        self._i += 1
        self.picked.append(account.realm)
        return account


class StubAccount(object):
    def __init__(self, uid, realm):
        self.uid = uid
        self.realm = realm
        self.access_token = "stub"
        self.refresh_token = ""
        self.expires_at = int(time.time()) + 86400
        self.cooldown_until = 0
        self.last_error = ""
        self.nickname = uid
        self.domain = ("www.workbuddy.ai" if realm == "intl"
                       else "copilot.tencent.com")
        self.credits = None
        self.enabled = True
        self.path = None
        self.added_at = time.time()
        self.source = "stub"

    def headers(self, purpose="chat"):
        return {}

    def ready(self):
        return self.enabled and self.cooldown_until <= time.time()

    def clear_error(self):
        pass

    def note_error(self, message, cooldown=60, single_account=False):
        pass

    def save(self, directory):
        return None


class StubResponse(object):
    """Minimal file-like SSE response carrying one chunk and a usage block."""

    def __init__(self, realm):
        payload = {
            "id": "stub-1",
            "model": "deepseek-v4.1-flash",
            "choices": [{"index": 0, "delta": {"content": "realm=%s" % realm},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                      "total_tokens": 7},
        }
        self._lines = [
            ("data: " + json.dumps(payload) + "\n").encode(),
            b"data: [DONE]\n",
        ]

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"".join(self._lines)


def main():
    accounts = [StubAccount("cn00000000000001", "cn"),
                StubAccount("intl000000000001", "intl")]
    pool = StubPool(accounts)

    # Capture which realm open_upstream resolved to, then serve a stub.
    resolved = []
    seen_requests = []

    def fake_urlopen(req, timeout=None):
        # Record the host so we can tell which exit was addressed.
        host = req.full_url.split("/")[2] if hasattr(req, "full_url") else "?"
        seen_requests.append(host)
        realm = "cn" if "codebuddy" in host or "copilot" in host else "intl"
        resolved.append(realm)
        return StubResponse(realm)

    original_urlopen = wb_proxy.urllib.request.urlopen
    original_pool = wb_proxy.POOL
    original_auto = wb_proxy.realm_auto()
    wb_proxy.urllib.request.urlopen = fake_urlopen
    wb_proxy.POOL = pool

    try:
        print("=== auto mode: a shared model is served by either realm ===")
        wb_proxy.set_realm_auto(True)
        wb_proxy.CURRENT_REALM = "auto"
        for i in range(4):
            resp, account = wb_proxy.open_upstream({
                "model": "deepseek-v4.1-flash",
                "messages": [{"role": "user", "content": "hi"}],
            })
            list(resp)          # drain so the request completes
        check("4 requests used both realms",
              set(pool.picked[-4:]) == {"intl", "cn"},
              str(pool.picked[-4:]))
        check("the exit matched the account used",
              len(resolved) == 4 and set(resolved) == {"intl", "cn"},
              str(resolved))

        print()
        print("=== pinned mode: a shared model stays on one realm ===")
        wb_proxy.set_realm_auto(False)
        wb_proxy.CURRENT_REALM = "cn"
        pool.picked.clear()
        resolved.clear()
        for i in range(3):
            resp, account = wb_proxy.open_upstream({
                "model": "deepseek-v4.1-flash",
                "messages": [{"role": "user", "content": "hi"}],
            })
            list(resp)
        check("all three used the domestic realm",
              set(pool.picked) == {"cn"}, str(pool.picked))
        check("all three addressed the domestic host",
              set(resolved) == {"cn"}, str(resolved))

        print()
        print("=== an exclusive model ignores auto and pins itself ===")
        wb_proxy.set_realm_auto(True)
        wb_proxy.CURRENT_REALM = "auto"
        pool.picked.clear()
        resolved.clear()
        resp, account = wb_proxy.open_upstream({
            "model": "gpt-5.6-sol",
            "messages": [{"role": "user", "content": "hi"}],
        })
        list(resp)
        check("gpt-5.6-sol went to the international realm",
              resolved == ["intl"], str(resolved))
        check("and used the international account",
              pool.picked == ["intl"], str(pool.picked))

        pool.picked.clear()
        resolved.clear()
        resp, account = wb_proxy.open_upstream({
            "model": "minimax-m3",
            "messages": [{"role": "user", "content": "hi"}],
        })
        list(resp)
        check("minimax-m3 went to the domestic realm",
              resolved == ["cn"], str(resolved))
        check("and used the domestic account",
              pool.picked == ["cn"], str(pool.picked))

        print()
        print("=== auto skips a realm whose only account is unusable ===")
        # Cool the domestic account down: auto must fall back to intl.
        accounts[0].cooldown_until = time.time() + 3600
        wb_proxy.CURRENT_REALM = "auto"
        pool.picked.clear()
        resolved.clear()
        pool._i = 0
        for i in range(2):
            resp, account = wb_proxy.open_upstream({
                "model": "deepseek-v4.1-flash",
                "messages": [{"role": "user", "content": "hi"}],
            })
            list(resp)
        check("every request used the only usable realm",
              set(pool.picked) == {"intl"}, str(pool.picked))
        accounts[0].cooldown_until = 0

        print()
        print("=== an explicit realm overrides auto ===")
        wb_proxy.set_realm_auto(True)
        pool.picked.clear()
        resolved.clear()
        resp, account = wb_proxy.open_upstream(
            {"model": "deepseek-v4.1-flash",
             "messages": [{"role": "user", "content": "hi"}]},
            target_realm="cn")
        list(resp)
        check("target_realm=cn forced the domestic exit",
              resolved == ["cn"], str(resolved))

    finally:
        wb_proxy.urllib.request.urlopen = original_urlopen
        wb_proxy.POOL = original_pool
        wb_proxy.set_realm_auto(original_auto)
        shutil.rmtree(SCRATCH, ignore_errors=True)

    print()
    if failures:
        print("RESULT: %d FAILURE(S)" % len(failures))
        for name in failures:
            print("  - %s" % name)
        return 1
    print("RESULT: ALL AUTO-ROUTING CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
