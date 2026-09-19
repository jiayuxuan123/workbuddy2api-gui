"""Verify the GUI tasks panel exposes and drives the domestic-realm actions.

The web panel could sign in, run growth tasks and dispatch cat travel; the
desktop window could only start or stop the scheduler. These checks confirm the
window now offers those actions, that the account picker lists domestic
accounts only, and that each action reaches the right upstream call.

The upstream functions are stubbed so nothing is sent to the real service.
"""

import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCRATCH = os.path.realpath(tempfile.mkdtemp(prefix="wbtasks_"))
os.environ["WB_DATA_DIR"] = SCRATCH

failures = []


def check(name, ok, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


def pump(app, seconds=0.5):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    import wb_accounts
    import wb_gui
    import wb_proxy
    import wb_ui_theme as theme
    app.setStyleSheet(theme.stylesheet())

    class Args(object):
        minimized = False
        start = False

    window = wb_gui.MainWindow(Args())
    app.processEvents()

    # Modal dialogs block a headless run; the window routes them through these.
    window.ask = lambda title, text: True
    window.info = lambda title, text: None
    window.warn = lambda title, text: None
    window.fail = lambda title, text: None

    print("=== the tasks panel offers the three manual actions ===")
    for attr, label in (("checkin_button", "手动签到"),
                        ("growth_button", "执行成长任务"),
                        ("travel_button", "猫猫旅行"),
                        ("scheduler_toggle_button", "启用 / 暂停排程"),
                        ("run_tasks_button", "立即执行全部任务")):
        exists = hasattr(window, attr)
        text_ok = exists and window.__getattribute__(attr).text() == label
        check("%s button present and labelled" % label, text_ok,
              "missing" if not exists else window.__getattribute__(attr).text())

    print()
    print("=== an account picker exists ===")
    check("picker present", hasattr(window, "task_account"))
    check("refresh helper present", hasattr(window, "refresh_task_accounts"))

    print()
    print("=== the picker lists domestic accounts only ===")
    pool = wb_accounts.AccountPool(SCRATCH, log=lambda m: None)
    now = time.time()
    pool.accounts = [
        wb_accounts.Account({"uid": "cn-account-1", "nickname": "国内一号",
                             "realm": "cn", "accessToken": "a",
                             "enabled": True, "expiresAt": int(now + 86400)}),
        wb_accounts.Account({"uid": "intl-account1", "nickname": "国际一号",
                             "realm": "intl", "accessToken": "b",
                             "enabled": True, "expiresAt": int(now + 86400)}),
        wb_accounts.Account({"uid": "cn-account-2", "nickname": "国内二号",
                             "realm": "cn", "accessToken": "c",
                             "enabled": False, "expiresAt": int(now + 86400)}),
    ]
    window.gateway._prepare()
    wb_proxy.POOL = pool
    window.refresh_task_accounts()
    app.processEvents()

    labels = [window.task_account.itemText(i)
              for i in range(window.task_account.count())]
    check("an 'all domestic' entry is first",
          labels and "全部" in labels[0], str(labels))
    # Skip the first entry ("全部国内版账号"), which also contains 国内.
    account_labels = labels[1:]
    check("both domestic accounts listed",
          len(account_labels) == 2, str(labels))
    check("the international account is not listed",
          not any("国际" in x for x in labels), str(labels))
    check("a disabled account is still shown but marked",
          any("已停用" in x for x in labels), str(labels))
    check("default target is all",
          window._task_target_uid() == "all",
          str(window._task_target_uid()))

    print()
    print("=== selecting one account narrows the target ===")
    idx = window.task_account.findData("cn-account-1")
    check("the domestic account is selectable", idx >= 0)
    if idx >= 0:
        window.task_account.setCurrentIndex(idx)
        check("target becomes that uid",
              window._task_target_uid() == "cn-account-1",
              str(window._task_target_uid()))
    window.task_account.setCurrentIndex(0)

    print()
    print("=== each action calls the matching upstream function ===")
    calls = {"checkin": [], "growth": [], "travel": []}

    # Stub the module-level functions the handlers import.
    class FakeAccount(object):
        pass

    def fake_checkin():
        pass

    # checkin is a method on the account object; replace it on the instance.
    def account_checkin(self):
        calls["checkin"].append(self.uid)
        return {"ok": True, "msg": "签到成功"}

    wb_accounts.Account.checkin = account_checkin

    import wb_tasks
    original_growth = getattr(wb_tasks, "run_growth_tasks", None)
    original_travel = getattr(wb_tasks, "do_cat_travel", None)

    def fake_growth(account):
        calls["growth"].append(account.uid)
        return {"ok": True, "logs": ["接受任务 1 个", "领取奖励 10 积分"]}

    def fake_travel(account):
        calls["travel"].append(account.uid)
        return {"action": "depart", "msg": "猫猫已出发"}

    wb_tasks.run_growth_tasks = fake_growth
    wb_tasks.do_cat_travel = fake_travel

    try:
        window.do_checkin()
        pump(app, 1.2)
        check("checkin reached both domestic accounts",
              sorted(calls["checkin"]) == ["cn-account-1", "cn-account-2"],
              str(calls["checkin"]))

        window.do_run_growth()
        pump(app, 1.2)
        check("growth ran for enabled domestic accounts only",
              calls["growth"] == ["cn-account-1"], str(calls["growth"]))

        window.do_travel()
        pump(app, 1.2)
        check("travel ran for enabled domestic accounts only",
              calls["travel"] == ["cn-account-1"], str(calls["travel"]))

        print()
        print("=== targeting a single account ===")
        calls["checkin"].clear()
        idx = window.task_account.findData("cn-account-2")
        if idx >= 0:
            window.task_account.setCurrentIndex(idx)
            window.do_checkin()
            pump(app, 1.2)
            check("only the chosen account was signed in",
                  calls["checkin"] == ["cn-account-2"], str(calls["checkin"]))
    finally:
        if original_growth is not None:
            wb_tasks.run_growth_tasks = original_growth
        if original_travel is not None:
            wb_tasks.do_cat_travel = original_travel

    print()
    print("=== with no pool the actions refuse politely ===")
    wb_proxy.POOL = None
    for name, fn in (("checkin", window.do_checkin),
                     ("growth", window.do_run_growth),
                     ("travel", window.do_travel)):
        try:
            fn()
            pump(app, 0.2)
            check("%s handled a missing pool" % name, True)
        except Exception as exc:
            check("%s handled a missing pool" % name, False, str(exc))

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
    print("RESULT: ALL TASK PANEL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
