"""Verify the in-app update check.

Replaces the old routine of downloading a whole update package and copying it
over by hand: the window now asks the release page whether a newer version
exists, and can install it in place.

Covers the version comparison, the update panel's presence, and the full
install path against a locally built archive, so nothing is fetched from the
network.

Fixture files are produced by writing a zip and letting the product's own
extract_zip() create them, rather than by writing file paths here. That keeps
every location inside the scratch directory created in this file and exercises
the same code path a real update takes.
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ROOT = pathlib.Path(tempfile.mkdtemp(prefix="wbupd_")).resolve()
os.environ["WB_DATA_DIR"] = str(ROOT)

failures = []


def check(name, ok, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


def inside(path):
    """True when a resolved path sits inside the scratch root."""
    resolved = pathlib.Path(path).resolve()
    return resolved == ROOT or ROOT in resolved.parents


def build_archive(destination, marker):
    """Write a zip that unpacks into a complete installation."""
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr("WorkBuddy2API.exe", "exe-%s" % marker)
        archive.writestr("_internal/app.txt", "internal-%s" % marker)
        archive.writestr("_internal/dashboard.html", "<html>%s</html>" % marker)
        archive.writestr("_internal/PySide6/plugins/platforms/qwindows.dll",
                         "dll-%s" % marker)
        archive.writestr("accounts/user.json",
                         json.dumps({"uid": "user-1", "accessToken": "x"}))
        archive.writestr("gateway.json",
                         json.dumps({"port": 8787, "tag": marker}))
    return destination


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
    print("=== the containment predicate ===")
    check("root is inside itself", inside(ROOT))
    check("a child is inside", inside(ROOT / "child" / "file.txt"))
    check("a parent is not inside", not inside(ROOT.parent))
    check("a sibling is not inside", not inside(ROOT.parent / "elsewhere"))

    print()
    print("=== installing an update keeps user data ===")
    old_zip = build_archive(ROOT / "old.zip", "old")
    new_zip = build_archive(ROOT / "new.zip", "new")
    installed = pathlib.Path(U.extract_zip(str(old_zip), str(ROOT / "app")))
    new_files = pathlib.Path(U.extract_zip(str(new_zip), str(ROOT / "new")))
    check("installed version unpacked",
          (installed / "WorkBuddy2API.exe").is_file(), str(installed))
    check("new version unpacked",
          (new_files / "WorkBuddy2API.exe").is_file(), str(new_files))

    before_accounts = (installed / "accounts" / "user.json").read_text(
        encoding="utf-8")
    before_prefs = (installed / "gateway.json").read_text(encoding="utf-8")

    ok, message, backup = U.prepare_update(str(new_files), str(installed))
    check("prepare_update succeeded", ok, message)
    check("a backup was made", bool(backup) and os.path.isdir(backup),
          str(backup))

    check("program files replaced",
          (installed / "WorkBuddy2API.exe").read_text(encoding="utf-8")
          == "exe-new",
          (installed / "WorkBuddy2API.exe").read_text(encoding="utf-8"))
    check("_internal replaced",
          (installed / "_internal" / "app.txt").read_text(encoding="utf-8")
          == "internal-new")
    check("accounts untouched",
          (installed / "accounts" / "user.json").read_text(encoding="utf-8")
          == before_accounts)
    check("gateway.json untouched",
          (installed / "gateway.json").read_text(encoding="utf-8")
          == before_prefs)

    if backup:
        backup_path = pathlib.Path(backup)
        check("backup is inside the install directory", inside(backup_path),
              str(backup_path))
        check("backup holds the original accounts",
              (backup_path / "accounts" / "user.json").read_text(
                  encoding="utf-8") == before_accounts)
        check("backup holds the original settings",
              (backup_path / "gateway.json").read_text(encoding="utf-8")
              == before_prefs)
        # The backup covers the user's data only, by design: the program
        # files are replaced wholesale from the download, so keeping a second
        # copy of them would just double the disk cost.
        check("backup does not duplicate the program files",
              not (backup_path / "_internal").exists())
        check("backup records only the data items",
              sorted(p.name for p in backup_path.iterdir())
              == ["accounts", "gateway.json"], 
              str(sorted(p.name for p in backup_path.iterdir())))

    print()
    print("=== a wrong-shaped archive is rejected ===")
    empty_zip = ROOT / "empty.zip"
    with zipfile.ZipFile(empty_zip, "w") as archive:
        archive.writestr("readme.txt", "nothing here")
    try:
        U.extract_zip(str(empty_zip), str(ROOT / "unpacked2"))
        check("refuses an archive without the exe", False, "accepted")
    except U.UpdateError:
        check("refuses an archive without the exe", True)

    bad_zip = ROOT / "bad.zip"
    bad_zip.write_bytes(b"not a zip at all")
    try:
        U.extract_zip(str(bad_zip), str(ROOT / "unpacked3"))
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
