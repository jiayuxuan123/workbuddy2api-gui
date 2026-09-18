"""Verify that every GUI preference survives a save/load round trip.

Background: save_prefs() builds its payload from DEFAULTS and then overlays
whatever the caller passes. That means a key the GUI writes but DEFAULTS does
not list is silently discarded. "autostart_start" was in exactly that
position - the checkbox existed, the value was collected, and the key was then
dropped on the way to disk, so boot-time autostart never began serving and the
operator still had to open the window and press Start.

These checks pin the contract: anything the settings screen can change must
come back unchanged after a restart.
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCRATCH = os.path.realpath(tempfile.mkdtemp(prefix="wbprefs_"))
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


import wb_gateway                                       # noqa: E402


def main():
    print("=== every GUI-switchable key is present in DEFAULTS ===")
    # The set the settings screen writes via _collect_prefs(). If a key is
    # added there but not here, it will not persist.
    gui_keys = [
        "port", "lan", "host",
        "require_local_key",
        "start_minimized", "autostart_start",
        "open_dashboard_on_start",
        "user_agent", "system_prompt",
        "autostart", "minimize_to_tray",
    ]
    missing = [k for k in gui_keys if k not in wb_gateway.DEFAULTS]
    check("no GUI key is missing from DEFAULTS", not missing,
          "missing: %s" % missing)
    print("       keys checked: %d" % len(gui_keys))

    print()
    print("=== autostart_start specifically ===")
    check("autostart_start is in DEFAULTS",
          "autostart_start" in wb_gateway.DEFAULTS)
    check("it defaults to off",
          wb_gateway.DEFAULTS["autostart_start"] is False,
          repr(wb_gateway.DEFAULTS.get("autostart_start")))

    print()
    print("=== a full round trip preserves every value ===")
    wanted = {
        "port": 8811,
        "lan": False,
        "host": "127.0.0.1",
        "require_local_key": True,
        "start_minimized": True,
        "autostart_start": True,
        "open_dashboard_on_start": False,
        "user_agent": "TestAgent/1.0",
        "system_prompt": "You are a test.",
    }
    written = wb_gateway.save_prefs(wanted)
    raw = json.load(open(written, encoding="utf-8"))
    for key, value in wanted.items():
        check("%s persisted as %r" % (key, value), raw.get(key) == value,
              "got %r" % raw.get(key))

    loaded = wb_gateway.load_prefs()
    for key, value in wanted.items():
        check("%s read back as %r" % (key, value), loaded.get(key) == value,
              "got %r" % loaded.get(key))

    print()
    print("=== autostart_start survives in both directions ===")
    wb_gateway.save_prefs({"autostart_start": True})
    check("on stays on", wb_gateway.load_prefs()["autostart_start"] is True,
          str(wb_gateway.load_prefs().get("autostart_start")))
    wb_gateway.save_prefs({"autostart_start": False})
    check("off stays off",
          wb_gateway.load_prefs()["autostart_start"] is False,
          str(wb_gateway.load_prefs().get("autostart_start")))

    print()
    print("=== a partial save keeps the defaults for everything else ===")
    wb_gateway.save_prefs({"port": 9000})
    reloaded = wb_gateway.load_prefs()
    check("port updated", reloaded["port"] == 9000, str(reloaded.get("port")))
    for key, default in wb_gateway.DEFAULTS.items():
        if key == "port":
            continue
        check("%s falls back to its default" % key,
              reloaded.get(key) == default,
              "got %r want %r" % (reloaded.get(key), default))

    print()
    print("=== a corrupt file does not break loading ===")
    write_under(wb_gateway.prefs_path(), "{not valid json")
    loaded = wb_gateway.load_prefs()
    check("falls back to defaults", loaded["port"] == 8788,
          str(loaded.get("port")))
    check("autostart_start still present",
          "autostart_start" in loaded)
    check("port is an int", isinstance(loaded["port"], int))

    print()
    print("=== an out-of-range port is clamped ===")
    write_under(wb_gateway.prefs_path(),
                json.dumps({"port": 999999, "autostart_start": True}))
    loaded = wb_gateway.load_prefs()
    check("port clamped into range", 1 <= loaded["port"] <= 65535,
          str(loaded.get("port")))


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
print("RESULT: ALL PREFERENCE CHECKS PASSED")
