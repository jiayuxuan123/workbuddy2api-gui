"""installer.py —— WorkBuddy2API 的 Windows 安装向导（标准安装器）。

为什么是自己写的而不是 Inno Setup
--------------------------------
Inno Setup / NSIS 都没有装在这台机器上，而安装器要能被任何人重新构建，
不能依赖"某台机器上碰巧有的编译器"。这里只用 Python 标准库实现一个
标准形态的安装向导，任何人 `python installer.py` 就能复现。

向导流程与常见的 Windows 安装器一致：

    欢迎 → [选择已有安装] → 选择安装位置 → 安装选项 → 正在安装 → 完成

中括号那一页只在**检测到多处已有安装**时出现。检测覆盖三个来源：注册表
里的 ``InstallLocation``、两个默认安装目录、以及正在运行的实例的镜像
路径。只有一处时直接把它预填为目标（并在页面上说明原因），一处都没有
时走全新安装 —— 也就是说，双击安装包的默认行为是"更新已有的那一份"，
而不是在别处又装一份。

安装选项里可以勾选 **创建桌面快捷方式**、**创建开始菜单快捷方式**，
以及装完是否立刻启动。安装位置支持"仅为我安装"（默认，写入
``%LOCALAPPDATA%\\Programs``，不需要管理员）与"为所有用户安装"
（写入 ``Program Files``，会通过 UAC 提权）。

三种运行方式
------------
* ``installer.py``               —— 图形向导（默认）
* ``installer.py --silent ...``   —— 静默安装，供测试与自动化使用
* ``installer.py --uninstall``    —— 卸载（卸载程序由同一个源构建）

安全性
------
安装器**永远不会**结束正在运行的 WorkBuddy2API 进程。检测到实例在跑时
只提示并让用户自己处理 —— 这个程序自己就可能是用户当前正在用的网关，
强行关掉会直接断掉他的对话。

只用标准库：``ctypes`` 走 COM 创建快捷方式，不依赖 pywin32。
"""

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

APP_NAME = "WorkBuddy2API"
APP_DISPLAY = "WorkBuddy2API"
PUBLISHER = "WorkBuddy2API"
APP_EXE = "WorkBuddy2API.exe"
UNINST_EXE = "uninstall.exe"

#: 版本号由打包脚本注入，源码直跑时用这个兜底值。
DEFAULT_VERSION = "1.8.3"

#: 卸载信息在注册表中的位置（Add/Remove Programs 读的就是这里）。
UNINSTALL_KEY = ("Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\"
                 + APP_NAME)

#: 会在安装目录旁生成、卸载时可选删除的数据目录。
DATA_DIRNAME = APP_NAME


class InstallError(Exception):
    """A problem worth showing to the user as-is."""


# ==========================================================================
# 路径与权限
# ==========================================================================

def default_dir(all_users):
    """The conventional install directory for the chosen mode."""
    if all_users:
        base = os.environ.get("ProgramFiles") or r"C:\Program Files"
        return os.path.join(base, APP_NAME)
    base = (os.environ.get("LOCALAPPDATA")
            or os.path.join(os.path.expanduser("~"), "AppData", "Local"))
    return os.path.join(base, "Programs", APP_NAME)


def is_admin():
    """True when the current process can write to Program Files."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def appdata_data_dir():
    """Where wb_runtime falls back to for accounts/usage when the install
    directory is not writable (which is the case under Program Files)."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, DATA_DIRNAME)


def dir_writable(path):
    """Probe whether ``path`` can be written, creating it if needed.

    The check is done by actually writing a file: an ACL denial and a
    read-only parent both surface the same way, and that is exactly the
    question being asked.
    """
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".wb_write_probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


# ==========================================================================
# 进程检测
# ==========================================================================

#: CreateFileW arguments used by the in-use probe.
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
#: ERROR_SHARING_VIOLATION: someone else holds the file open.
_ERROR_SHARING_VIOLATION = 32
#: ERROR_ACCESS_DENIED: ACL/read-only, which is not the same as "running".
_ERROR_ACCESS_DENIED = 5

#: INVALID_HANDLE_VALUE is (HANDLE)-1, i.e. all bits set for the pointer size.
_INVALID_HANDLE = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1


def file_in_use(path):
    """True when a file is being held open by any process.

    The question that actually matters before an install or uninstall is
    "are *these* files open", not "is a process with this name alive
    somewhere". Matching on the image name alone is too coarse: a user can
    legitimately have a portable copy running from another folder, and
    refusing to uninstall this one because of that is wrong.

    ``os.open`` cannot answer this on Windows - Python opens with generous
    sharing flags, so a second open succeeds even while the file is busy.
    ``CreateFileW`` with a share mode of 0 asks the kernel for exclusive
    access, which fails with ERROR_SHARING_VIOLATION whenever any other
    handle exists. That is the actual test.
    """
    if not os.path.exists(path):
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        create = kernel32.CreateFileW
        create.restype = ctypes.c_void_p
        create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                           ctypes.c_uint32, ctypes.c_void_p,
                           ctypes.c_uint32, ctypes.c_uint32,
                           ctypes.c_void_p]
        handle = create(path, _GENERIC_READ | _GENERIC_WRITE, 0, None,
                        _OPEN_EXISTING, 0, None)
        if not handle or handle == _INVALID_HANDLE:
            return kernel32.GetLastError() == _ERROR_SHARING_VIOLATION
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    except Exception:
        return False


def install_dir_in_use(install_dir):
    """True when the program in ``install_dir`` is currently running."""
    return file_in_use(os.path.join(install_dir, APP_EXE))


def _run_quiet(cmd, timeout=20):
    """Run a console tool and return its stdout as text.

    Two Windows realities are handled here: the tool writes GBK on a Chinese
    system (so a strict UTF-8 decode raises), and ``capture_output`` leaves
    ``stdout`` as None when the call fails. Both would otherwise turn a
    diagnostic helper into a crash.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=0x08000000)   # CREATE_NO_WINDOW
        raw = proc.stdout or b""
    except Exception:
        return ""
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def running_instances(install_dir=None):
    """PIDs of running WorkBuddy2API processes.

    Read through ``tasklist`` rather than a psutil-style API so no
    dependency is needed. Never used to terminate anything - callers only
    warn, because killing the process would cut off whoever is using it.
    """
    pids = []
    out = _run_quiet(
        ["tasklist", "/FI", "IMAGENAME eq " + APP_EXE, "/FO", "CSV", "/NH"], 15)
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if parts and parts[0].strip('"').lower() == APP_EXE.lower():
            for part in parts[1:2]:
                if part.isdigit():
                    pids.append(int(part))
                    break
    return pids


def gateway_listening(ports=(8787, 8788)):
    """Ports in ``ports`` that are currently listening.

    An instance on 8787 is very likely the one the user is actively using,
    so the installer surfaces it instead of silently replacing files.
    """
    listening = []
    out = _run_quiet(["netstat", "-ano", "-p", "TCP"], 20)
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0].upper() != "TCP":
            continue
        if fields[3].upper() != "LISTENING":
            continue
        local = fields[1]
        if ":" not in local:
            continue
        try:
            port = int(local.rsplit(":", 1)[1])
        except ValueError:
            continue
        if port in ports and port not in listening:
            listening.append(port)
    return sorted(listening)


# ==========================================================================
# 快捷方式（IShellLink via ctypes —— 不依赖 pywin32）
# ==========================================================================

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(d1, d4):
    return _GUID(d1, 0, 0, (ctypes.c_ubyte * 8)(*d4))


CLSID_SHELL_LINK = _guid(0x00021401, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))
IID_ISHELL_LINK_W = _guid(0x000214F9, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))
IID_IPERSIST_FILE = _guid(0x0000010B, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))

#: IShellLinkW 的虚表下标，顺序照 shlobj.h 声明，前三个是 IUnknown。
#:
#: 这里曾经整组错位：SetPath 被当成 17（其实是 SetIconLocation）、
#: SetWorkingDirectory 被当成 20（其实那才是 SetPath）。后果不是报错，而是
#: 静默写出了一个**指向安装目录**的快捷方式 —— 双击打开的是文件夹，图标和
#: 描述全空。COM 调用不校验参数语义，错下标照样返回 S_OK，所以必须靠调用后
#: 回读校验（read_shortcut）兜住，不能只看返回值。
_VT_QUERY_INTERFACE = 0
_VT_GET_PATH = 3
_VT_GET_DESCRIPTION = 6
_VT_SET_DESCRIPTION = 7
_VT_GET_WORKING_DIR = 8
_VT_SET_WORKING_DIR = 9
_VT_GET_ARGUMENTS = 10
_VT_SET_ARGUMENTS = 11
_VT_GET_ICON_LOCATION = 16
_VT_SET_ICON_LOCATION = 17
_VT_SET_PATH = 20
#: IPersistFile 的虚表下标。
_VT_PF_LOAD = 5
_VT_SAVE = 6


def _vt_call(ptr, index, restype, argtypes, *args):
    """Invoke vtable entry ``index`` on the COM interface at ``ptr``."""
    table = ctypes.cast(
        ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(table[index])(ptr, *args)


def _with_shell_link(fn):
    """Run ``fn(psl)`` against a fresh IShellLinkW, creating and releasing it.

    Returns ``(ok, value_or_error)``. Every COM call here uses the corrected
    vtable indices; see the constants above for why they are easy to get wrong.
    """
    ole32 = ctypes.windll.ole32
    hr = ole32.CoInitialize(None)
    initialized = hr in (0, 1)                       # S_OK or S_FALSE
    psl = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_SHELL_LINK), None, 1,   # CLSCTX_INPROC_SERVER
            ctypes.byref(IID_ISHELL_LINK_W), ctypes.byref(psl))
        if hr != 0 or not psl:
            return False, "CoCreateInstance 失败 (0x%08X)" % (hr & 0xFFFFFFFF)
        return fn(psl)
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    finally:
        if psl:
            try:
                _vt_call(psl, 2, ctypes.c_ulong, [])   # Release
            except Exception:
                pass
        if initialized:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass


def read_shortcut(link_path):
    """Read a .lnk back and return its target/workdir/icon as a dict.

    Used right after ``create_shortcut`` to prove the link really points at
    the executable. The COM setters return S_OK even when the vtable index is
    wrong, so a successful create is not evidence of a correct link - only
    reading it back is.
    """
    def body(psl):
        ppf = ctypes.c_void_p()
        hr = _vt_call(psl, _VT_QUERY_INTERFACE, ctypes.c_long,
                      [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
                      ctypes.byref(IID_IPERSIST_FILE), ctypes.byref(ppf))
        if hr != 0 or not ppf:
            return False, "IPersistFile 查询失败 (0x%08X)" % (hr & 0xFFFFFFFF)
        try:
            hr = _vt_call(ppf, _VT_PF_LOAD, ctypes.c_long,
                          [ctypes.c_wchar_p, ctypes.c_int], link_path, 0)
            if hr != 0:
                return False, "读取快捷方式失败 (0x%08X)" % (hr & 0xFFFFFFFF)

            out = {}
            buf = ctypes.create_unicode_buffer(2048)
            icon_idx = ctypes.c_int()
            # GetPath 有四个参数：pszFile, cch, WIN32_FIND_DATAW*, fFlags
            for flags, key in ((0, "target"), (4, "target_raw")):
                _vt_call(psl, _VT_GET_PATH, ctypes.c_long,
                         [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p,
                          ctypes.c_ulong], buf, 2048, None, flags)
                out[key] = buf.value
            _vt_call(psl, _VT_GET_WORKING_DIR, ctypes.c_long,
                     [ctypes.c_wchar_p, ctypes.c_int], buf, 2048)
            out["workdir"] = buf.value
            _vt_call(psl, _VT_GET_ARGUMENTS, ctypes.c_long,
                     [ctypes.c_wchar_p, ctypes.c_int], buf, 2048)
            out["args"] = buf.value
            _vt_call(psl, _VT_GET_ICON_LOCATION, ctypes.c_long,
                     [ctypes.c_wchar_p, ctypes.c_int,
                      ctypes.POINTER(ctypes.c_int)], buf, 2048,
                     ctypes.byref(icon_idx))
            out["icon"] = buf.value
            out["icon_index"] = icon_idx.value
            return True, out
        finally:
            try:
                _vt_call(ppf, 2, ctypes.c_ulong, [])   # Release
            except Exception:
                pass

    return _with_shell_link(body)


def create_shortcut(link_path, target, arguments="", workdir="",
                    icon="", description="", verify=True):
    """Create a .lnk at ``link_path`` pointing at ``target``.

    Uses IShellLinkW + IPersistFile through raw COM calls, so no pywin32 is
    required. Returns True on success; failures are reported rather than
    raised because a missing shortcut should not fail an install.

    With ``verify`` (the default) the link is read back and rejected unless
    its target matches ``target``. That check exists because a wrong vtable
    index writes a plausible-looking but wrong link while still returning
    S_OK: the shipped 1.8.2 setup pointed its shortcuts at the install
    directory, so double-clicking opened a folder instead of launching the
    program.
    """
    def body(psl):
        _vt_call(psl, _VT_SET_PATH, ctypes.c_long, [ctypes.c_wchar_p], target)
        if arguments:
            _vt_call(psl, _VT_SET_ARGUMENTS, ctypes.c_long,
                     [ctypes.c_wchar_p], arguments)
        if workdir:
            _vt_call(psl, _VT_SET_WORKING_DIR, ctypes.c_long,
                     [ctypes.c_wchar_p], workdir)
        if description:
            _vt_call(psl, _VT_SET_DESCRIPTION, ctypes.c_long,
                     [ctypes.c_wchar_p], description)
        _vt_call(psl, _VT_SET_ICON_LOCATION, ctypes.c_long,
                 [ctypes.c_wchar_p, ctypes.c_int], icon or target, 0)

        ppf = ctypes.c_void_p()
        hr = _vt_call(psl, _VT_QUERY_INTERFACE, ctypes.c_long,
                      [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
                      ctypes.byref(IID_IPERSIST_FILE), ctypes.byref(ppf))
        if hr != 0 or not ppf:
            return False, "IPersistFile 查询失败 (0x%08X)" % (hr & 0xFFFFFFFF)
        try:
            os.makedirs(os.path.dirname(link_path), exist_ok=True)
            hr = _vt_call(ppf, _VT_SAVE, ctypes.c_long,
                          [ctypes.c_wchar_p, ctypes.c_int], link_path, 1)
            if hr != 0:
                return False, "保存快捷方式失败 (0x%08X)" % (hr & 0xFFFFFFFF)
        finally:
            try:
                _vt_call(ppf, 2, ctypes.c_ulong, [])   # Release
            except Exception:
                pass

        if not verify:
            return True, ""

        ok, info = read_shortcut(link_path)
        if not ok:
            return False, "校验快捷方式失败：%s" % info
        got = os.path.normcase(os.path.normpath(info.get("target") or ""))
        want = os.path.normcase(os.path.normpath(target))
        if got != want:
            return False, ("快捷方式指向错误：期望 %s，实际 %s"
                           % (target, info.get("target") or "(空)"))
        return True, ""

    return _with_shell_link(body)


def start_menu_dir(all_users):
    """The Start Menu \\ Programs folder to put the group in."""
    if all_users:
        base = os.environ.get("ProgramData") or r"C:\ProgramData"
    else:
        base = (os.environ.get("APPDATA")
                or os.path.join(os.path.expanduser("~"), "AppData", "Roaming"))
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs")


def desktop_dir(all_users):
    """The Desktop folder, shared or per-user."""
    if all_users:
        return os.path.join(os.environ.get("PUBLIC") or r"C:\Users\Public",
                            "Desktop")
    return os.path.join(os.path.expanduser("~"), "Desktop")


# ==========================================================================
# 注册表（Add/Remove Programs）
# ==========================================================================

def _reg_root(all_users):
    import winreg
    return winreg.HKEY_LOCAL_MACHINE if all_users else winreg.HKEY_CURRENT_USER


def write_uninstall_entry(install_dir, version, all_users, size_kb):
    """Register the install so it shows up in 程序和功能."""
    import winreg
    exe = os.path.join(install_dir, APP_EXE)
    uninst = os.path.join(install_dir, UNINST_EXE)
    values = {
        "DisplayName": APP_DISPLAY,
        "DisplayVersion": version,
        "Publisher": PUBLISHER,
        "InstallLocation": install_dir,
        "DisplayIcon": exe,
        "UninstallString": '"%s"' % uninst,
        "QuietUninstallString": '"%s" --silent' % uninst,
        "NoModify": 1,
        "NoRepair": 1,
        "EstimatedSize": max(1, int(size_kb)),
    }
    with winreg.CreateKeyEx(_reg_root(all_users), UNINSTALL_KEY, 0,
                            winreg.KEY_WRITE) as key:
        for name, value in values.items():
            if isinstance(value, int):
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def delete_uninstall_entry(all_users):
    """Remove the Add/Remove Programs entry.

    ``DeleteKeyEx``'s third argument is a **WOW64 view flag**, not a security
    mask: passing ``KEY_WRITE`` there (0x00020006) sets the 32-bit bit
    (0x0200) and sends the delete into ``WOW6432Node``, where the entry
    written by :func:`write_uninstall_entry` does not exist. The uninstall
    then silently leaves a dead row behind in 程序和功能. The flag is
    therefore left at 0 so both calls stay in the native view.
    """
    import winreg
    try:
        winreg.DeleteKeyEx(_reg_root(all_users), UNINSTALL_KEY, 0, 0)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False


def read_uninstall_entry(all_users):
    """The registered values, or an empty dict when not installed."""
    import winreg
    out = {}
    try:
        with winreg.OpenKey(_reg_root(all_users), UNINSTALL_KEY) as key:
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                out[name] = value
                index += 1
    except Exception:
        pass
    return out


# ==========================================================================
# 已有安装检测
# ==========================================================================

def _process_paths(exe_name=APP_EXE):
    """Full image paths of running processes named ``exe_name``.

    Raw Win32 rather than ``tasklist`` (which reports only the image name and
    no directory) or PowerShell (slow to start and encoding-sensitive). Three
    calls: enumerate every PID, open the ones we are allowed to, and ask each
    for its image path. Processes we cannot open - another user's, or an
    elevated one - are simply skipped; not knowing about them is the same as
    the installer's behaviour before this existed.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    #: PROCESS_QUERY_LIMITED_INFORMATION: enough for the path, and the only
    #: right a non-elevated process can get on an elevated one.
    _QUERY_LIMITED = 0x1000

    count = 2048
    while True:
        array = (wintypes.DWORD * count)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcesses(array, ctypes.sizeof(array),
                                   ctypes.byref(needed)):
            return []
        used = needed.value // ctypes.sizeof(wintypes.DWORD)
        if used < count:
            pids = list(array[:used])
            break
        # The list could have grown past the buffer; retry with more room.
        count *= 2
        if count > 65536:
            return []

    paths = []
    buf = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(buf))
    for pid in pids:
        if not pid:
            continue
        handle = kernel32.OpenProcess(_QUERY_LIMITED, False, pid)
        if not handle:
            continue
        try:
            size.value = len(buf)
            if kernel32.QueryFullProcessImageNameW(
                    ctypes.c_void_p(handle), 0, buf, ctypes.byref(size)):
                path = buf.value
                if path and os.path.basename(path).lower() == \
                        exe_name.lower() and path not in paths:
                    paths.append(path)
        except Exception:
            pass
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    return paths


def _installation_sources():
    """Raw ``(path, origin)`` pairs for every install this machine admits to.

    Three sources are combined because none of them is complete on its own:

    * **registry** - the only place that records a folder the user picked by
      hand. Ordered first so it wins the de-duplication below and becomes the
      wizard's default: a registered install is the "real" one, whereas a
      running copy is often just a portable build someone is testing.
    * **default** - catches an install whose registry entry was lost, e.g. a
      copy restored from a backup.
    * **running** - the copy the user has open right now, located exactly.
    """
    sources = []

    for all_users in (True, False):
        location = read_uninstall_entry(all_users).get("InstallLocation")
        if location:
            sources.append((location, "registry"))

    for all_users in (False, True):
        sources.append((default_dir(all_users), "default"))

    for exe in _process_paths():
        sources.append((os.path.dirname(exe), "running"))

    return sources


def _running_dirs(exe_name=APP_EXE):
    """Directories a currently running copy of the program lives in."""
    dirs = set()
    for path in _process_paths(exe_name):
        try:
            dirs.add(os.path.normcase(os.path.dirname(os.path.abspath(path))))
        except Exception:
            pass
    return dirs


def describe_install(path, running_dirs=None):
    """What the chooser shows for one installation.

    "Running" is decided from the processes' own image paths rather than by
    probing the file: the install directory may be under ``Program Files``,
    where opening the exe for the exclusive access that probe needs fails
    with ACCESS_DENIED for a non-elevated process. That denial would be
    misread as "not running", which is exactly the wrong answer here.
    """
    path = os.path.abspath(path)
    if running_dirs is None:
        running_dirs = _running_dirs()
    info = {"path": path, "exists": os.path.isdir(path), "version": "",
            "all_users": False, "accounts": 0, "running": False,
            "uninstaller": False, "origin": ""}
    info["uninstaller"] = os.path.exists(os.path.join(path, UNINST_EXE))
    info["running"] = os.path.normcase(path) in running_dirs

    # install.json is written by this installer, so it is the most
    # trustworthy record of what is there; the registry is the fallback for
    # installs predating it or carried over from a portable copy.
    try:
        with open(os.path.join(path, "install.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        info["version"] = str(data.get("version") or "")
        info["all_users"] = bool(data.get("all_users"))
    except Exception:
        pass

    if not info["version"]:
        for all_users in (True, False):
            entry = read_uninstall_entry(all_users)
            if os.path.normcase(entry.get("InstallLocation", "")) == \
                    os.path.normcase(path):
                info["version"] = str(entry.get("DisplayVersion") or "")
                info["all_users"] = all_users
                break

    try:
        info["accounts"] = len([
            name for name in os.listdir(os.path.join(path, "accounts"))
            if name.endswith(".json") and name != "settings.json"])
    except Exception:
        pass
    return info


def install_summary(info):
    """A one-line description of an install, for the chooser."""
    bits = []
    if info.get("version"):
        bits.append("v" + str(info["version"]))
    bits.append("所有用户" if info.get("all_users") else "当前用户")
    if info.get("accounts"):
        bits.append("%d 个账号" % info["accounts"])
    if info.get("running"):
        bits.append("正在运行")
    if info.get("origin") == "running" and not info.get("version"):
        bits.append("便携版")
    if not info.get("exists"):
        bits.append("目录不存在")
    return " · ".join(bits)


def detect_installations(sources=None):
    """Every existing installation, best candidate first.

    Only directories that actually exist are returned: this list feeds a
    "which one do you want to update" chooser, and offering a folder that is
    not there would be a trap. A registry entry pointing at a deleted folder
    is therefore skipped too - the user gets a normal fresh install, which is
    what they wanted anyway.

    ``sources`` is a sequence of ``(path, origin)`` pairs and defaults to the
    live probe. Tests pass it explicitly so the result does not depend on
    what happens to be installed on the machine running the suite.
    """
    if sources is None:
        sources = _installation_sources()

    running_dirs = _running_dirs()
    seen = set()
    found = []
    for path, origin in sources:
        if not path:
            continue
        try:
            key = os.path.normcase(os.path.normpath(os.path.abspath(path)))
        except Exception:
            continue
        if key in seen:
            continue
        # An installation means the program is there. A default location that
        # was never used, or a registry entry left behind after a manual
        # delete, must not show up as a candidate to "update" - it would send
        # the user looking at an empty folder.
        if not is_install_dir(path):
            continue
        seen.add(key)
        info = describe_install(path, running_dirs=running_dirs)
        info["origin"] = origin
        found.append(info)
    return found


# ==========================================================================
# 载荷读取与解包
# ==========================================================================

def payload_path():
    """Where the bundled program archive lives.

    Frozen builds carry it inside the executable; a source checkout looks
    for it next to this file so the installer can be exercised without a
    full PyInstaller round trip.
    """
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        return os.path.join(base, "payload.zip")
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("payload.zip", os.path.join("dist", "payload.zip")):
        candidate = os.path.join(here, name)
        if os.path.exists(candidate):
            return candidate
    return None


def payload_version(archive):
    """Read the version recorded inside the payload, if it has one."""
    try:
        with zipfile.ZipFile(archive) as zf:
            if "payload.json" in zf.namelist():
                return json.loads(
                    zf.read("payload.json").decode("utf-8")).get("version", "")
    except Exception:
        pass
    return ""


def _aside_name(dest):
    """A free ``<dest>.old`` style name to park a locked file under."""
    base = dest + ".old"
    if not os.path.exists(base):
        return base
    # 上一轮留下的 .old 还在（多半是那个进程仍在跑）。先试删，再退到编号名。
    try:
        os.remove(base)
        return base
    except OSError:
        pass
    for index in range(1, 100):
        candidate = "%s.old%d" % (dest, index)
        if not os.path.exists(candidate):
            return candidate
    raise InstallError("无法为被占用的文件腾出名字：%s" % dest)


def _write_member(zf, member, target):
    """Extract one member, stepping around a file that is currently in use.

    A running program holds its own image open **without** ``FILE_SHARE_WRITE``,
    so overwriting it fails with ``ERROR_SHARING_VIOLATION`` (32) - which is
    exactly what an in-place upgrade of a running install hits. Windows does
    still allow the *name* to be moved, so the locked file is renamed aside and
    the new one takes its place; the old bytes vanish once the process exits,
    and :func:`clean_leftovers` sweeps them on the next run.

    Returns the aside path when one was created, else ``None``.
    """
    try:
        zf.extract(member, target)
        return None
    except PermissionError:
        original = sys.exc_info()[1]

    dest = os.path.join(target, member.filename.replace("/", os.sep))
    if not os.path.exists(dest):
        # 不是"文件被占用"，而是别处的权限问题，原样抛出。
        raise original
    aside = _aside_name(dest)
    try:
        os.replace(dest, aside)
    except OSError:
        raise original
    zf.extract(member, target)
    return aside


def clean_leftovers(target):
    """Delete ``*.old`` files parked by :func:`_write_member`.

    Best effort: a leftover whose process is still running cannot be removed
    yet, and leaving it costs nothing beyond the disk space.
    """
    removed = 0
    for root, _dirs, files in os.walk(target):
        for name in files:
            if ".old" not in name:
                continue
            path = os.path.join(root, name)
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def extract_payload(archive, target, progress=None):
    """Unpack the program files into ``target``.

    ``progress`` is called with (done, total, name) so the caller can show a
    progress bar. Extraction is done member-by-member so the bar moves, and a
    member whose destination is in use is parked rather than aborting the
    install (see :func:`_write_member`).
    """
    os.makedirs(target, exist_ok=True)
    parked = []
    with zipfile.ZipFile(archive) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        total = len(members)
        for index, member in enumerate(members, 1):
            if member.filename == "payload.json":
                if progress:
                    progress(index, total, member.filename)
                continue
            aside = _write_member(zf, member, target)
            if aside:
                parked.append(aside)
            if progress:
                progress(index, total, member.filename)
    return total, parked


def installed_size_kb(path):
    """Total size of the tree in kilobytes, for the ARP entry."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total // 1024


# ==========================================================================
# 安装
# ==========================================================================

def check_target(install_dir, force=False):
    """Reject install locations that would clearly be a mistake."""
    if not install_dir or not install_dir.strip():
        raise InstallError("安装位置不能为空。")
    abs_dir = os.path.abspath(install_dir)
    if os.path.splitdrive(abs_dir)[0] == "":
        raise InstallError("请提供完整的路径（例如 D:\\Apps\\WorkBuddy2API）。")
    if abs_dir.rstrip("\\/") in (os.path.splitdrive(abs_dir)[0] + "\\",
                                 os.path.expanduser("~")):
        raise InstallError("不要把程序装到磁盘根目录或用户主目录下。")
    if not force and not dir_writable(abs_dir):
        raise InstallError("这个位置没有写入权限：\n%s\n\n"
                           "请换一个位置，或改用“为所有用户安装”。" % abs_dir)
    return abs_dir


def do_install(archive, install_dir, version, all_users,
               desktop_icon=True, startmenu_icon=True,
               progress=None, log=print):
    """Install the program and return a summary dict.

    Steps: unpack program files, drop the uninstaller next to them, create
    the requested shortcuts, and register the install. User data that
    already exists is never touched.
    """
    install_dir = check_target(install_dir)
    log("安装位置：%s" % install_dir)

    log("正在解包程序文件 ...")
    count, parked = extract_payload(archive, install_dir, progress)
    log("已写入 %d 个文件" % count)
    if parked:
        # 旧实例还在跑：新文件已经就位，但旧镜像要到它退出后才消失。
        # 绝不能在这里结束那个进程 —— 它可能正在给别的客户端服务。
        log("有 %d 个文件正被运行中的实例占用，已挪到一边："
            "该实例退出后会自动消失。" % len(parked))
        for path in parked:
            log("    %s" % os.path.basename(path))
    # 顺手清掉上一轮遗留的 .old（那个进程可能已经退出了）。
    swept = clean_leftovers(install_dir)
    if swept:
        log("已清理 %d 个上次更新遗留的旧文件" % swept)

    exe = os.path.join(install_dir, APP_EXE)
    if not os.path.exists(exe):
        raise InstallError("安装包不完整：没有找到 %s" % APP_EXE)

    # 卸载程序：与安装器同源的独立构建，放在安装目录里。
    uninst_src = bundled_uninstaller()
    uninst_dst = os.path.join(install_dir, UNINST_EXE)
    uninst_ok = False
    if uninst_src and os.path.exists(uninst_src):
        try:
            shutil.copy2(uninst_src, uninst_dst)
            uninst_ok = True
            log("已放置卸载程序 uninstall.exe")
        except Exception as exc:
            log("卸载程序复制失败：%s" % exc)

    shortcuts = []
    if desktop_icon:
        link = os.path.join(desktop_dir(all_users),
                            "%s.lnk" % APP_DISPLAY)
        ok, err = create_shortcut(link, exe, workdir=install_dir,
                                  description="%s 本地 API 网关" % APP_DISPLAY)
        shortcuts.append(("桌面", link, ok, err))
        log("桌面快捷方式：%s" % ("已创建" if ok else "失败 %s" % err))

    if startmenu_icon:
        group = os.path.join(start_menu_dir(all_users), APP_DISPLAY)
        link = os.path.join(group, "%s.lnk" % APP_DISPLAY)
        ok, err = create_shortcut(link, exe, workdir=install_dir,
                                  description="%s 本地 API 网关" % APP_DISPLAY)
        shortcuts.append(("开始菜单", link, ok, err))
        log("开始菜单快捷方式：%s" % ("已创建" if ok else "失败 %s" % err))
        if uninst_ok:
            create_shortcut(
                os.path.join(group, "卸载 %s.lnk" % APP_DISPLAY),
                uninst_dst, workdir=install_dir,
                description="卸载 %s" % APP_DISPLAY)

    size_kb = installed_size_kb(install_dir)
    try:
        write_uninstall_entry(install_dir, version, all_users, size_kb)
        log("已注册到“程序和功能”")
    except Exception as exc:
        log("注册表写入失败（不影响使用）：%s" % exc)

    # 记一份安装信息，卸载程序据此判断模式与版本。
    try:
        with open(os.path.join(install_dir, "install.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"version": version, "all_users": bool(all_users),
                       "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "install_dir": install_dir}, fh, ensure_ascii=False,
                      indent=2)
    except Exception:
        pass

    return {"install_dir": install_dir, "files": count,
            "shortcuts": shortcuts, "uninstaller": uninst_ok,
            "size_kb": size_kb}


def bundled_uninstaller():
    """The uninstaller shipped inside the setup executable, if any."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        candidate = os.path.join(base, UNINST_EXE)
        if os.path.exists(candidate):
            return candidate
        return None
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, UNINST_EXE),
                      os.path.join(here, "dist", "uninstaller", UNINST_EXE)):
        if os.path.exists(candidate):
            return candidate
    return None


# ==========================================================================
# 卸载
# ==========================================================================

def find_install_dir():
    """Locate the installation to remove.

    The running uninstaller lives in the install directory, so its own
    location is the answer; the registry is only a cross-check.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    for all_users in (False, True):
        entry = read_uninstall_entry(all_users)
        if entry.get("InstallLocation"):
            return entry["InstallLocation"]
    return ""


def is_install_dir(path):
    """True when ``path`` looks like an installation of this program."""
    return bool(path) and os.path.exists(os.path.join(path, APP_EXE))


def do_uninstall(install_dir, remove_data=False, log=print):
    """Remove the program, its shortcuts and its registry entry.

    The program directory itself is left in place when files are locked
    (the app may be running); a note is printed and the caller can retry
    after the user closes it.
    """
    install_dir = os.path.abspath(install_dir)
    all_users = False
    info_path = os.path.join(install_dir, "install.json")
    if os.path.exists(info_path):
        try:
            with open(info_path, encoding="utf-8") as fh:
                all_users = bool(json.load(fh).get("all_users"))
        except Exception:
            pass
    if not info_path and read_uninstall_entry(True).get("InstallLocation") == \
            install_dir:
        all_users = True

    log("正在卸载：%s" % install_dir)

    # 快捷方式
    removed = []
    for label, link in (
            ("桌面", os.path.join(desktop_dir(all_users),
                                  "%s.lnk" % APP_DISPLAY)),
            ("开始菜单", os.path.join(
                start_menu_dir(all_users), APP_DISPLAY,
                "%s.lnk" % APP_DISPLAY)),
            ("开始菜单", os.path.join(
                start_menu_dir(all_users), APP_DISPLAY,
                "卸载 %s.lnk" % APP_DISPLAY))):
        try:
            if os.path.exists(link):
                os.remove(link)
                removed.append(label)
        except Exception as exc:
            log("删除快捷方式失败 %s：%s" % (link, exc))
    # 同名程序在两种模式下都可能存在，把另一种也清掉。
    for other in (not all_users,):
        for link in (os.path.join(desktop_dir(other), "%s.lnk" % APP_DISPLAY),
                     os.path.join(start_menu_dir(other), APP_DISPLAY,
                                  "%s.lnk" % APP_DISPLAY),
                     os.path.join(start_menu_dir(other), APP_DISPLAY,
                                  "卸载 %s.lnk" % APP_DISPLAY)):
            try:
                if os.path.exists(link):
                    os.remove(link)
            except Exception:
                pass
    group = os.path.join(start_menu_dir(all_users), APP_DISPLAY)
    try:
        if os.path.isdir(group) and not os.listdir(group):
            os.rmdir(group)
    except Exception:
        pass
    if removed:
        log("已删除 %d 个快捷方式" % len(removed))

    # 用户数据（默认保留 —— 里面是账号与用量，误删代价太高）
    data_dirs = []
    local = appdata_data_dir()
    beside = os.path.join(os.path.dirname(install_dir), DATA_DIRNAME)
    for candidate in (local, os.path.join(install_dir, DATA_DIRNAME)):
        if os.path.isdir(candidate) and candidate not in data_dirs:
            data_dirs.append(candidate)
    del beside

    if remove_data:
        for candidate in data_dirs:
            try:
                shutil.rmtree(candidate)
                log("已删除数据目录：%s" % candidate)
            except Exception as exc:
                log("删除数据目录失败 %s：%s" % (candidate, exc))
    elif data_dirs:
        log("数据目录已保留：%s" % "、".join(data_dirs))

    # 注册表
    ok = delete_uninstall_entry(all_users)
    if all_users:
        delete_uninstall_entry(False)
    log("注册表项：%s" % ("已清理" if ok else "清理失败（可忽略）"))

    # 程序文件
    #
    # 卸载器自己就住在这个目录里，而且正在运行 —— 它锁住了自己的 exe，
    # 所以整个目录一次 rmtree 必然失败。做法是先把除自己之外的东西全清掉，
    # 再把整个目录交给一个分离的批处理：它等我们退出后才删。
    keep = os.path.normcase(os.path.abspath(sys.executable)) \
        if is_uninstaller_build() else ""
    removed_here, failed_here = remove_tree_except(install_dir, keep)
    log("已删除 %d 个程序文件" % removed_here)

    # 目录只要还在（要么有文件被锁，要么就是卸载器本身占着），
    # 就需要延迟删除来收尾。
    leftover = []
    if os.path.isdir(install_dir):
        leftover = [install_dir]
        if failed_here:
            log("有 %d 个文件被占用，稍后自动清理。" % len(failed_here))
        else:
            log("卸载程序将在退出后自动清理安装目录。")
    if leftover:
        schedule_self_delete(install_dir)

    return {"install_dir": install_dir, "data_kept": (not remove_data),
            "data_dirs": data_dirs, "leftover": leftover}


def remove_tree_except(root, keep_path):
    """Delete everything under ``root`` except ``keep_path`` itself.

    Returns ``(removed_count, failed_paths)``. Directories are removed
    bottom-up so an emptied folder can go; ``keep_path``'s parent directories
    are left alone because they still contain it.
    """
    root = os.path.abspath(root)
    keep = os.path.normcase(os.path.abspath(keep_path)) if keep_path else ""
    removed = 0
    failed = []
    if not os.path.isdir(root):
        return 0, []

    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            full = os.path.join(dirpath, name)
            if keep and os.path.normcase(os.path.abspath(full)) == keep:
                continue
            try:
                os.remove(full)
                removed += 1
            except Exception:
                failed.append(full)
        if keep and os.path.normcase(os.path.abspath(dirpath)) == \
                os.path.dirname(keep):
            continue                       # still holds the running file
        for name in dirnames:
            full = os.path.join(dirpath, name)
            try:
                os.rmdir(full)
            except OSError:
                pass                        # not empty, or is keep's parent
        if os.path.normcase(dirpath) != os.path.normcase(root):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

    if not keep:
        try:
            os.rmdir(root)
        except OSError:
            pass
    return removed, failed


def schedule_self_delete(directory):
    """Ask a detached cmd to remove ``directory`` once we exit.

    A running executable cannot delete itself, so the removal is handed to a
    short-lived shell. Two details make or break this:

    **The script must not wait on a PID.** An earlier version polled
    ``tasklist`` for our own PID and looped until it disappeared; it never
    finished. Retrying the actual operation is both simpler and locale-proof:
    ``rmdir`` fails while the file is locked and succeeds the moment it is
    released, and the loop is bounded so it can never hang forever.

    **Line endings must be written literally.** ``open(..., "w")`` translates
    ``\\n`` to ``\\r\\n`` on Windows, so writing ``\\r\\n`` by hand produced
    ``\\r\\r\\n`` and cmd mis-parsed the script - the deletion silently never
    happened. ``newline="\\r\\n"`` writes what is intended.

    **The template is not built with ``%`` formatting.** In a batch file a
    for-loop variable is ``%%i``, but ``"...%%i..." % (...)`` collapses it to
    ``%i`` and cmd aborts with "i was unexpected at this time". Substituting
    a placeholder instead keeps every percent sign literal.
    """
    try:
        script = ("@echo off\n"
                  "for /L %%i in (1,1,60) do (\n"
                  "    rmdir /s /q \"__DIR__\" 2>nul\n"
                  "    if not exist \"__DIR__\" goto done\n"
                  "    ping -n 2 127.0.0.1 >nul\n"
                  ")\n"
                  ":done\n"
                  "del \"%~f0\" >nul 2>nul\n").replace("__DIR__", directory)
        fd, path = tempfile.mkstemp(suffix=".bat", prefix="wb_uninst_")
        os.close(fd)
        # newline="\r\n": translate the plain \n above into real CRLF once.
        with open(path, "w", encoding="gbk", errors="replace",
                  newline="\r\n") as fh:
            fh.write(script)
        subprocess.Popen(["cmd", "/c", path],
                         creationflags=0x00000008 | 0x08000000,
                         close_fds=True)
        return True
    except Exception:
        return False


# ==========================================================================
# 图形向导
# ==========================================================================

BG = "#f0f0f0"
PANEL = "#ffffff"
ACCENT = "#2563eb"
TEXT = "#1f2937"
MUTED = "#6b7280"


def run_wizard(archive, version, start_dir=None, silent=False,
               all_users=False, desktop_icon=True, startmenu_icon=True,
               launch=False, installations=None):
    """Show the install wizard. Returns a process exit code.

    When an earlier installation exists, the wizard aims at it instead of
    silently dropping a second copy in the default folder: with one match the
    path is pre-filled (and explained), with several the user picks which one
    to update. ``installations`` overrides the live probe for tests.
    """
    import tkinter as tk
    from tkinter import filedialog, ttk

    if installations is None:
        installations = detect_installations()
    # 只保留真实存在的安装：选择页列出一个空目录会把人引向错误的目标。
    installations = [info for info in installations if info.get("exists", True)]

    state = {
        "dir": start_dir or default_dir(all_users),
        "all_users": all_users,
        "desktop": desktop_icon,
        "startmenu": startmenu_icon,
        "launch": launch,
        "done": None,
        "error": "",
        "page": 0,
        "installs": installations,
        "existing": None,
    }

    # 已有安装就是默认目标：把路径填好，多于一处的再让用户挑。这样
    # "更新"就是双击安装包的默认行为，而不是在别处又装一份。
    if start_dir is None and installations:
        state["existing"] = installations[0]
        state["dir"] = installations[0]["path"]
        state["all_users"] = bool(installations[0]["all_users"])

    root = tk.Tk()
    root.title("安装 %s %s" % (APP_DISPLAY, version))
    root.resizable(False, False)
    try:
        icon = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "app.ico")
        if getattr(sys, "frozen", False):
            icon = os.path.join(getattr(sys, "_MEIPASS", ""), "app.ico")
        if os.path.exists(icon):
            root.iconbitmap(icon)
    except Exception:
        pass

    width, height = 640, 460
    root.geometry("%dx%d" % (width, height))
    root.configure(bg=PANEL)
    try:
        root.eval("tk::PlaceWindow . center")
    except Exception:
        pass

    # ---- 顶部标题栏（安装器的常见长相） ----
    header = tk.Frame(root, bg=PANEL, height=64)
    header.pack(fill="x")
    header.pack_propagate(False)
    title_label = tk.Label(header, text="欢迎使用 %s 安装向导" % APP_DISPLAY,
                           bg=PANEL, fg=TEXT,
                           font=("Microsoft YaHei UI", 14, "bold"))
    title_label.pack(anchor="w", padx=24, pady=(16, 0))
    sub_label = tk.Label(header, text="这个向导将引导你完成安装。",
                         bg=PANEL, fg=MUTED,
                         font=("Microsoft YaHei UI", 9))
    sub_label.pack(anchor="w", padx=24)

    ttk.Separator(root, orient="horizontal").pack(fill="x")

    body = tk.Frame(root, bg=PANEL)
    body.pack(fill="both", expand=True)

    # ---- 底部按钮栏 ----
    ttk.Separator(root, orient="horizontal").pack(fill="x", side="bottom")
    bar = tk.Frame(root, bg=BG, height=56)
    bar.pack(fill="x", side="bottom")
    bar.pack_propagate(False)

    pages = {}

    #: 页面顺序。只有检测到多个已有安装时才插入"选择"页，导航按页名
    #: 而不是下标工作，所以顺序里有几页都不影响其余逻辑。
    order = ["welcome"]
    if len(installations) > 1:
        order.append("pick")
    order += ["dir", "opt", "run", "done"]

    #: 每页的标题栏文案。
    page_titles = {
        "welcome": ("欢迎使用 %s 安装向导" % APP_DISPLAY,
                    "这个向导将引导你完成安装。"),
        "pick": ("选择要更新的安装",
                 "这台电脑上有多处 %s，请选择要更新哪一个。" % APP_DISPLAY),
        "dir": ("选择安装位置",
                "安装程序会把程序文件放到下面的文件夹。"),
        "opt": ("安装选项", "选择安装时要执行的其他任务。"),
        "run": ("正在安装", "安装程序正在把文件复制到你的电脑。"),
        "done": ("安装完成", "向导已完成，点击“完成”退出。"),
    }

    def show(name):
        for frame in pages.values():
            frame.pack_forget()
        pages[name].pack(fill="both", expand=True)
        state["page"] = order.index(name)
        heading, subtitle = page_titles.get(name, (APP_DISPLAY, ""))
        title_label.configure(text=heading)
        sub_label.configure(text=subtitle)

    # ---------- 页面 1：欢迎 ----------
    p_welcome = tk.Frame(body, bg=PANEL)
    pages["welcome"] = p_welcome
    tk.Label(p_welcome, text="即将在你的电脑上安装 %s %s。"
             % (APP_DISPLAY, version),
             bg=PANEL, fg=TEXT, font=("Microsoft YaHei UI", 10),
             wraplength=560, justify="left").pack(anchor="w", padx=24, pady=(20, 8))
    tk.Label(p_welcome,
             text="%s 是一个本地 API 网关，把多种 AI 供应商统一成 "
                  "OpenAI / Anthropic 兼容的接口，并提供账号池、熔断与用量看板。"
                  % APP_DISPLAY,
             bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9),
             wraplength=560, justify="left").pack(anchor="w", padx=24)
    tk.Label(p_welcome, text="点击“下一步”继续，或点击“取消”退出安装。",
             bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9),
             wraplength=560, justify="left").pack(anchor="w", padx=24, pady=(16, 0))

    # ---------- 页面 1b：选择已有安装（仅在有多处安装时出现） ----------
    pick_var = tk.StringVar(value="")
    if len(installations) > 1:
        p_pick = tk.Frame(body, bg=PANEL)
        pages["pick"] = p_pick
        tk.Label(p_pick,
                 text="更新会覆盖所选目录里的程序文件。选中后仍可在下一步"
                      "修改路径。",
                 bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9),
                 wraplength=560, justify="left").pack(anchor="w", padx=24,
                                                       pady=(20, 0))

        pick_box = tk.Frame(p_pick, bg=PANEL)
        pick_box.pack(fill="both", expand=True, padx=24, pady=(12, 0))
        for index, info in enumerate(installations):
            value = str(index)
            row = tk.Frame(pick_box, bg=PANEL)
            row.pack(fill="x", anchor="w", pady=2)
            tk.Radiobutton(row, text=info["path"], variable=pick_var,
                           value=value, bg=PANEL, fg=TEXT, selectcolor=PANEL,
                           activebackground=PANEL, anchor="w",
                           font=("Microsoft YaHei UI", 9),
                           command=lambda: apply_pick(pick_var.get())).pack(
                anchor="w")
            tk.Label(row, text="    " + install_summary(info), bg=PANEL,
                     fg=MUTED, font=("Microsoft YaHei UI", 8),
                     wraplength=540, justify="left").pack(anchor="w")
        pick_var.set("0")

    def apply_pick(value):
        """选中一处安装后，把它的路径与安装模式同步到后续页面。"""
        try:
            info = installations[int(value)]
        except (ValueError, IndexError):
            return
        state["existing"] = info
        state["all_users"] = bool(info["all_users"])
        state["dir"] = info["path"]
        dir_var.set(info["path"])
        scope.set("all" if info["all_users"] else "me")
        warn_var.set(scope_hint())

    # ---------- 页面 2：安装位置 ----------
    p_dir = tk.Frame(body, bg=PANEL)
    pages["dir"] = p_dir
    dir_hint_var = tk.StringVar(value="")
    tk.Label(p_dir, text="安装程序会把程序文件放到下面的文件夹。",
             bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(
        anchor="w", padx=24, pady=(20, 0))

    scope = tk.StringVar(value="all" if state["all_users"] else "me")
    scope_frame = tk.Frame(p_dir, bg=PANEL)
    scope_frame.pack(fill="x", padx=24, pady=(12, 4))

    dir_var = tk.StringVar(value=state["dir"])

    def on_scope():
        state["all_users"] = (scope.get() == "all")
        dir_var.set(default_dir(state["all_users"]))
        warn_var.set(scope_hint())

    def scope_hint():
        if state["all_users"] and not is_admin():
            return ("已选择“为所有用户安装”，需要管理员权限，"
                    "开始安装时会弹出 UAC 提示。")
        return ""

    tk.Radiobutton(scope_frame, text="为所有用户安装（需要管理员权限）",
                   variable=scope, value="all", command=on_scope,
                   bg=PANEL, fg=TEXT, selectcolor=PANEL,
                   activebackground=PANEL,
                   font=("Microsoft YaHei UI", 9)).pack(anchor="w")
    tk.Radiobutton(scope_frame, text="仅为我安装（不需要管理员权限）",
                   variable=scope, value="me", command=on_scope,
                   bg=PANEL, fg=TEXT, selectcolor=PANEL,
                   activebackground=PANEL,
                   font=("Microsoft YaHei UI", 9)).pack(anchor="w")

    path_row = tk.Frame(p_dir, bg=PANEL)
    path_row.pack(fill="x", padx=24, pady=(10, 0))
    entry = ttk.Entry(path_row, textvariable=dir_var, width=52)
    entry.pack(side="left", fill="x", expand=True)

    def browse():
        chosen = filedialog.askdirectory(
            title="选择安装位置", initialdir=dir_var.get() or os.path.expanduser("~"))
        if chosen:
            dir_var.set(os.path.normpath(chosen))

    ttk.Button(path_row, text="浏览...", command=browse, width=10).pack(
        side="left", padx=(8, 0))

    warn_var = tk.StringVar(value=scope_hint())
    tk.Label(p_dir, textvariable=warn_var, bg=PANEL, fg="#b45309",
             font=("Microsoft YaHei UI", 8), wraplength=560,
             justify="left").pack(anchor="w", padx=24, pady=(8, 0))

    # 路径被预填成已有安装时要说清原因，否则用户会以为装错了地方。
    def refresh_dir_hint():
        info = state.get("existing")
        if not info or os.path.normcase(os.path.abspath(dir_var.get())) != \
                os.path.normcase(os.path.abspath(info["path"])):
            dir_hint_var.set("")
            return
        dir_hint_var.set("已检测到这里的安装（%s），安装将就地更新它。"
                         % install_summary(info))

    dir_var.trace_add("write", lambda *_a: refresh_dir_hint())
    refresh_dir_hint()
    tk.Label(p_dir, textvariable=dir_hint_var, bg=PANEL, fg=ACCENT,
             font=("Microsoft YaHei UI", 8), wraplength=560,
             justify="left").pack(anchor="w", padx=24, pady=(4, 0))

    err_var = tk.StringVar(value="")
    err_label = tk.Label(p_dir, textvariable=err_var, bg=PANEL, fg="#dc2626",
                         font=("Microsoft YaHei UI", 8), wraplength=560,
                         justify="left")
    err_label.pack(anchor="w", padx=24, pady=(6, 0))

    tk.Label(p_dir, text="所需磁盘空间约 120 MB。已存在的账号与用量数据不会被改动。",
             bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 8),
             wraplength=560, justify="left").pack(anchor="w", padx=24, pady=(10, 0))

    # ---------- 页面 3：安装选项 ----------
    p_opt = tk.Frame(body, bg=PANEL)
    pages["opt"] = p_opt
    tk.Label(p_opt, text="选择安装时要执行的其他任务。",
             bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(
        anchor="w", padx=24, pady=(20, 0))

    opt_frame = tk.Frame(p_opt, bg=PANEL)
    opt_frame.pack(fill="x", padx=24, pady=(12, 0))

    desktop_var = tk.IntVar(value=1 if state["desktop"] else 0)
    startmenu_var = tk.IntVar(value=1 if state["startmenu"] else 0)
    launch_var = tk.IntVar(value=1 if state["launch"] else 0)

    for text, var in (
            ("创建桌面快捷方式", desktop_var),
            ("创建开始菜单快捷方式", startmenu_var),
            ("安装完成后启动 %s" % APP_DISPLAY, launch_var)):
        tk.Checkbutton(opt_frame, text=text, variable=var, bg=PANEL, fg=TEXT,
                       selectcolor=PANEL, activebackground=PANEL,
                       font=("Microsoft YaHei UI", 9)).pack(anchor="w",
                                                            pady=(2, 2))

    hint_var = tk.StringVar(value="")
    tk.Label(p_opt, textvariable=hint_var, bg="#fef3c7", fg="#92400e",
             font=("Microsoft YaHei UI", 8), wraplength=560, justify="left",
             anchor="w").pack(fill="x", padx=24, pady=(14, 0))

    # ---------- 页面 4：安装中 ----------
    p_run = tk.Frame(body, bg=PANEL)
    pages["run"] = p_run
    tk.Label(p_run, text="安装程序正在把文件复制到你的电脑。",
             bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(
        anchor="w", padx=24, pady=(20, 0))

    prog_var = tk.DoubleVar(value=0.0)
    prog = ttk.Progressbar(p_run, variable=prog_var, maximum=100.0,
                           length=560)
    prog.pack(padx=24, pady=(20, 8), anchor="w")
    status_var = tk.StringVar(value="准备中 ...")
    tk.Label(p_run, textvariable=status_var, bg=PANEL, fg=MUTED,
             font=("Microsoft YaHei UI", 8), wraplength=560,
             justify="left").pack(anchor="w", padx=24)

    log_text = tk.Text(p_run, height=7, width=70, relief="solid", borderwidth=1,
                       font=("Consolas", 8), bg="#fafafa", fg=TEXT)
    log_text.pack(padx=24, pady=(12, 0), fill="both", expand=True)
    log_text.configure(state="disabled")

    # ---------- 页面 5：完成 ----------
    p_done = tk.Frame(body, bg=PANEL)
    pages["done"] = p_done
    done_body = tk.Label(p_done, text="", bg=PANEL, fg=MUTED,
                         font=("Microsoft YaHei UI", 9), wraplength=560,
                         justify="left")
    done_body.pack(anchor="w", padx=24, pady=(20, 0))

    # ---- 按钮 ----
    #
    # Windows 向导的按钮从左到右是 上一步 / 下一步 / 取消，所以从右侧
    # 依次 pack 的顺序必须反过来：先 pack 的最靠右。
    back_btn = ttk.Button(bar, text="上一步", width=12)
    next_btn = ttk.Button(bar, text="下一步", width=12)
    cancel_btn = ttk.Button(bar, text="取消", width=12)
    cancel_btn.pack(side="right", padx=(6, 16), pady=14)
    next_btn.pack(side="right", padx=6, pady=14)
    back_btn.pack(side="right", padx=6, pady=14)

    def append_log(message):
        log_text.configure(state="normal")
        log_text.insert("end", message + "\n")
        log_text.see("end")
        log_text.configure(state="disabled")
        root.update_idletasks()

    def current_name():
        return order[state["page"]]

    def update_buttons():
        name = current_name()
        if name == "done":
            back_btn.configure(state="disabled")
            next_btn.configure(text="完成", state="normal")
            cancel_btn.configure(state="normal")
        elif name == "run":
            back_btn.configure(state="disabled")
            next_btn.configure(text="下一步", state="disabled")
            cancel_btn.configure(state="disabled")
        else:
            # 安装中与完成后不能回退；第一页没有可回退的目标。
            back_btn.configure(
                state=("disabled" if name == "welcome" else "normal"))
            next_btn.configure(text="下一步", state="normal")
            cancel_btn.configure(state="normal")

    def go_next():
        name = current_name()
        if name == "pick":
            apply_pick(pick_var.get())
        elif name == "dir":
            try:
                state["dir"] = check_target(dir_var.get(), force=True)
            except InstallError as exc:
                err_var.set(str(exc))
                return
            err_var.set("")
            if running_instances():
                hint_var.set(
                    "检测到 %s 正在运行。安装不会结束它 —— 请先自行退出，"
                    "否则部分文件可能被占用。" % APP_DISPLAY)
            elif gateway_listening():
                hint_var.set(
                    "检测到本机 8787/8788 端口有服务在监听，可能正是 %s。"
                    "安装不会动它。" % APP_DISPLAY)
            else:
                hint_var.set("")
        elif name == "opt":
            state["desktop"] = bool(desktop_var.get())
            state["startmenu"] = bool(startmenu_var.get())
            state["launch"] = bool(launch_var.get())
            start_install()
            return
        elif name == "done":
            root.destroy()
            return
        if state["page"] + 1 < len(order):
            show(order[state["page"] + 1])
            update_buttons()

    def go_back():
        name = current_name()
        if name not in ("run", "done") and state["page"] > 0:
            show(order[state["page"] - 1])
            update_buttons()

    def do_cancel():
        if current_name() == "run":
            return
        root.destroy()

    next_btn.configure(command=go_next)
    back_btn.configure(command=go_back)
    cancel_btn.configure(command=do_cancel)
    root.protocol("WM_DELETE_WINDOW", do_cancel)

    def start_install():
        show("run")
        update_buttons()
        root.update_idletasks()

        def progress(done, total, name):
            prog_var.set(done * 100.0 / max(1, total))
            if done % 10 == 0 or done == total:
                status_var.set("正在写入 %s" % name)
                root.update_idletasks()

        # 需要管理员但当前没有权限时，先提权重启自己。
        if state["all_users"] and not is_admin():
            append_log("需要管理员权限，正在请求提权 ...")
            root.update_idletasks()
            if relaunch_elevated(state):
                root.destroy()
                return
            append_log("提权被取消，改为仅为我安装。")
            state["all_users"] = False
            state["dir"] = default_dir(False)

        try:
            result = do_install(
                archive, state["dir"], version, state["all_users"],
                desktop_icon=state["desktop"],
                startmenu_icon=state["startmenu"],
                progress=progress, log=append_log)
            state["done"] = result
            prog_var.set(100.0)
            status_var.set("完成")
            made = [label for label, _l, ok, _e in result["shortcuts"] if ok]
            text = ("%s %s 已安装到：\n\n%s\n\n" % (
                APP_DISPLAY, version, result["install_dir"]))
            if made:
                text += "已创建快捷方式：%s\n" % "、".join(made)
            text += ("\n账号与用量数据保存在程序目录旁（或 "
                     "%s），卸载时默认保留。"
                     % appdata_data_dir())
            done_body.configure(text=text)
            show("done")
            update_buttons()
            if state["launch"]:
                launch_app(result["install_dir"])
        except Exception as exc:
            state["error"] = str(exc)
            append_log("安装失败：%s" % exc)
            status_var.set("安装失败")
            from tkinter import messagebox
            messagebox.showerror("安装失败", str(exc))
            show("opt")
            update_buttons()

    show("welcome")
    update_buttons()
    root.mainloop()

    if state["error"]:
        return 1
    return 0


def relaunch_elevated(state):
    """Restart this installer with a UAC prompt. True when it was started."""
    try:
        if getattr(sys, "frozen", False):
            exe = sys.executable
            params = []
        else:
            exe = sys.executable
            params = [os.path.abspath(__file__)]
        params += ["--all-users", "--dir", state["dir"]]
        if state.get("desktop"):
            params.append("--desktop")
        if state.get("startmenu"):
            params.append("--startmenu")
        if state.get("launch"):
            params.append("--launch")
        arg_line = subprocess.list2cmdline(params)
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, arg_line, None, 1)
        return int(ret) > 32
    except Exception:
        return False


def launch_app(install_dir):
    """Start the installed program detached."""
    exe = os.path.join(install_dir, APP_EXE)
    if not os.path.exists(exe):
        return False
    try:
        subprocess.Popen([exe], cwd=install_dir, close_fds=True,
                         creationflags=0x00000008)
        return True
    except Exception:
        return False


# ==========================================================================
# 卸载界面
# ==========================================================================

def ask_yes_no(title, text, yes_no=True, silent=False, default_yes=False):
    """A MessageBoxW question. Returns True for yes.

    Plain Win32 instead of tkinter so the uninstaller build can leave Tcl/Tk
    out of the bundle entirely, which keeps it small.
    """
    if silent:
        return default_yes
    flags = 0x04 if yes_no else 0x01              # MB_YESNO / MB_OKCANCEL
    flags |= 0x20 | 0x100                          # MB_ICONQUESTION | MB_DEFBUTTON2
    ret = ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    return ret in (6, 1)                           # IDYES or IDOK


def run_uninstaller(silent=False):
    """Uninstall with a minimal confirmation flow."""
    install_dir = find_install_dir()
    if not is_install_dir(install_dir):
        if not silent:
            ctypes.windll.user32.MessageBoxW(
                None, "没有找到 %s 的安装信息。" % APP_DISPLAY,
                "卸载 %s" % APP_DISPLAY, 0x30)
        return 1

    # 只有**这个安装目录里的**程序在运行才算冲突。按进程名匹配会把
    # 用户从别处运行的便携副本也算进来，从而拒绝一次合法的卸载。
    if install_dir_in_use(install_dir):
        if not silent:
            ctypes.windll.user32.MessageBoxW(
                None,
                "%s 正在运行。\n\n请先退出程序，再重新运行卸载程序。\n"
                "（卸载程序不会强行结束它。）" % APP_DISPLAY,
                "无法卸载", 0x30)
        return 1

    if not ask_yes_no("卸载 %s" % APP_DISPLAY,
                      "要从你的电脑上卸载 %s 吗？\n\n安装位置：\n%s\n\n"
                      "你的账号与用量数据默认会保留。"
                      % (APP_DISPLAY, install_dir), silent=silent,
                      default_yes=True):
        return 0

    remove_data = ask_yes_no(
        "删除用户数据",
        "是否同时删除账号与用量数据？\n\n%s\n\n"
        "选择“是”会一并删除，且无法恢复；\n"
        "选择“否”只卸载程序，数据保留。" % appdata_data_dir(),
        silent=silent, default_yes=False)

    lines = []
    result = do_uninstall(install_dir, remove_data=remove_data,
                          log=lines.append)
    text = "\n".join(lines)

    # do_uninstall 已经安排好延迟删除；这里只如实告诉用户目录会消失。
    if result["leftover"]:
        text += "\n\n程序目录将在本窗口关闭后自动删除。"

    if not silent:
        ctypes.windll.user32.MessageBoxW(
            None,
            "%s\n\n%s 已卸载。" % (text, APP_DISPLAY),
            "卸载完成" if not result["leftover"] else "卸载即将完成", 0x40)
    else:
        print(text)
    return 0


# ==========================================================================
# 命令行
# ==========================================================================

def is_uninstaller_build():
    """True when this process *is* the uninstaller.

    Add/Remove Programs registers ``"<install dir>\\uninstall.exe"`` with no
    arguments, and a user double-clicking the file passes none either. So the
    program cannot rely on ``--uninstall`` being present: without this check
    the uninstaller would fall through to the install branch and report
    "installation package is damaged" - which is exactly what a first build
    of this file did.

    The executable's own name is the signal, which is how Windows
    uninstallers conventionally behave. ``--install`` overrides it for the
    rare case of a renamed binary.
    """
    if not getattr(sys, "frozen", False):
        return False
    stem = os.path.splitext(os.path.basename(sys.executable))[0].lower()
    return stem.startswith("uninstall")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="installer",
        description="%s 安装 / 卸载程序" % APP_DISPLAY)
    parser.add_argument("--uninstall", action="store_true",
                        help="卸载而不是安装")
    parser.add_argument("--install", action="store_true",
                        help="强制进入安装分支（即使可执行文件叫 uninstall）")
    parser.add_argument("--silent", action="store_true",
                        help="不显示界面；安装用默认选项，卸载不再询问")
    parser.add_argument("--dir", default="", help="安装位置")
    parser.add_argument("--all-users", action="store_true", default=None,
                        help="为所有用户安装（写入 Program Files）")
    parser.add_argument("--desktop", action="store_true",
                        help="创建桌面快捷方式（静默安装时）")
    parser.add_argument("--startmenu", action="store_true",
                        help="创建开始菜单快捷方式（静默安装时）")
    parser.add_argument("--no-desktop", action="store_true",
                        help="静默安装时不创建桌面快捷方式")
    parser.add_argument("--no-startmenu", action="store_true",
                        help="静默安装时不创建开始菜单快捷方式")
    parser.add_argument("--launch", action="store_true",
                        help="安装完成后启动程序")
    parser.add_argument("--payload", default="",
                        help="指定载荷 zip（开发调试用）")
    parser.add_argument("--version", default="", help="覆盖记录到注册表的版本号")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    # No-argument launches happen two ways - Add/Remove Programs and a plain
    # double-click - so the build's own name decides. ``--install`` opts out.
    if args.uninstall or (is_uninstaller_build() and not args.install):
        return run_uninstaller(silent=args.silent)

    archive = args.payload or payload_path()
    if not archive or not os.path.exists(archive):
        message = ("安装包损坏：没有找到程序文件（payload.zip）。\n"
                   "请重新下载安装程序。")
        if args.silent:
            print(message)
        else:
            ctypes.windll.user32.MessageBoxW(
                None, message, "无法安装", 0x10)
        return 1

    version = args.version or payload_version(archive) or DEFAULT_VERSION

    # 目标路径的优先级：显式 --dir > 检测到的已有安装 > 默认目录。
    # 检测放在前面，双击安装包默认就是"更新已有的那一份"，而不是在别处
    # 又装一份 —— 用户机器上两个实例都跑 8787 时那是纯粹的麻烦。
    #
    # --all-users 用 None 作默认值，好把"用户没表态"和"用户明确选了仅为我"
    # 区分开：只有没表态时才让检测到的安装模式说了算。
    detected = []
    if not args.dir:
        detected = detect_installations()
    if detected and args.all_users is None:
        args.all_users = bool(detected[0]["all_users"])
    args.all_users = bool(args.all_users)
    target = args.dir or (detected[0]["path"] if detected
                          else default_dir(args.all_users))

    if args.silent:
        # 静默安装：勾选项显式给出才算数，避免自动化里凭空多出快捷方式。
        desktop = bool(args.desktop) and not args.no_desktop
        startmenu = bool(args.startmenu) and not args.no_startmenu
        try:
            result = do_install(archive, target, version, args.all_users,
                                desktop_icon=desktop,
                                startmenu_icon=startmenu)
        except Exception as exc:
            print("安装失败：%s" % exc)
            return 1
        print("安装完成：%s" % result["install_dir"])
        if args.launch:
            launch_app(result["install_dir"])
        return 0

    return run_wizard(archive, version, start_dir=args.dir or None,
                      all_users=args.all_users,
                      desktop_icon=not args.no_desktop,
                      startmenu_icon=not args.no_startmenu,
                      launch=args.launch,
                      installations=detected or None)


if __name__ == "__main__":
    sys.exit(main())
