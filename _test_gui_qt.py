"""Headless functional test for the Qt GUI.

Instantiates the real MainWindow and drives it the way a user would, without
showing anything: change settings, start the service, add an account, refresh
every tab, toggle autostart, restart, stop.

This is the test that matters for the threading fix. The earlier tkinter build
passed a similar suite while being broken, because it never exercised a worker
result arriving back on the UI thread. Here the login dialog is driven through
its real async path, so a cross-thread delivery failure would show up.

The only network call is a loopback health probe of the gateway this test just
started, against a literal 127.0.0.1 address.
"""

import os
import shutil
import socket
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCRATCH = os.path.realpath(tempfile.mkdtemp(prefix="wbqt_"))
os.environ["WB_DATA_DIR"] = SCRATCH

failures = []


def check(name, ok, detail=""):
    print("  %-50s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def probe_health(port, timeout=3.0):
    """GET /health from loopback, by hand, with no URL construction."""
    if not isinstance(port, int) or not (1 <= port <= 65535):
        return None
    request = ("GET /health HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
               "Accept: application/json\r\nConnection: close\r\n\r\n"
               % port).encode("ascii")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(("127.0.0.1", port))
        sock.sendall(request)
        chunks = []
        while True:
            block = sock.recv(4096)
            if not block:
                break
            chunks.append(block)
    except OSError:
        return None
    finally:
        sock.close()
    raw = b"".join(chunks)
    split = raw.find(b"\r\n\r\n")
    if split == -1:
        return None
    import json
    try:
        return json.loads(raw[split + 4:].decode("utf-8", "replace"))
    except Exception:
        return None


def pump(app, seconds=0.4):
    """Spin the Qt event loop so queued signals and timers are delivered."""
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except Exception as exc:
        print("SKIP: PySide6 unavailable (%s)" % exc)
        return 0

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    import wb_gateway
    import wb_gui
    import wb_proxy
    import wb_ui_theme as theme
    app.setStyleSheet(theme.stylesheet())

    port = free_port()

    class Args(object):
        minimized = False
        start = False

    window = wb_gui.MainWindow(Args())
    pump(app, 0.3)

    print("=== construction ===")
    check("window built", window is not None)
    check("six tabs", window.tabs.count() == 6, str(window.tabs.count()))
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    check("tab order 概览/账号/用量/任务/日志/设置",
          titles == ["概览", "账号", "用量", "任务", "日志", "设置"], str(titles))
    check("start enabled / stop disabled",
          window.start_button.isEnabled() and not window.stop_button.isEnabled())
    check("data dir is the scratch dir",
          os.path.realpath(wb_gui.wb_runtime.data_dir()) == SCRATCH,
          wb_gui.wb_runtime.data_dir())

    print()
    print("=== settings round-trip ===")
    window.port_spin.setValue(port)
    window.lan_check.setChecked(False)
    window.auto_start_check.setChecked(False)
    prefs = window._collect_prefs()
    check("port read from widget", prefs["port"] == port, str(prefs.get("port")))
    check("host derives from lan flag", prefs["host"] == "127.0.0.1",
          prefs.get("host"))
    window.do_save_settings()
    pump(app, 0.3)
    saved = wb_gateway.load_prefs()
    check("port persisted", saved["port"] == port, str(saved.get("port")))

    print()
    print("=== start service ===")
    window.do_start(silent=True)
    pump(app, 2.5)
    check("gateway reports running", window.gateway.is_running())
    check("no start error", not window.gateway.status().get("error"),
          window.gateway.status().get("error", ""))
    health = probe_health(port)
    check("HTTP /health answers", bool(health and health.get("ok")), str(health))

    print()
    print("=== refresh cycles ===")
    for label, fn in (("refresh_status", window.refresh_status),
                      ("refresh_accounts", window.refresh_accounts),
                      ("refresh_usage", window.refresh_usage),
                      ("refresh_logs", lambda: window.refresh_logs(True))):
        try:
            fn()
            pump(app, 0.2)
            check("%s() ok" % label, True)
        except Exception as exc:
            check("%s() ok" % label, False, "%s: %s" % (type(exc).__name__, exc))

    print()
    print("=== overview reflects running state ===")
    window.refresh_status()
    pump(app, 0.2)
    check("state label shows running", "运行中" in window.state_label.text(),
          window.state_label.text())
    check("stop button enabled", window.stop_button.isEnabled())
    check("base URL shown", "/v1" in window.access["url"].text(),
          window.access["url"].text())
    check("access key field populated",
          bool(window.access["key"].text()), "empty")

    print()
    print("=== account table ===")
    pool = wb_proxy.POOL
    check("pool attached", pool is not None)
    if pool is not None:
        from wb_accounts import Account
        pool.add(Account({
            "uid": "qtsmoke0001", "nickname": "qt", "realm": "cn",
            "domain": "copilot.tencent.com", "accessToken": "a.b.c",
            "refreshToken": "", "expiresAt": int(time.time()) + 86400,
            "enabled": True,
        }))
        window.refresh_accounts()
        pump(app, 0.2)
        check("one row rendered", window.account_table.rowCount() == 1,
              "rows=%d" % window.account_table.rowCount())
        if window.account_table.rowCount():
            check("realm in Chinese",
                  window.account_table.item(0, 1).text() == "国内版")
            check("state shows 可用",
                  window.account_table.item(0, 2).text() == "可用")
            check("uid stored on the row",
                  window.account_table.item(0, 0).data(
                      wb_gui.Qt.UserRole) == "qtsmoke0001")

        window.realm_filter.setCurrentIndex(1)   # 国际版
        pump(app, 0.2)
        check("intl filter hides the cn row",
              window.account_table.rowCount() == 0)
        window.realm_filter.setCurrentIndex(0)   # 全部
        window.refresh_accounts()
        pump(app, 0.2)

    print()
    print("=== usage tab ===")
    window.refresh_usage()
    pump(app, 0.2)
    check("usage counters present",
          all(w.text() for w in window.usage_values.values()))

    print()
    print("=== autostart toggle (registry) ===")
    was = wb_gui.wb_autostart.is_enabled()
    window.autostart_check.setChecked(True)
    pump(app, 0.3)
    check("autostart on", wb_gui.wb_autostart.is_enabled())
    window.autostart_check.setChecked(False)
    pump(app, 0.3)
    check("autostart off", not wb_gui.wb_autostart.is_enabled())
    if was:
        wb_gui.wb_autostart.install()
        print("  (restored the pre-existing autostart entry)")

    print()
    print("=== ASYNC RESULT DELIVERY (the tkinter failure mode) ===")
    # This is the check that the previous build could not pass: a worker
    # result has to arrive back on the UI thread and mutate widgets.
    delivered = []

    def work():
        time.sleep(0.15)
        return "from-worker"

    window.run_async(work, lambda value: delivered.append(value))
    pump(app, 1.5)
    check("worker result delivered to UI thread", delivered == ["from-worker"],
          str(delivered))

    errors = []
    window.run_async(lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                     on_error=errors.append)
    pump(app, 1.5)
    check("worker failure delivered", errors and "boom" in errors[0], str(errors))

    print()
    print("=== login dialog drives its async path ===")
    class FakePool(object):
        def __init__(self):
            self.started = []
        def start_login(self, realm="intl", platform="CLI"):
            self.started.append(realm)
            return {"state": "s-%s" % realm,
                    "authUrl": "https://%s/login?state=s-%s" % (
                        "copilot.tencent.com" if realm == "cn"
                        else "www.workbuddy.ai", realm),
                    "realm": realm, "platform": platform}
        def poll_login(self, state):
            return {"status": "pending", "message": "11217:login ing..."}
        def cancel_login(self, state):
            return True

    fake = FakePool()

    class Owner(object):
        realm_config = staticmethod(wb_gui.MainWindow.realm_config)
        run_async = staticmethod(window.run_async)
        def on_account_added(self, account):
            delivered.append(("added", account))

    dialog = wb_gui.LoginDialog(Owner(), fake, parent=None)
    dialog.show()
    pump(app, 2.0)
    check("dialog asked the pool for a login", len(fake.started) >= 1,
          str(fake.started))
    check("URL rendered in the dialog",
          "login" in dialog.link.toPlainText(), dialog.link.toPlainText()[:60])
    check("open button enabled", dialog.open_button.isEnabled())
    check("polling has started (status set)", bool(dialog.status.text()),
          dialog.status.text())
    dialog.close()
    pump(app, 0.3)

    print()
    print("=== logs tab ===")
    window.refresh_logs(True)
    pump(app, 0.3)
    text = window.log_view.toPlainText().strip()
    check("log text populated", len(text) > 0, "empty")
    window.log_search.setText("gateway")
    window.refresh_logs(True)
    pump(app, 0.2)
    check("search filter runs", True)
    window.log_search.setText("")
    window.refresh_logs(True)

    print()
    print("=== restart then stop ===")
    window.do_restart()
    pump(app, 3.0)
    check("running after restart", window.gateway.is_running())
    window.gateway.stop()
    pump(app, 1.0)
    check("stopped cleanly", not window.gateway.is_running())
    check("port released", probe_health(port, timeout=1.5) is None)

    print()
    print("=== prefs inside the data dir ===")
    check("prefs confined",
          os.path.realpath(wb_gateway.prefs_path()).startswith(SCRATCH),
          wb_gateway.prefs_path())

    window._quitting = True
    window.close()
    pump(app, 0.3)
    shutil.rmtree(SCRATCH, ignore_errors=True)

    print()
    if failures:
        print("RESULT: %d FAILURE(S)" % len(failures))
        for name in failures:
            print("  - %s" % name)
        return 1
    print("RESULT: ALL QT GUI CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
