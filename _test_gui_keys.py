"""Verify the GUI key manager and panel-password controls.

These drive the real widgets against a scratch data directory, so the checks
cover what a user would click rather than the underlying helpers in isolation.

The password reset is the interesting one: it has to work without knowing the
current password, because that is exactly the situation it exists for.
"""

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCRATCH = os.path.realpath(tempfile.mkdtemp(prefix="wbkeys_"))
os.environ["WB_DATA_DIR"] = SCRATCH

failures = []


def check(name, ok, detail=""):
    print("  %-56s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    import wb_gui
    import wb_proxy
    import wb_settings
    import wb_ui_theme as theme
    app.setStyleSheet(theme.stylesheet())

    class Args(object):
        minimized = False
        start = False

    window = wb_gui.MainWindow(Args())
    app.processEvents()

    # Modal dialogs block until a human clicks them, which never happens in a
    # headless run. MainWindow routes every prompt through these five methods
    # precisely so they can be replaced here; patching QMessageBox itself does
    # not work because it is a Qt class.
    window.ask = lambda title, text: True          # always confirm
    window.info = lambda title, text: None
    window.warn = lambda title, text: None
    window.fail = lambda title, text: None
    window.ask_text = lambda title, prompt, default="": ("测试 Key", True)
    shown_keys = []
    window.show_new_key = shown_keys.append

    print("=== settings tab exposes the new controls ===")
    check("require_local_key checkbox exists",
          hasattr(window, "require_local_key_check"))
    check("key table exists", hasattr(window, "key_table"))
    check("panel password fields exist",
          hasattr(window, "new_pwd_edit") and hasattr(window, "new_pwd_confirm"))
    check("reset-password button exists", hasattr(window, "reset_pwd_button"))
    check("defaults to not requiring a local key",
          window.require_local_key_check.isChecked() is False)

    print()
    print("=== key table renders an empty state ===")
    window.refresh_keys()
    app.processEvents()
    check("no rows when nothing is configured",
          window.key_table.rowCount() == 0,
          "rows=%d" % window.key_table.rowCount())
    check("hint explains local mode needs no key",
          "不需要 Key" in window.key_hint.text(), window.key_hint.text()[:60])

    print()
    print("=== creating a key through the real button path ===")
    window.do_new_key()
    app.processEvents()
    check("key was created", window.key_table.rowCount() == 1,
          "rows=%d" % window.key_table.rowCount())
    check("creation popup was shown", len(shown_keys) == 1,
          "shown=%d" % len(shown_keys))
    created = shown_keys[0] if shown_keys else ""
    check("created key looks like a key",
          created.startswith("wb-") and len(created) > 20, created[:20])
    check("name from the dialog is used",
          window.key_table.item(0, 0).text() == "测试 Key",
          window.key_table.item(0, 0).text())
    check("created key authenticates", wb_proxy.identify_key(created) is not None)
    key = created

    print()
    print("=== local mode does NOT demand the key ===")
    window.require_local_key_check.setChecked(False)
    prefs = window._collect_prefs()
    check("pref collected", prefs.get("require_local_key") is False)
    window.gateway.prefs.update(prefs)
    window.gateway._prepare()
    check("auth not required on loopback", wb_proxy.auth_required() is False)
    check("but the key still authenticates",
          wb_proxy.identify_key(key) is not None)

    print()
    print("=== turning the switch on demands it ===")
    window.require_local_key_check.setChecked(True)
    window.gateway.prefs.update(window._collect_prefs())
    window.gateway._prepare()
    check("auth now required", wb_proxy.auth_required() is True)
    check("the key still works", wb_proxy.identify_key(key) is not None)
    check("a wrong key does not", wb_proxy.identify_key("nope") is None)

    print()
    print("=== toggling a key off ===")
    window.key_table.selectRow(0)
    window.do_toggle_key()
    app.processEvents()
    check("row now shows disabled",
          window.key_table.item(0, 2).text() == "已停用",
          window.key_table.item(0, 2).text())
    check("disabled key no longer authenticates",
          wb_proxy.identify_key(key) is None)
    # refresh_keys() rebuilds the table, so the selection is gone by now;
    # re-select before the next action.
    window.key_table.selectRow(0)
    window.do_toggle_key()
    app.processEvents()
    check("re-enabled", wb_proxy.identify_key(key) is not None)

    print()
    print("=== panel password: default state ===")
    window.refresh_panel_password_state()
    app.processEvents()
    check("reports default password",
          "admin" in window.panel_pwd_status.text(),
          window.panel_pwd_status.text())

    print()
    print("=== panel password: change it ===")
    window.new_pwd_edit.setText("my-secret-pass")
    window.new_pwd_confirm.setText("my-secret-pass")
    window.do_set_panel_password()
    app.processEvents()
    check("new password verifies",
          wb_settings.verify_panel_password(wb_proxy.ACCOUNTS_DIR,
                                            "my-secret-pass"))
    check("old default no longer works",
          not wb_settings.verify_panel_password(wb_proxy.ACCOUNTS_DIR, "admin"))
    check("status reports custom password",
          "自定义" in window.panel_pwd_status.text(),
          window.panel_pwd_status.text())
    check("input fields cleared after success",
          window.new_pwd_edit.text() == "" and window.new_pwd_confirm.text() == "")

    print()
    print("=== the forgotten-password recovery path ===")
    # This is the whole point: reset must work WITHOUT the current password.
    window.do_reset_panel_password()
    app.processEvents()
    check("admin works again after reset",
          wb_settings.verify_panel_password(wb_proxy.ACCOUNTS_DIR, "admin"))
    check("the forgotten password no longer works",
          not wb_settings.verify_panel_password(wb_proxy.ACCOUNTS_DIR,
                                                "my-secret-pass"))
    check("status back to default",
          "admin" in window.panel_pwd_status.text(),
          window.panel_pwd_status.text())

    print()
    print("=== deleting a key ===")
    window.key_table.selectRow(0)
    window.do_delete_key()
    app.processEvents()
    check("key no longer authenticates",
          wb_proxy.identify_key(key) is None)
    # The table may still show the launcher key if LAN mode generated one in
    # this session; what matters is that the configured key is gone.
    remaining = [window.key_table.item(r, 0).text()
                 for r in range(window.key_table.rowCount())]
    check("the deleted key is gone from the table",
          "测试 Key" not in remaining, str(remaining))
    check("only the launcher key may remain",
          all("局域网" in name for name in remaining), str(remaining))

    window._quitting = True
    window.close()
    app.processEvents()
    shutil.rmtree(SCRATCH, ignore_errors=True)

    print()
    if failures:
        print("RESULT: %d FAILURE(S)" % len(failures))
        for name in failures:
            print("  - %s" % name)
        return 1
    print("RESULT: ALL KEY-MANAGER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
