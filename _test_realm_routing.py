"""Verify realm routing is internally consistent.

Background: the domestic model list was written out twice - once inside
detect_model_realm() and once as the CN_EXCLUSIVE set. The two drifted, so
glm-5v-turbo was classified as domestic for the cross-realm check but routed to
the international exit, which the upstream rejects with an opaque 403.

These checks pin the invariant: the two views must agree for every model either
list mentions.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import wb_proxy as P

failures = []


def check(name, ok, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


print("=== detect_model_realm agrees with the exclusive sets ===")
mismatched = []
for model in sorted(P.CN_EXCLUSIVE):
    actual = P.detect_model_realm(model)
    if actual != "cn":
        mismatched.append((model, actual))
check("every CN_EXCLUSIVE model routes to cn", not mismatched, str(mismatched))

mismatched = []
for model in sorted(P.INTL_EXCLUSIVE):
    actual = P.detect_model_realm(model)
    if actual != "intl":
        mismatched.append((model, actual))
check("every INTL_EXCLUSIVE model routes to intl", not mismatched,
      str(mismatched))

print()
print("=== exclusive_realm agrees with detect_model_realm ===")
disagreements = []
for model in sorted(P.CN_EXCLUSIVE | P.INTL_EXCLUSIVE):
    owner = P.exclusive_realm(model)
    routed = P.detect_model_realm(model)
    if owner and owner != routed:
        disagreements.append((model, owner, routed))
check("no model is owned by one realm but routed to the other",
      not disagreements, str(disagreements))

print()
print("=== prefix rules are honoured ===")
for model in ("gpt-5.6-sol", "gpt-6-astra", "gemini-3.5-flash",
              "gpt-anything-new"):
    check("%s -> intl by prefix/set" % model,
          P.detect_model_realm(model) == "intl",
          P.detect_model_realm(model))
for model in ("minimax-m3", "minimax-anything-new", "deepseek-v4-pro"):
    check("%s -> cn by prefix/set" % model,
          P.detect_model_realm(model) == "cn",
          P.detect_model_realm(model))

print()
print("=== shared models fall back to the active realm ===")
original = P.CURRENT_REALM
try:
    for realm in ("intl", "cn"):
        P.CURRENT_REALM = realm
        for model in ("deepseek-v4.1-flash", "glm-5.3", "hy3", "kimi-k3"):
            actual = P.detect_model_realm(model)
            check("%s with active=%s -> %s" % (model, realm, realm),
                  actual == realm, actual)
finally:
    P.CURRENT_REALM = original

print()
print("=== an empty model falls back to the active realm ===")
P.CURRENT_REALM = "cn"
check("empty string -> cn", P.detect_model_realm("") == "cn")
check("None -> cn", P.detect_model_realm(None) == "cn")
P.CURRENT_REALM = original

print()
print("=== the two sets do not overlap ===")
overlap = P.CN_EXCLUSIVE & P.INTL_EXCLUSIVE
check("no model is claimed by both realms", not overlap, str(overlap))

print()
print("=== every prefix entry is lowercase-safe ===")
# detect_model_realm lowercases the input, so a set entry with capitals would
# never match. Guard against that being introduced later.
upper = [m for m in (P.CN_EXCLUSIVE | P.INTL_EXCLUSIVE)
         if m != m.lower()]
check("all set entries are lowercase", not upper, str(upper))

print()
if failures:
    print("RESULT: %d FAILURE(S)" % len(failures))
    for name in failures:
        print("  - %s" % name)
    sys.exit(1)
print("RESULT: ALL REALM ROUTING CHECKS PASSED")
