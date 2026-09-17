"""wb_autostart.py —— 开机自启管理（纯标准库，供 GUI 调用）。

原先这件事由 ``install-autostart.bat`` 调 PowerShell 创建 Startup 快捷方式完成。
界面里再拉一个 powershell.exe 出来既慢又难回报错误，所以改为在程序内直接操作
注册表。

实现选择：``HKCU\\...\\CurrentVersion\\Run``，而不是 Startup 文件夹里的 .lnk。

* ``.lnk`` 是二进制格式，用标准库手写需要精确拼出 IDList、LinkInfo 等多个
  块；实测手工拼出的文件 Windows 读不回来（TargetPath 为空），说明格式细节
  不对，而这条路径不值得继续调。
* 注册表 ``Run`` 键是 Windows 自启的标准机制，``winreg`` 是标准库，写入与
  读取都是几十行以内的事。任务管理器的「启动」页能看到并随时禁用这一项，
  用户可以自己关掉，不会变成隐藏的常驻项。

范围限定：只写 **HKCU**（当前用户），绝不碰 HKLM，也不创建系统服务，
因此不需要管理员权限，也不影响其他用户。

只用标准库。
"""

import os
import sys

#: Name shown in Task Manager > Startup and in regedit.
RUN_VALUE_NAME = "WorkBuddy2API"

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _run_key_path():
    return _RUN_KEY


def _target_command():
    """The exact command line autostart should run.

    Returns (executable, arguments). Frozen builds launch the EXE directly.
    Running from source launches ``pythonw.exe`` so no console window flashes
    at login, with the GUI script as its argument.
    """
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable), []

    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    interpreter = None
    for name in ("pythonw.exe", "python.exe"):
        candidate = os.path.join(exe_dir, name)
        if os.path.isfile(candidate):
            interpreter = candidate
            break
    if interpreter is None:
        interpreter = os.path.abspath(sys.executable)

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wb_gui.py")
    return interpreter, [script]


def _quote(token):
    """Quote one command-line token for the registry value."""
    token = str(token)
    if token and not any(ch in token for ch in ' \t"'):
        return token
    return '"%s"' % token.replace('"', r'\"')


def build_command(port=None, minimized=False):
    """Assemble the command line stored in the Run value."""
    exe, args = _target_command()
    parts = [_quote(exe)]
    parts.extend(_quote(a) for a in args)
    if port:
        parts.append("--port %d" % int(port))
    if minimized:
        # Autostart should not steal focus at login: the GUI honours this by
        # starting hidden and restoring from the tray/notification area.
        parts.append("--minimized")
    return " ".join(parts)


def install(port=None, minimized=True):
    """Add or update the Run entry. Returns (ok, message)."""
    if os.name != "nt":
        return False, "开机自启仅在 Windows 上可用"
    try:
        import winreg
    except ImportError:
        return False, "当前 Python 缺少 winreg 模块"

    command = build_command(port=port, minimized=minimized)
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _run_key_path(),
                                0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, command)
    except PermissionError:
        return False, "没有权限写入注册表启动项"
    except Exception as exc:
        return False, "写入启动项失败: %s" % exc
    return True, "已开启开机自启"


def uninstall():
    """Remove the Run entry. Returns (ok, message)."""
    if os.name != "nt":
        return False, "开机自启仅在 Windows 上可用"
    try:
        import winreg
    except ImportError:
        return False, "当前 Python 缺少 winreg 模块"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _run_key_path(), 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        return True, "开机自启本来就是关闭的"
    except PermissionError:
        return False, "没有权限修改注册表启动项"
    except Exception as exc:
        return False, "删除启动项失败: %s" % exc
    return True, "已关闭开机自启"


def stored_command():
    """The command line currently registered, or "" when absent."""
    if os.name != "nt":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _run_key_path(), 0,
                            winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE_NAME)
        return str(value or "")
    except Exception:
        return ""


def is_enabled():
    """True when the Run entry exists."""
    return bool(stored_command())


def _first_path(command):
    """Extract the executable path from a stored command line."""
    command = (command or "").strip()
    if not command:
        return ""
    if command.startswith('"'):
        end = command.find('"', 1)
        if end > 1:
            return command[1:end]
    return command.split(" ")[0]


def status():
    """A small dict for the UI."""
    command = stored_command()
    target = _first_path(command)
    exe, _ = _target_command()
    return {
        "supported": os.name == "nt",
        "enabled": bool(command),
        "command": command,
        "target": target,
        "target_exists": bool(target) and os.path.isfile(target),
        "expected_target": exe,
        # A stale entry (program moved or rebuilt elsewhere) would start
        # nothing at login; the UI surfaces this instead of silently failing.
        "stale": bool(command) and bool(target) and not os.path.isfile(target),
        "location": r"HKCU\%s" % _run_key_path(),
    }
