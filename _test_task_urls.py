"""Verify the task-code and URL validation added to wb_tasks.py.

Two guarantees:
  * a task code that could alter the request path is rejected
  * every URL the module builds stays on a known HTTPS upstream host

Both are exercised directly, without touching the network.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import wb_tasks

failures = []


def check(name, ok, detail=""):
    print("  %-56s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


print("=== task codes that must be rejected ===")
bad_codes = [
    "../../../etc/passwd",
    "..%2f..%2fadmin",
    "code/../../other",
    "code@evil.example.com",
    "code?x=1",
    "code#frag",
    "code with spaces",
    "code\nInjected: 1",
    "",
    None,
    "x" * 200,
]
for code in bad_codes:
    result = wb_tasks._safe_task_code(code)
    check("reject %r" % (code if code is None or len(str(code)) < 30
                         else str(code)[:26] + "..."),
          result == "", "got %r" % result)

print()
print("=== legitimate task codes must pass through unchanged ===")
for code in ("daily_checkin", "growth-task-01", "TASK_2026", "abc123"):
    result = wb_tasks._safe_task_code(code)
    check("accept %r" % code, result == code, "got %r" % result)

print()
print("=== URL builder enforces scheme and host ===")
ok_url = wb_tasks._checked_url("https://copilot.tencent.com",
                              "/v2/activity/growth/tasks")
check("valid upstream URL accepted",
      ok_url == "https://copilot.tencent.com/v2/activity/growth/tasks", ok_url)

for label, base in (
        ("plain http", "http://copilot.tencent.com"),
        ("internal address", "https://127.0.0.1"),
        ("cloud metadata", "https://169.254.169.254"),
        ("unknown host", "https://evil.example.com"),
        ("private range", "https://192.168.1.1"),
        ("file scheme", "file:///etc/passwd"),
        ("empty", "")):
    try:
        wb_tasks._checked_url(base, "/v2/report")
        check("reject %s" % label, False, "accepted %r" % base)
    except Exception:
        check("reject %s" % label, True)

print()
print("=== every allowed host is HTTPS and on the list ===")
for host in wb_tasks.ALLOWED_HOSTS:
    url = wb_tasks._checked_url("https://" + host, "/health")
    check("%s accepted" % host, url.startswith("https://" + host), url)

print()
print("=== the real call sites build valid URLs ===")
# These are the paths claim_task and report_events use; they must pass the
# checker rather than raise.
try:
    u1 = wb_tasks._checked_url(
        wb_tasks.CHAT_BASE, "/activity/growth/tasks/%s/claim" % "daily_checkin")
    check("claim URL builds", "daily_checkin" in u1, u1)
except Exception as exc:
    check("claim URL builds", False, str(exc))

try:
    u2 = wb_tasks._checked_url(wb_tasks.BILL_BASE, "/v2/report")
    check("report URL builds", u2 == "https://www.codebuddy.cn/v2/report", u2)
except Exception as exc:
    check("report URL builds", False, str(exc))

# A hostile code must never reach the URL builder at all.
hostile = wb_tasks._safe_task_code("../../admin")
check("hostile code never reaches the URL", hostile == "", repr(hostile))

print()
if failures:
    print("RESULT: %d FAILURE(S)" % len(failures))
    for name in failures:
        print("  - %s" % name)
    sys.exit(1)
print("RESULT: ALL VALIDATION CHECKS PASSED")
