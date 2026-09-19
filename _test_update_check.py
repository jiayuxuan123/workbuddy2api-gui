"""Verify the in-app update check.

Replaces the old routine of downloading a whole update package and copying it
over by hand: the window now asks the release page whether a newer version
exists, and can install it in place.

These checks cover the version comparison, the update panel's presence, and the
full install path against a locally built archive, so nothing is fetched from
the network.

Every fixture file is addressed by path components relative to a scratch root
rather than by an absolute path: _join() rejects an absolute component,
a parent reference, or anything resolving outside the root, so a test cannot be
pointed at a real file by accident.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ROOT = os.path.realpath(tempfile.mkdtemp(prefix="wbupd_"))
os.environ["WB_DATA_DIR"] = ROOT

failures = []


def check(name, ok, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


def _join(*names):
    """Join fixed names under the scratch root, refusing traversal.

    No caller supplies a path: the components are names, and the result is
    verified to sit inside ROOT. That keeps the fixture tree unreachable from
    outside the scratch directory even if a test is edited carelessly.
    """
    for name in names:
        text = str(name)
        if os.path.isabs(text) or ":" in text or ".." in text:
            raise ValueError("refusing an unsafe path component: %r" % text)
    candidate = os.path.realpath(os.path.join(ROOT, *[str(n) for n in names]))
    if os.path.commonpath([candidate, ROOT]) != ROOT:
        raise ValueError("path resolves outside the fixture root")
    return candidate


#: Fixture locations, resolved once. Every helper below closes over these, so
#: none of them accepts a path and none can be pointed elsewhere.
APP_DIR = _join("app")
NEW_DIR = _join("new")
ARCHIVE = _join("update.zip")
EMPTY_ZIP = _join("empty.zip")
BAD_ZIP = _join("bad.zip")
UNPACK_A = _join("unpacked")
UNPACK_B = _join("unpacked2")
UNPACK_C = _join("unpacked3")

#: Files inside the fixture installation that assertions read back.
APP_EXE = _join("app", "WorkBuddy2API.exe")
APP_INTERNAL_TXT = _join("app", "_internal", "app.txt")
APP_ACCOUNTS_JSON = _join("app", "accounts", "user.json")
APP_PREFS_JSON = _join("app", "gateway.json")


def _safe(path_name):
    """Confirm a resolved fixture location is inside the scratch root."""
    resolved = os.path.realpath(path_name)
    if os.path.commonpath([resolved, ROOT]) != ROOT:
        raise ValueError("refusing to touch a path outside the fixture root")
    return resolved


def _write_at(location, text):
    """Write to one of the fixed fixture locations."""
    target = _safe(location)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd = os.open(_safe(location), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    return target


def _read_at(location):
    """Read from one of the fixed fixture locations."""
    with open(_safe(location), encoding="utf-8") as fh:
        return fh.read()


def build_install(directory, marker):
    """Populate a fixture installation. `directory` is one of the constants."""
    _write_at(os.path.join(directory, "WorkBuddy2API.exe"), "exe-%s" % marker)
    _write_at(os.path.join(directory, "_internal", "app.txt"),
           "internal-%s" % marker)
    _write_at(os.path.join(directory, "_internal", "dashboard.html"),
           "<html>%s</html>" % marker)
    _write_at(os.path.join(directory, "_internal", "PySide6", "plugins",
                        "platforms", "qwindows.dll"), "dll-%s" % marker)
    _write_at(os.path.join(directory, "accounts", "user.json"),
           json.dumps({"uid": "user-1", "accessToken": "x"}))
    _write_at(os.path.join(directory, "gateway.json"),
           json.dumps({"port": 8787, "tag": marker}))
    return directory


def pump(app, seconds=0.5):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def main():
    import wb_update as U

    print("=== version comparison ===")
    for a, b, expected in (("1.6.1", "1.7.0", True),
                           ("v1.7.0", "1.6.1", False),
                           ("1.7.0", "1.7.0", False),
                           ("1.7.0", "1.7.1", True),
                           ("1.9.9", "1.10.0", True),
                           ("2.0.0", "1.99.99", False),
                           ("", "1.0.0", True)):
        newer = U._version_tuple(b) > U._version_tuple(a)
        check("%s vs %s -> newer=%s" % (a, b, expected), newer == expected,
              "got %s" % newer)

    print()
    print("=== the module refuses a non-allowlisted host ===")
    for url in ("https://evil.example.com/x",
                "http://github.com/x",
                "file:///etc/passwd"):
        try:
            U._checked(url)
            check("rejects %s" % url[:38], False, "accepted")
        except U.UpdateError:
            check("rejects %s" % url[:38], True)
    try:
        U._checked("https://github.com/jiayuxuan123/x")
        check("accepts a github url", True)
    except Exception as exc:
        check("accepts a github url", False, str(exc))

    print()
    print("=== the fixture helpers refuse to escape the scratch root ===")
    for bad in (("/etc/passwd",), ("..", "escape"), ("C:", "windows"),
                ("a", "../../b"), ("a", "..", "b")):
        try:
            _join(*bad)
            check("rejects %r" % (bad,), False, "accepted")
        except ValueError:
            check("rejects %r" % (bad,), True)
    try:
        ok_path = _join("nested", "file.txt")
        check("accepts a plain relative name", ok_path.startswith(ROOT), ok_path)
    except Exception as exc:
        check("accepts a plain relative name", False, str(exc))

    print()
    print("=== installing an update keeps user data ===")
    installed = build_install(APP_DIR, "old")
    new_version = build_install(NEW_DIR, "new")

    archive = ARCHIVE
    with zipfile.ZipFile(archive, "w") as zf:
        for base, _dirs, names in os.walk(new_version):
            for name in names:
                full = os.path.join(base, name)
                zf.write(full, os.path.relpath(full, new_version))

    files_dir = U.extract_zip(archive, UNPACK_A)
    check("extracted to the exe folder",
          os.path.isfile(os.path.join(files_dir, "WorkBuddy2API.exe")),
          files_dir)

    before_accounts = _read_at(APP_ACCOUNTS_JSON)
    before_prefs = _read_at(APP_PREFS_JSON)

    ok, message, backup = U.prepare_update(files_dir, installed)
    check("prepare_update succeeded", ok, message)
    check("a backup was made", bool(backup) and os.path.isdir(backup),
          str(backup))

    check("program files replaced", _read_at(APP_EXE) == "exe-new", _read_at(APP_EXE))
    check("_internal replaced", _read_at(APP_INTERNAL_TXT) == "internal-new")
    check("accounts untouched", _read_at(APP_ACCOUNTS_JSON) == before_accounts)
    check("gateway.json untouched", _read_at(APP_PREFS_JSON) == before_prefs)
    if backup:
        backup_name = os.path.basename(backup)
        check("backup holds the original data",
              _read_at(_join("app", backup_name, "accounts", "user.json"))
              == before_accounts)

    print()
    print("=== a wrong-shaped archive is rejected ===")
    empty_zip = EMPTY_ZIP
    with zipfile.ZipFile(empty_zip, "w") as zf:
        zf.writestr("readme.txt", "nothing here")
    try:
        U.extract_zip(empty_zip, UNPACK_B)
        check("refuses an archive without the exe", False, "accepted")
    except U.UpdateError:
        check("refuses an archive without the exe", True)

    bad_zip = BAD_ZIP
    fd = os.open(bad_zip, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, b"not a zip at all")
    finally:
        os.close(fd)
    try:
        U.extract_zip(bad_zip, UNPACK_C)
        check("refuses a corrupt archive", False, "accepted")
    except U.UpdateError:
        check("refuses a corrupt archive", True)

    print()
    print("=== the settings panel exposes the update controls ===")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    import wb_gui
    import wb_ui_theme as theme
    app.setStyleSheet(theme.stylesheet())

    class Args(object):
        minimized = False
        start = False

    window = wb_gui.MainWindow(Args())
    app.processEvents()
    window.ask = lambda title, text: True
    window.info = lambda title, text: None
    window.warn = lambda title, text: None
    window.fail = lambda title, text: None

    for attr in ("check_update_button", "install_update_button",
                 "update_status", "update_progress", "update_page_button"):
        check("%s exists" % attr, hasattr(window, attr))
    check("install is disabled until a check finds something",
          not window.install_update_button.isEnabled())
    check("the version is shown", "1." in window.version_label.text(),
          window.version_label.text())

    print()
    print("=== the panel reacts to each check outcome ===")
    import wb_update
    original = wb_update.check_for_update
    try:
        wb_update.check_for_update = lambda timeout=None: {
            "ok": True, "current": "1.7.0", "latest": "1.7.0",
            "has_update": False, "notes": "", "error": ""}
        window.do_check_update()
        pump(app, 1.5)
        check("status says up to date", "最新" in window.update_status.text(),
              window.update_status.text())
        check("install stays disabled",
              not window.install_update_button.isEnabled())

        wb_update.check_for_update = lambda timeout=None: {
            "ok": True, "current": "1.7.0", "latest": "9.9.9",
            "has_update": True, "notes": "", "error": "",
            "assets": ["https://github.com/x/y/releases/download/v9.9.9/z.zip"]}
        window.do_check_update()
        pump(app, 1.5)
        check("status names the new version",
              "9.9.9" in window.update_status.text(),
              window.update_status.text())
        check("install is enabled", window.install_update_button.isEnabled())

        wb_update.check_for_update = lambda timeout=None: {
            "ok": False, "current": "1.7.0", "latest": "", "has_update": False,
            "notes": "", "error": "网络不可达"}
        window.do_check_update()
        pump(app, 1.5)
        check("the failure is shown",
              "网络不可达" in window.update_status.text(),
              window.update_status.text())
        check("install disabled after a failure",
              not window.install_update_button.isEnabled())
    finally:
        wb_update.check_for_update = original

    print()
    print("=== installing without a prior check is refused ===")
    window._update_result = None
    window.do_install_update()
    pump(app, 0.4)
    check("refused politely", True)

    window._quitting = True
    window.close()
    app.processEvents()


try:
    main()
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print()
if failures:
    print("RESULT: %d FAILURE(S)" % len(failures))
    for name in failures:
        print("  - %s" % name)
    sys.exit(1)
print("RESULT: ALL UPDATE-CHECK CHECKS PASSED")
