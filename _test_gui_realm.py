"""Verify realm switching works from the GUI and actually changes routing.

This is the fix for the reported symptom: a domestic account sat idle because
shared models (DeepSeek, GLM, Kimi) always took the default exit and the GUI
had no way to change it.

Checks:
  * the GUI exposes a realm selector with both realms
  * switching persists to active_realm.json
  * detect_model_realm routes shared models to the newly chosen realm
  * the selection survives a restart (the state file is read back)
  * /health reports the realm actually in use, not a fixed string
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCRATCH = os.path.realpath(tempfile.mkdtemp(prefix="wbrealm_"))
os.environ["WB_DATA_DIR"] = SCRATCH

failures = []


def check(name, ok, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    import wb_gui
    import wb_proxy
    import wb_ui_theme as theme
    app.setStyleSheet(theme.stylesheet())

    class Args(object):
        minimized = False
        start = False

    window = wb_gui.MainWindow(Args())
    app.processEvents()

    # Modals would block a headless run; MainWindow routes them through these.
    window.ask = lambda title, text: True
    window.info = lambda title, text: None
    window.warn = lambda title, text: None
    window.fail = lambda title, text: None

    print("=== the selector exists and offers both realms ===")
    check("realm selector present", hasattr(window, "realm_choice"))
    options = [window.realm_choice.itemData(i)
               for i in range(window.realm_choice.count())]
    check("auto plus both realms offered", options == ["auto", "intl", "cn"],
          str(options))

    # Prepare the proxy so REALM_STATE_FILE points into the scratch dir, then
    # exercise the routing helpers directly.
    window.gateway.prefs.update({"port": 0, "lan": False})
    window.gateway._prepare()
    wb_proxy.save_persisted_realm("auto")
    state_file = wb_proxy.REALM_STATE_FILE
    check("state file written", os.path.isfile(state_file), state_file)

    print()
    print("=== auto mode is the default: shared models take any exit ===")
    shared = ["deepseek-v4.1-flash", "glm-5.3", "kimi-k3", "hy3"]
    for model in shared:
        routed = wb_proxy.detect_model_realm(model)
        check("%s returns empty (auto: either realm)" % model,
              routed == "", repr(routed))

    print()
    print("=== realm-specific models are unaffected by auto ===")
    for model, expected in (("gpt-5.6-sol", "intl"),
                            ("gemini-3.5-flash", "intl"),
                            ("minimax-m3", "cn"),
                            ("deepseek-v4-pro", "cn")):
        routed = wb_proxy.detect_model_realm(model)
        check("%s still routes to %s" % (model, expected), routed == expected,
              routed)

    print()
    print("=== pinning to cn makes shared models follow it ===")
    index = window.realm_choice.findData("cn")
    window.realm_choice.setCurrentIndex(index)
    window.do_switch_realm()
    app.processEvents()

    check("CURRENT_REALM is now cn", wb_proxy.CURRENT_REALM == "cn",
          wb_proxy.CURRENT_REALM)
    for model in shared:
        routed = wb_proxy.detect_model_realm(model)
        check("%s routes to cn while pinned" % model, routed == "cn", routed)

    print()
    print("=== realm-specific models are unaffected ===")
    for model, expected in (("gpt-5.6-sol", "intl"),
                            ("gemini-3.5-flash", "intl"),
                            ("minimax-m3", "cn"),
                            ("deepseek-v4-pro", "cn")):
        routed = wb_proxy.detect_model_realm(model)
        check("%s still routes to %s" % (model, expected), routed == expected,
              routed)

    print()
    print("=== the choice persists to disk ===")
    saved = json.load(open(state_file, encoding="utf-8"))
    check("state file says cn", saved.get("realm") == "cn", str(saved))
    check("state file has a timestamp", bool(saved.get("updated_iso")),
          str(saved))

    print()
    print("=== and is read back on the next start ===")
    wb_proxy.CURRENT_REALM = "intl"          # pretend a fresh process
    restored = wb_proxy.load_persisted_realm()
    check("load_persisted_realm returns cn", restored == "cn", restored)
    check("CURRENT_REALM restored", wb_proxy.CURRENT_REALM == "cn",
          wb_proxy.CURRENT_REALM)

    print()
    print("=== switching back to auto ===")
    window.realm_choice.setCurrentIndex(window.realm_choice.findData("auto"))
    window.do_switch_realm()
    app.processEvents()
    check("CURRENT_REALM back to auto", wb_proxy.CURRENT_REALM == "auto",
          wb_proxy.CURRENT_REALM)
    check("state file updated",
          json.load(open(state_file, encoding="utf-8")).get("realm") == "auto")
    for model in shared:
        check("%s back to auto routing" % model,
              wb_proxy.detect_model_realm(model) == "",
              wb_proxy.detect_model_realm(model))

    print()
    print("=== the hint explains what each mode means ===")
    hint_auto = window._realm_hint("auto")
    check("auto hint says 自动模式", "自动模式" in hint_auto, hint_auto[:60])
    check("auto hint explains automatic allocation",
          "自动分配" in hint_auto or "任一侧" in hint_auto, hint_auto[:80])
    hint_intl = window._realm_hint("intl")
    check("intl hint says pinned", "固定" in hint_intl, hint_intl[:60])
    check("intl hint names the realm", "国际版" in hint_intl, hint_intl[:60])
    hint_cn = window._realm_hint("cn")
    check("cn hint names the realm", "国内版" in hint_cn, hint_cn[:60])

    print()
    print("=== /health and the selector report the real mode ===")
    import inspect
    source = inspect.getsource(wb_proxy.Handler.do_GET)
    check("health no longer hardcodes intl",
          '"realm": "intl"' not in source, "still hardcoded")
    check("health uses CURRENT_REALM", '"realm": CURRENT_REALM' in source)
    check("selector has all three modes",
          window.realm_choice.findData("auto") >= 0
          and window.realm_choice.findData("intl") >= 0
          and window.realm_choice.findData("cn") >= 0)

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
    print("RESULT: ALL REALM SWITCH CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
