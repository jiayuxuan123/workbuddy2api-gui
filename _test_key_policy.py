"""Verify the API-key policy matrix.

The intended behaviour:

  * a key configured in the panel is always ACCEPTED, wherever the request
    comes from - it is a valid credential, not a mode flag
  * whether a key is DEMANDED is controlled separately:
      - loopback + require_local_key off  -> not demanded (a local client
        needs no configuration)
      - loopback + require_local_key on   -> demanded
      - LAN mode                          -> always demanded

Checks run against the real functions with the module globals set directly,
so no server is started and nothing is written to the real settings file.
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

failures = []


def check(name, ok, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


ROOT = os.path.realpath(tempfile.mkdtemp(prefix="wbkey_"))
ACCOUNTS = os.path.join(ROOT, "accounts")
os.makedirs(ACCOUNTS, exist_ok=True)

try:
    import wb_proxy as P
    import wb_settings

    # Point the module at the scratch directory and start with no keys.
    P.ACCOUNTS_DIR = ACCOUNTS
    P.API_KEY = None
    P.API_KEY_FILE_SET = False
    P.API_KEY_LOCAL_REQUIRED = False
    wb_settings.save(ACCOUNTS, {})

    print("=== no key configured, loopback, switch off ===")
    check("auth not required", P.auth_required() is False)
    check("identify_key finds nothing", P.identify_key("anything") is None)

    print()
    print("=== a panel key is ACCEPTED even when not demanded ===")
    PANEL_KEY = "panel-key-abc123"
    wb_settings.set_api_keys(ACCOUNTS, [
        {"id": "k0", "name": "test", "key": PANEL_KEY, "realm": "", "enabled": True},
    ])
    entry = P.identify_key(PANEL_KEY)
    check("configured key is recognised", entry is not None,
          "identify_key returned None")
    check("wrong key is still rejected", P.identify_key("nope") is None)
    check("auth still not DEMANDED on loopback",
          P.auth_required() is False)

    print()
    print("=== switching require_local_key on demands it ===")
    P.API_KEY_LOCAL_REQUIRED = True
    check("auth now required", P.auth_required() is True)
    check("the configured key still works", P.identify_key(PANEL_KEY) is not None)
    check("a wrong key does not", P.identify_key("nope") is None)

    print()
    print("=== a disabled key is neither accepted nor a reason to demand ===")
    wb_settings.set_api_keys(ACCOUNTS, [
        {"id": "k0", "name": "test", "key": PANEL_KEY, "realm": "", "enabled": False},
    ])
    check("disabled key not recognised", P.identify_key(PANEL_KEY) is None)
    check("no enabled key means nothing to demand",
          P.auth_required() is False)

    print()
    print("=== launcher key (LAN mode) is honoured ===")
    wb_settings.set_api_keys(ACCOUNTS, [])
    LAUNCHER = "launcher-key-xyz789"
    P.API_KEY = LAUNCHER
    P.API_KEY_LOCAL_REQUIRED = True
    check("launcher key accepted", P.identify_key(LAUNCHER) is not None)
    check("auth required with only a launcher key",
          P.auth_required() is True)
    P.API_KEY_LOCAL_REQUIRED = False
    check("switch off means launcher key not demanded",
          P.auth_required() is False)
    check("but the launcher key still authenticates",
          P.identify_key(LAUNCHER) is not None)

    print()
    print("=== auth_disabled wins over everything ===")
    wb_settings.set_auth_disabled(ACCOUNTS, True)
    P.API_KEY = LAUNCHER
    P.API_KEY_LOCAL_REQUIRED = True
    check("auth_required is False when explicitly disabled",
          P.auth_required() is False)
    wb_settings.set_auth_disabled(ACCOUNTS, False)

    print()
    print("=== gateway sets the flag from preferences ===")
    import wb_gateway
    check("DEFAULTS has require_local_key",
          "require_local_key" in wb_gateway.DEFAULTS,
          str(sorted(wb_gateway.DEFAULTS.keys())))
    check("it defaults to False",
          wb_gateway.DEFAULTS.get("require_local_key") is False)

finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print()
if failures:
    print("RESULT: %d FAILURE(S)" % len(failures))
    for name in failures:
        print("  - %s" % name)
    sys.exit(1)
print("RESULT: ALL KEY-POLICY CHECKS PASSED")
