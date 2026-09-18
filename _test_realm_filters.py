"""Regression tests for realm filters that silently matched nothing.

Background: "auto" was introduced as a routing mode, but several viewers kept
passing CURRENT_REALM straight into an equality filter. "auto" is not a realm,
so those filters matched zero rows and the dashboard reported a pool of zero
accounts and all-zero usage while /health correctly showed two accounts.

These checks pin the behaviour that was broken:

  * a filter of "auto" (or None/"") means every realm
  * a filter of "intl"/"cn" still narrows to that side
  * get_realm_config refuses a mode name rather than silently returning the
    international config, which is how a domestic login became international

Every write goes through ``write_under``, which resolves the path and refuses
anything outside the fixture root.
"""

import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCRATCH = os.path.realpath(tempfile.mkdtemp(prefix="wbrf_"))
os.environ["WB_DATA_DIR"] = SCRATCH

failures = []


def check(name, ok, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


def write_under(path, text):
    """Write text to a path, proving it stays inside the fixture root."""
    resolved = os.path.realpath(path)
    if os.path.commonpath([resolved, SCRATCH]) != SCRATCH:
        raise ValueError("refusing to write outside the fixture root")
    os.makedirs(os.path.dirname(resolved), exist_ok=True)
    fd = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    return resolved


import wb_accounts                                       # noqa: E402
import wb_proxy                                          # noqa: E402


def seed_usage():
    """Write a small usage log: five rows per realm."""
    lines = []
    for realm in ("intl", "cn"):
        for i in range(5):
            lines.append(json.dumps({
                "at": time.time(), "iso": "2026-09-18T10:00:00",
                "model": "shared-model", "realm": realm,
                "account": "uid-%s" % realm, "stream": True,
                "prompt_tokens": 8, "completion_tokens": 2,
                "total_tokens": 10,
            }))
    write_under(wb_proxy.USAGE_LOG, "\n".join(lines) + "\n")


def main():
    print("=== realm_filter normalisation ===")
    for value, expected in (("intl", "intl"), ("cn", "cn"),
                            ("auto", ""), (None, ""), ("", ""),
                            ("bogus", ""), (0, "")):
        actual = wb_proxy.realm_filter(value)
        check("realm_filter(%r) -> %r" % (value, expected), actual == expected,
              repr(actual))

    print()
    print("=== usage totals: auto sees every realm ===")
    seed_usage()
    index = wb_proxy.usage_index()
    totals_intl = index.totals("intl")
    totals_cn = index.totals("cn")
    totals_auto = index.totals("auto")
    totals_none = index.totals(None)

    check("intl totals are the intl rows", totals_intl["requests"] == 5,
          str(totals_intl["requests"]))
    check("cn totals are the cn rows", totals_cn["requests"] == 5,
          str(totals_cn["requests"]))
    check("auto totals cover both", totals_auto["requests"] == 10,
          str(totals_auto["requests"]))
    check("None totals cover both", totals_none["requests"] == 10,
          str(totals_none["requests"]))
    check("auto equals the sum of the parts",
          totals_auto["requests"] == totals_intl["requests"]
          + totals_cn["requests"])
    check("auto token total is not zero", totals_auto["total_tokens"] == 100,
          str(totals_auto["total_tokens"]))

    print()
    print("=== by_model and recent honour auto the same way ===")
    check("by_model('auto') lists the model",
          "shared-model" in index.by_model("auto"),
          str(list(index.by_model("auto"))))
    count_auto, rows_auto = index.recent(limit=100, realm="auto")
    check("recent('auto') returns every row", count_auto == 10,
          str(count_auto))
    check("recent('intl') narrows",
          index.recent(limit=100, realm="intl")[0] == 5,
          str(index.recent(limit=100, realm="intl")[0]))

    print()
    print("=== account listing: auto means the whole pool ===")
    pool = wb_accounts.AccountPool(SCRATCH, log=lambda m: None)
    pool.accounts = [
        wb_accounts.Account({"uid": "intl-1", "realm": "intl",
                             "accessToken": "a", "enabled": True}),
        wb_accounts.Account({"uid": "cn-1", "realm": "cn",
                             "accessToken": "b", "enabled": True}),
    ]
    wb_proxy.POOL = pool

    check("listing all returns both",
          len(pool.list_public(realm=wb_proxy.realm_filter("auto"))) == 2,
          str(len(pool.list_public(realm=wb_proxy.realm_filter("auto")))))
    check("listing intl returns one",
          len(pool.list_public(realm=wb_proxy.realm_filter("intl"))) == 1)
    check("listing cn returns one",
          len(pool.list_public(realm=wb_proxy.realm_filter("cn"))) == 1)

    print()
    print("=== get_realm_config refuses a mode name ===")
    for realm in ("intl", "cn"):
        try:
            cfg = wb_accounts.get_realm_config(realm)
            check("%s resolves" % realm, bool(cfg.get("domain")), str(cfg))
        except Exception as exc:
            check("%s resolves" % realm, False, str(exc))
    for bogus in ("auto", "", None, "bogus"):
        try:
            wb_accounts.get_realm_config(bogus)
            check("get_realm_config(%r) raises" % bogus, False,
                  "returned silently")
        except ValueError:
            check("get_realm_config(%r) raises" % bogus, True)

    print()
    print("=== catalog selection: auto falls back to the broader list ===")
    original = wb_proxy.CURRENT_REALM
    try:
        wb_proxy.CURRENT_REALM = "auto"
        entries = wb_proxy.merge_catalog([])
        check("merge_catalog works with auto active", bool(entries),
              "returned %d entries" % len(entries))

        wb_proxy.CURRENT_REALM = "cn"
        cn_entries = wb_proxy.merge_catalog([])
        check("merge_catalog works with cn active", bool(cn_entries),
              "returned %d entries" % len(cn_entries))

        wb_proxy.CURRENT_REALM = "intl"
        intl_entries = wb_proxy.merge_catalog([])
        check("merge_catalog works with intl active", bool(intl_entries),
              "returned %d entries" % len(intl_entries))
        check("a catalogue is produced in every mode",
              bool(entries) and bool(cn_entries) and bool(intl_entries))
    finally:
        wb_proxy.CURRENT_REALM = original


try:
    main()
finally:
    shutil.rmtree(SCRATCH, ignore_errors=True)

print()
if failures:
    print("RESULT: %d FAILURE(S)" % len(failures))
    for name in failures:
        print("  - %s" % name)
    sys.exit(1)
print("RESULT: ALL REALM FILTER CHECKS PASSED")
