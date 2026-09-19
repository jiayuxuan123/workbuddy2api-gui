"""Verify the web dashboard understands all three routing modes.

Background: the gateway gained an "auto" mode, but the dashboard still treated
the realm as a boolean - "is it intl?" - so auto fell into the else branch. The
page then reported a fixed realm (usually 国内版) no matter what the gateway was
actually doing, which is what made routing look unimplemented.

These checks read the served HTML and assert the properties that make the
three-way handling correct. They are static checks by design: the dashboard is
plain script with no build step, so the file itself is the artefact.
"""

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.join(HERE, "dashboard.html")

failures = []


def check(name, ok, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


def main():
    if not os.path.isfile(DASHBOARD):
        print("SKIP: dashboard.html not found")
        return 0

    src = io.open(DASHBOARD, encoding="utf-8").read()
    match = re.search(r"<script>(.*)</script>", src, re.S)
    if not match:
        print("FAIL: no <script> block")
        return 1
    js = match.group(1)

    print("=== the three modes are represented ===")
    for mode in ("intl", "cn", "auto"):
        check("%r appears as a mode" % mode,
              ('"%s"' % mode) in js, "not referenced")

    check("a shared label table exists", "REALM_LABELS" in js)
    check("it names auto", re.search(r'auto\s*:\s*"', js) is not None,
          "no auto entry")
    check("a label helper exists", "function realmLabel(" in js)
    check("auto has its own view tab", 'id="tabAuto"' in src)
    check("auto is reachable from the switch button",
          "NEXT_GATEWAY_REALM" in js)

    print()
    print("=== no realm value is still treated as a boolean ===")
    # The bug was an "is it intl?" test whose else-branch swallowed auto.
    suspicious = [
        (r'VIEW_REALM\s*===\s*"intl"\)\s*\?', "ternary on VIEW_REALM == intl"),
        (r'ACTIVE_GATEWAY_REALM\s*===\s*"intl"\)\s*\?',
         "ternary on ACTIVE_GATEWAY_REALM == intl"),
        (r'isIntl(Gw|View)\s*\)\s*\?', "isIntl flag used in a ternary"),
    ]
    for pattern, label in suspicious:
        hits = re.findall(pattern, js)
        check("cleared: %s" % label, not hits,
              "%d occurrence(s)" % len(hits))

    print()
    print("=== the switch cycles through all three ===")
    m = re.search(r'const next = \(([^;]+)\);', js)
    check("a next-mode expression exists", m is not None)
    if m:
        expr = m.group(1)
        check("it mentions auto", "auto" in expr, expr)
        check("it mentions intl", "intl" in expr, expr)
        check("it mentions cn", "cn" in expr, expr)

    print()
    print("=== adding an account always names a concrete realm ===")
    # A login cannot target "auto"; the page must fall back to a real realm
    # rather than letting auto reach the API.
    check("the login radio preselect is explicit",
          "preferCn" in js, "no explicit preselection")
    m2 = re.search(r'realm !== "intl" && realm !== "cn"', js)
    check("the login fallback validates the realm", m2 is not None,
          "fallback does not check the value")
    check("auto is not sent as a login realm",
          'realm: "auto"' not in js and "realm: window.VIEW_REALM" not in js)

    print()
    print("=== the page does not start pinned to one exit ===")
    m3 = re.search(r'window\.ACTIVE_GATEWAY_REALM = "(\w+)"', js)
    check("initial value is a known mode",
          m3 is not None and m3.group(1) in ("intl", "cn", "auto"),
          m3.group(1) if m3 else "not found")
    if m3:
        check("initial value is auto", m3.group(1) == "auto", m3.group(1))

    print()
    print("=== structural sanity ===")
    for open_ch, close_ch, label in (("{", "}", "braces"), ("(", ")", "parens"),
                                     ("[", "]", "brackets")):
        o, c = js.count(open_ch), js.count(close_ch)
        check("%s are balanced" % label, o == c, "%d vs %d" % (o, c))


main()

print()
if failures:
    print("RESULT: %d FAILURE(S)" % len(failures))
    for name in failures:
        print("  - %s" % name)
    sys.exit(1)
print("RESULT: ALL DASHBOARD REALM CHECKS PASSED")
