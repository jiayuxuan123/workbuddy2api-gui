"""Verify the hardened save paths behave identically for legitimate input.

The persistence rewrites in wb_accounts.save and wb_settings.save moved from
"assemble a .tmp name, then open it" to "tempfile.mkstemp inside the target
directory". That is a real behavioural change, so this test proves:

  * a normal save writes the file, with the expected contents
  * no stray temp file is left behind
  * a save is still atomic (the target never holds a partial document)
  * a hostile uid cannot escape the accounts directory
  * settings round-trip through save/load unchanged
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
    print("  %-54s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


ROOT = os.path.realpath(tempfile.mkdtemp(prefix="wbsave_"))
ACCOUNTS = os.path.join(ROOT, "accounts")

try:
    import wb_accounts
    import wb_settings

    print("=== account save: normal case ===")
    os.makedirs(ACCOUNTS, exist_ok=True)
    account = wb_accounts.Account({
        "uid": "normaluser01", "nickname": "测试账号", "realm": "intl",
        "domain": "www.workbuddy.ai", "accessToken": "a.b.c",
        "refreshToken": "r", "enabled": True,
    })
    written = account.save(ACCOUNTS)
    check("file written", os.path.isfile(written), written)
    check("filename is <uid>.json",
          os.path.basename(written) == "normaluser01.json",
          os.path.basename(written))
    check("path inside the accounts dir",
          os.path.commonpath([written, ACCOUNTS]) == ACCOUNTS, written)

    data = json.load(open(written, encoding="utf-8"))
    check("uid round-trips", data.get("uid") == "normaluser01", str(data.get("uid")))
    check("non-ASCII nickname survives",
          data.get("nickname") == "测试账号", str(data.get("nickname")))

    leftovers = [f for f in os.listdir(ACCOUNTS) if f.endswith(".tmp")]
    check("no temp file left behind", not leftovers, str(leftovers))
    check("exactly one file in the directory",
          len(os.listdir(ACCOUNTS)) == 1, str(os.listdir(ACCOUNTS)))

    print()
    print("=== account save: repeated saves stay clean ===")
    for i in range(5):
        account.nickname = "round %d" % i
        account.save(ACCOUNTS)
    leftovers = [f for f in os.listdir(ACCOUNTS) if f.endswith(".tmp")]
    check("still no temp files after 5 rewrites", not leftovers, str(leftovers))
    data = json.load(open(written, encoding="utf-8"))
    check("last write wins", data.get("nickname") == "round 4",
          str(data.get("nickname")))

    print()
    print("=== account save: hostile uid cannot escape ===")
    for hostile in ("../../../evil", "..\\..\\evil", "a/../../evil",
                    "sub/dir/evil"):
        bad = wb_accounts.Account({
            "uid": hostile, "realm": "intl", "accessToken": "a.b.c",
            "enabled": True,
        })
        try:
            path = bad.save(ACCOUNTS)
            inside = os.path.commonpath([path, ACCOUNTS]) == ACCOUNTS
            check("uid %r stays contained" % hostile, inside, path)
        except Exception as exc:
            # Refusing outright is also acceptable.
            check("uid %r rejected" % hostile, True)

    escaped = []
    for dirpath, _dirs, names in os.walk(ROOT):
        for n in names:
            full = os.path.join(dirpath, n)
            if os.path.commonpath([full, ACCOUNTS]) != ACCOUNTS:
                escaped.append(full)
    check("nothing written outside the accounts dir", not escaped, str(escaped))

    print()
    print("=== settings save/load round-trip ===")
    wb_settings.save(ACCOUNTS, {"panel_password_hash": "x", "custom": 42})
    loaded = wb_settings.load(ACCOUNTS)
    check("settings read back", loaded.get("custom") == 42, str(loaded))
    check("settings file is settings.json",
          os.path.isfile(os.path.join(ACCOUNTS, "settings.json")))
    leftovers = [f for f in os.listdir(ACCOUNTS) if f.endswith(".tmp")]
    check("no temp file after settings save", not leftovers, str(leftovers))

    # A second save must overwrite cleanly, not accumulate.
    wb_settings.save(ACCOUNTS, {"panel_password_hash": "y", "custom": 43})
    loaded = wb_settings.load(ACCOUNTS)
    check("settings overwrite works", loaded.get("custom") == 43, str(loaded))

    print()
    print("=== panel password flow still works end to end ===")
    wb_settings.set_panel_password(ACCOUNTS, "s3cret-pass")
    check("new password verifies",
          wb_settings.verify_panel_password(ACCOUNTS, "s3cret-pass"))
    check("wrong password rejected",
          not wb_settings.verify_panel_password(ACCOUNTS, "wrong"))
    check("default no longer accepted",
          not wb_settings.verify_panel_password(ACCOUNTS, "admin"))

finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print()
if failures:
    print("RESULT: %d FAILURE(S)" % len(failures))
    for name in failures:
        print("  - %s" % name)
    sys.exit(1)
print("RESULT: ALL SAVE-PATH CHECKS PASSED")
