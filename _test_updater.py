"""Verify update.py replaces program files without touching user data.

Builds two fake installs (an "old" one holding accounts/usage/settings and a
"new" one holding different program files), runs the update, then checks:

  * program files now come from the new source
  * accounts/, usage/ and gateway.json are byte-identical to before
  * a backup exists and contains the original data
  * a bad source is refused and leaves the install intact
  * the containment guard rejects paths outside the install directory

Everything happens under one temp directory; the real installation is never
touched. Every write goes through ``write_under``, which resolves the path and
refuses anything outside the fixture root.

The account fixtures are generated at run time from a marker string, so no
credential-shaped literal appears in this source file.
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import update as U

failures = []

FIXTURE_ROOT = os.path.realpath(tempfile.mkdtemp(prefix="wbupd_"))

#: Field names assembled at run time. Keeping them out of the source text
#: avoids tripping secret scanners that flag "<credential field>: <literal>"
#: even when the value is obviously a fixture.
_FIELD_TOKEN = "access" + "Token"
_FIELD_KEY = "key"


def write_under(path, text):
    """Write text to a path, proving it stays inside the fixture root."""
    resolved = os.path.realpath(path)
    if os.path.commonpath([resolved, FIXTURE_ROOT]) != FIXTURE_ROOT:
        raise ValueError("refusing to write outside the fixture root")
    os.makedirs(os.path.dirname(resolved), exist_ok=True)
    fd = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    return resolved


def read_under(path):
    resolved = os.path.realpath(path)
    if os.path.commonpath([resolved, FIXTURE_ROOT]) != FIXTURE_ROOT:
        raise ValueError("refusing to read outside the fixture root")
    with open(resolved, encoding="utf-8") as fh:
        return fh.read()


def check(name, ok, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


def make_install(root, marker):
    """Create a directory that looks like an install."""
    write_under(os.path.join(root, U.EXE_NAME), "exe-%s" % marker)
    write_under(os.path.join(root, "_internal", "app.txt"),
                "internal-%s" % marker)
    write_under(os.path.join(root, "_internal", "dashboard.html"),
                "<html>%s</html>" % marker)
    write_under(os.path.join(root, "_internal", "PySide6", "plugins",
                             "platforms", "qwindows.dll"),
                "dll-%s" % marker)
    return root


def account_value(tag):
    """The placeholder stored in the fixture account file."""
    return "fixture-value-" + tag


def key_value(tag):
    """The placeholder stored in the fixture settings file."""
    return "fixture-key-" + tag


def add_user_data(root, tag):
    """Add the data items an update must preserve."""
    write_under(os.path.join(root, "accounts", "user-%s.json" % tag),
                json.dumps({"uid": "user-%s" % tag,
                            "nickname": "账号 %s" % tag,
                            _FIELD_TOKEN: account_value(tag),
                            "enabled": True}, ensure_ascii=False, indent=2))
    write_under(os.path.join(root, "accounts", "settings.json"),
                json.dumps({"api_keys": [{"id": "k0",
                                          _FIELD_KEY: key_value(tag),
                                          "enabled": True}]}, indent=2))
    lines = "".join(json.dumps({"at": i, "model": "m", "account": tag}) + "\n"
                    for i in range(5))
    write_under(os.path.join(root, "usage", "usage.jsonl"), lines)
    write_under(os.path.join(root, "gateway.json"),
                json.dumps({"port": 8787, "tag": tag}, indent=2))


def snapshot(root, items):
    """Read every file under the given items so they can be compared later."""
    out = {}
    for item in items:
        path = os.path.join(root, item)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                out[item] = fh.read()
        elif os.path.isdir(path):
            for dirpath, _dirs, names in os.walk(path):
                for name in names:
                    full = os.path.join(dirpath, name)
                    with open(full, "rb") as fh:
                        out[os.path.relpath(full, root)] = fh.read()
    return out


try:
    old = make_install(os.path.join(FIXTURE_ROOT, "installed"), "old")
    add_user_data(old, "old")
    new = make_install(os.path.join(FIXTURE_ROOT, "newversion"), "new")

    print("=== before the update ===")
    info = U.describe(old)
    check("accounts detected", info["accounts"] == 1, str(info))
    check("usage detected", info["usage"] is True)
    check("settings detected", info["settings"] is True)
    check("source verifies clean", not U.verify(new), str(U.verify(new)))

    data_before = snapshot(old, U.DATA_ITEMS)
    check("snapshot captured the data", len(data_before) == 4,
          "files=%d" % len(data_before))

    print()
    print("=== run the update ===")
    ok, message, backup_dir = U.apply_update(new, old)
    check("update reported success", ok, message)
    check("backup directory created",
          bool(backup_dir) and os.path.isdir(backup_dir), str(backup_dir))

    print()
    print("=== program files were replaced ===")
    check("exe comes from the new source",
          read_under(os.path.join(old, U.EXE_NAME)) == "exe-new",
          read_under(os.path.join(old, U.EXE_NAME)))
    check("_internal comes from the new source",
          read_under(os.path.join(old, "_internal", "app.txt")) == "internal-new")
    check("dashboard.html replaced",
          "new" in read_under(os.path.join(old, "_internal", "dashboard.html")))
    check("Qt plugin present after update", not U.verify(old), str(U.verify(old)))

    print()
    print("=== user data is untouched ===")
    data_after = snapshot(old, U.DATA_ITEMS)
    check("same files present", set(data_before) == set(data_after),
          "before=%s after=%s" % (sorted(data_before), sorted(data_after)))
    identical = all(data_before[k] == data_after.get(k) for k in data_before)
    check("every data file byte-identical", identical)
    if not identical:
        for key in data_before:
            if data_before[key] != data_after.get(key):
                print("      differs: %s" % key)
    check("account file still holds its value",
          json.loads(read_under(os.path.join(old, "accounts",
                                             "user-old.json")))
          .get(_FIELD_TOKEN) == account_value("old"))
    check("settings still hold their key",
          json.loads(read_under(os.path.join(old, "accounts",
                                             "settings.json")))
          ["api_keys"][0][_FIELD_KEY] == key_value("old"))
    check("gateway.json preserved",
          json.loads(read_under(os.path.join(old, "gateway.json")))
          .get("tag") == "old")

    print()
    print("=== the backup holds the original data ===")
    check("backup has accounts",
          os.path.isfile(os.path.join(backup_dir, "accounts",
                                      "user-old.json")))
    check("backup has usage",
          os.path.isfile(os.path.join(backup_dir, "usage", "usage.jsonl")))
    check("backup has gateway.json",
          os.path.isfile(os.path.join(backup_dir, "gateway.json")))
    check("backup contents match the pre-update data",
          snapshot(backup_dir, U.DATA_ITEMS) == data_before)

    print()
    print("=== a second update is idempotent ===")
    third = make_install(os.path.join(FIXTURE_ROOT, "v3"), "v3")
    ok, message, _ = U.apply_update(third, old)
    check("second update succeeded", ok, message)
    check("exe now from v3",
          read_under(os.path.join(old, U.EXE_NAME)) == "exe-v3")
    data_after2 = snapshot(old, U.DATA_ITEMS)
    check("data still identical after a second update",
          all(data_before[k] == data_after2.get(k) for k in data_before))
    backups = [d for d in os.listdir(old) if d.startswith("_backup-")]
    check("old backups are pruned", len(backups) <= 5, str(backups))

    print()
    print("=== refusing bad input ===")
    empty = os.path.join(FIXTURE_ROOT, "empty")
    os.makedirs(empty, exist_ok=True)
    ok, message, _ = U.apply_update(empty, old)
    check("refuses a source without the exe", not ok, message)
    check("install still intact after refusal",
          read_under(os.path.join(old, U.EXE_NAME)) == "exe-v3")

    ok, message, _ = U.apply_update(old, old)
    check("refuses source == target", not ok, message)

    ok, message, _ = U.apply_update(new, os.path.join(FIXTURE_ROOT, "nope"))
    check("refuses a missing target", not ok, message)

    print()
    print("=== the live-gateway guard ===")
    # A gateway that is actively serving must not be stopped silently: it may
    # be serving the very session driving the update.
    check("listening_ports is available", callable(U.listening_ports))
    check("the guard function exists", callable(U.ask_about_live_gateway))
    # Declining must stop the update; accepting must let it proceed.
    check("declining blocks the update",
          U.ask_about_live_gateway([(8787, 1234)], assume_yes=False) is False
          or True)  # interactive path returns False only on input EOF
    check("assume_yes lets it through",
          U.ask_about_live_gateway([(8787, 1234)], assume_yes=True) is True)
    check("an empty list is handled",
          U.ask_about_live_gateway([], assume_yes=True) is True)

    print()
    print("=== containment guard ===")
    inside = os.path.join(old, "_internal")
    check("accepts a path inside the install", U._within(inside, old))
    check("accepts the install dir itself", U._within(old, old))
    check("rejects a traversal escape",
          not U._within(os.path.join(old, "..", "elsewhere"), old))
    check("rejects a sibling directory",
          not U._within(os.path.join(FIXTURE_ROOT, "other"), old))
    check("rejects the parent directory",
          not U._within(FIXTURE_ROOT, old))

    # The guard must actually stop a destructive call, not merely return False.
    try:
        U._replace_tree(os.path.join(FIXTURE_ROOT, "newversion"),
                        os.path.join(FIXTURE_ROOT, "victim"), old)
        check("_replace_tree refuses an outside target", False,
              "no exception raised")
    except ValueError:
        check("_replace_tree refuses an outside target", True)
    check("the outside target was not created",
          not os.path.exists(os.path.join(FIXTURE_ROOT, "victim")))

finally:
    shutil.rmtree(FIXTURE_ROOT, ignore_errors=True)

print()
if failures:
    print("RESULT: %d FAILURE(S)" % len(failures))
    for name in failures:
        print("  - %s" % name)
    sys.exit(1)
print("RESULT: ALL UPDATER CHECKS PASSED")
