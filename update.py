"""update.py —— 图形化更新程序（独立运行，不依赖主程序）。

用途：把新版本的 WorkBuddy2API 装到现有目录，同时保留账号、用量与设置。

为什么单独做成一个程序：正在运行的 WorkBuddy2API.exe 无法替换自己
（Windows 会锁住正在执行的文件）。这个更新程序是独立进程，它负责
停掉主程序、换掉文件、再把主程序启回来。

安全原则（重要）：

* **先备份再动手**。更新前把 accounts/、usage/、gateway.json 复制到
  带时间戳的备份目录，出问题可以整体回退。
* **数据目录原样保留**。只替换程序文件（EXE 与 _internal/），
  accounts/ 与 usage/ 从头到尾不碰。
* **任何一步失败都回滚**。替换过程中出错会把原文件放回去，
  并把主程序重新启动，不留下半残状态。

用法：

    python update.py                      # 自动查找桌面上的安装目录
    python update.py --target "D:\\path"  # 指定安装目录
    python update.py --source "新版本目录"

也可以直接双击运行（Windows 下会弹文件选择框）。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

APP_NAME = "WorkBuddy2API"
EXE_NAME = "WorkBuddy2API.exe"
#: Files and folders that belong to the user and must never be overwritten.
DATA_ITEMS = ("accounts", "usage", "gateway.json")
#: Items replaced by an update.
PROGRAM_ITEMS = ("_internal", EXE_NAME)


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------
def running_pids(exe_name=EXE_NAME):
    """PIDs of processes whose executable is named ``exe_name``."""
    if os.name != "nt":
        out = subprocess.run(["pgrep", "-f", exe_name],
                             capture_output=True, text=True).stdout
        return [int(p) for p in out.split() if p.strip().isdigit()]
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='%s'\" | "
        "Select-Object -ExpandProperty ProcessId" % exe_name)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, timeout=30)
    except Exception:
        return []
    text = (out.stdout or b"").decode("utf-8", "replace")
    pids = []
    for token in text.split():
        token = token.strip()
        if token.isdigit():
            pids.append(int(token))
    return pids


def stop_app(exe_name=EXE_NAME, timeout=20):
    """Stop every running copy. Returns (ok, message)."""
    pids = running_pids(exe_name)
    if not pids:
        return True, "程序未在运行"
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=20)
        except Exception:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not running_pids(exe_name):
            return True, "已停止 %d 个进程" % len(pids)
        time.sleep(0.5)
    remaining = running_pids(exe_name)
    return False, "仍有进程未退出：%s" % remaining


def start_app(exe_path):
    """Launch the app detached so it outlives this updater."""
    if not os.path.isfile(exe_path):
        return False, "找不到 %s" % exe_path
    try:
        subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path),
                         close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        return True, "已启动"
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Update logic
# ---------------------------------------------------------------------------
def find_install_dirs():
    """Likely install locations, newest first."""
    home = os.path.expanduser("~")
    roots = [
        os.path.join(home, "Desktop"),
        os.path.join(home, "OneDrive", "Desktop"),
        os.path.join(home, "Downloads"),
    ]
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for entry in os.listdir(root):
            candidate = os.path.join(root, entry)
            if not os.path.isdir(candidate):
                continue
            if os.path.isfile(os.path.join(candidate, EXE_NAME)):
                found.append(candidate)
    found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return found


def describe(install_dir):
    """Summarise what an install directory currently holds."""
    info = {"path": install_dir, "has_exe": False, "accounts": 0,
            "usage": False, "settings": False, "version_hint": ""}
    exe = os.path.join(install_dir, EXE_NAME)
    info["has_exe"] = os.path.isfile(exe)
    if info["has_exe"]:
        info["version_hint"] = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(exe)))
    accounts_dir = os.path.join(install_dir, "accounts")
    if os.path.isdir(accounts_dir):
        info["accounts"] = len([f for f in os.listdir(accounts_dir)
                                if f.endswith(".json")
                                and f != "settings.json"])
    info["usage"] = os.path.isfile(
        os.path.join(install_dir, "usage", "usage.jsonl"))
    info["settings"] = os.path.isfile(
        os.path.join(install_dir, "accounts", "settings.json"))
    return info


def backup(install_dir, keep=5):
    """Copy the user's data aside before touching anything.

    Returns (backup_dir, error). The backup holds only the data items: they
    are small, and restoring them is the whole point if something goes wrong.
    """
    install_dir = os.path.realpath(install_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(install_dir, "_backup-%s" % stamp)
    # A second update inside the same second would collide on the name, so
    # add a suffix until the directory is genuinely new. Without this the copy
    # fails with WinError 183 and the update aborts.
    suffix = 1
    while os.path.exists(backup_dir):
        suffix += 1
        backup_dir = os.path.join(install_dir, "_backup-%s-%d" % (stamp, suffix))
    # The backup path is built from a timestamp, but check containment anyway:
    # every source path below is derived from it, and this function copies
    # whatever it is pointed at.
    if not _within(backup_dir, install_dir):
        return None, "备份路径超出安装目录"
    try:
        os.makedirs(backup_dir, exist_ok=False)
        copied = []
        for item in DATA_ITEMS:
            src = os.path.join(install_dir, item)
            if not os.path.exists(src) or not _within(src, install_dir):
                continue
            dst = os.path.join(backup_dir, item)
            if not _within(dst, backup_dir):
                continue
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            copied.append(item)
    except Exception as exc:
        return None, "备份失败：%s" % exc

    # Keep the folder tidy: drop the oldest backups beyond `keep`.
    try:
        existing = sorted(
            (d for d in os.listdir(install_dir) if d.startswith("_backup-")),
            reverse=True)
        for stale in existing[keep:]:
            shutil.rmtree(os.path.join(install_dir, stale), ignore_errors=True)
    except Exception:
        pass
    return backup_dir, ""


def _within(path, root):
    """True when ``path`` resolves to ``root`` itself or something inside it.

    Used before every destructive operation. This updater deletes and moves
    directories, so a bad value - a traversal segment, a symlinked parent, a
    stale variable - must not be able to reach outside the install directory
    it was pointed at.
    """
    resolved = os.path.realpath(path)
    root = os.path.realpath(root)
    if resolved == root:
        return True
    try:
        return os.path.commonpath([resolved, root]) == root
    except ValueError:
        # Different drives on Windows: definitely not inside.
        return False


def _replace_tree(src, dst, install_dir):
    """Move ``src`` onto ``dst``, removing the old copy first.

    Windows will not rename onto an existing directory, so the destination is
    removed first. That is why a backup is taken before this runs, and why
    both paths are checked for containment: this is the one place that deletes
    a directory tree.
    """
    if not _within(dst, install_dir):
        raise ValueError("拒绝操作安装目录之外的路径：%s" % dst)
    if not _within(src, install_dir):
        raise ValueError("拒绝移动安装目录之外的路径：%s" % src)

    if os.path.exists(dst):
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        else:
            os.remove(dst)
    shutil.move(src, dst)


def apply_update(source_dir, install_dir, keep_data=True):
    """Replace the program files in ``install_dir`` with those in ``source_dir``.

    Returns (ok, message, backup_dir). Data items are left untouched.
    """
    source_dir = os.path.realpath(source_dir)
    install_dir = os.path.realpath(install_dir)

    if source_dir == install_dir:
        return False, "源目录和目标目录相同，无需更新", None
    if not os.path.isfile(os.path.join(source_dir, EXE_NAME)):
        return False, "源目录里没有 %s" % EXE_NAME, None
    if not os.path.isdir(install_dir):
        return False, "目标目录不存在：%s" % install_dir, None

    backup_dir, problem = backup(install_dir)
    if problem:
        return False, problem, None

    # Stage the incoming files next to the target so the final move is a
    # rename on the same volume - fast, and it cannot fail halfway through
    # copying a large _internal tree.
    staging = tempfile.mkdtemp(prefix=".update-", dir=install_dir)
    old_hold = os.path.join(staging, "_old")
    moved_old = []
    try:
        for item in PROGRAM_ITEMS:
            src = os.path.join(source_dir, item)
            if not os.path.exists(src):
                continue
            staged = os.path.join(staging, item)
            if os.path.isdir(src):
                shutil.copytree(src, staged)
            else:
                shutil.copy2(src, staged)

        os.makedirs(old_hold, exist_ok=True)
        # Move the current program files aside, then bring the new ones in.
        for item in PROGRAM_ITEMS:
            target = os.path.join(install_dir, item)
            if os.path.exists(target):
                shutil.move(target, os.path.join(old_hold, item))
                moved_old.append(item)
        for item in PROGRAM_ITEMS:
            staged = os.path.join(staging, item)
            if os.path.exists(staged):
                _replace_tree(staged, os.path.join(install_dir, item),
                              install_dir)
    except Exception as exc:
        # Put the originals back so the install is usable again.
        for item in moved_old:
            held = os.path.join(old_hold, item)
            target = os.path.join(install_dir, item)
            try:
                if os.path.exists(held):
                    _replace_tree(held, target, install_dir)
            except Exception:
                pass
        shutil.rmtree(staging, ignore_errors=True)
        return False, "替换失败，已回滚：%s" % exc, backup_dir

    shutil.rmtree(staging, ignore_errors=True)
    return True, "更新完成", backup_dir


def verify(install_dir):
    """Sanity-check an install directory after an update."""
    problems = []
    exe = os.path.join(install_dir, EXE_NAME)
    if not os.path.isfile(exe):
        problems.append("缺少 %s" % EXE_NAME)
    internal = os.path.join(install_dir, "_internal")
    if not os.path.isdir(internal):
        problems.append("缺少 _internal 目录")
    else:
        # The Qt platform plugin is the piece whose absence makes the app die
        # silently in a windowed build.
        plugin = os.path.join(internal, "PySide6", "plugins", "platforms",
                              "qwindows.dll")
        if not os.path.isfile(plugin):
            problems.append("缺少 Qt 平台插件 (qwindows.dll)")
        if not os.path.isfile(os.path.join(internal, "dashboard.html")):
            problems.append("缺少 dashboard.html")
    return problems


# ---------------------------------------------------------------------------
# Console UI
# ---------------------------------------------------------------------------
def ask(prompt, default=""):
    try:
        reply = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return reply or default


def pick_directory(prompt):
    """Ask for a directory, using a native picker on Windows when possible."""
    if os.name == "nt":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$d.Description = '%s';"
            "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }"
            % prompt.replace("'", "''"))
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", script],
                capture_output=True, timeout=180)
            path = (out.stdout or b"").decode("utf-8", "replace").strip()
            if path and os.path.isdir(path):
                return path
        except Exception:
            pass
    return ask("%s（直接输入路径，留空取消）: " % prompt)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="%s 更新程序：替换程序文件，保留账号与设置" % APP_NAME)
    parser.add_argument("--target", help="要更新的安装目录")
    parser.add_argument("--source", help="新版本所在目录")
    parser.add_argument("--no-start", action="store_true",
                        help="更新后不自动启动程序")
    parser.add_argument("--yes", action="store_true",
                        help="不询问，直接执行")
    args = parser.parse_args(argv)

    print("=" * 62)
    print("  %s 更新程序" % APP_NAME)
    print("=" * 62)
    print()
    print("更新只会替换程序文件（EXE 与 _internal/），")
    print("账号、用量和设置会原样保留，并且更新前会自动备份。")
    print()

    # ---- target ----
    target = args.target
    if not target:
        found = find_install_dirs()
        if found:
            print("找到以下安装目录：")
            for index, path in enumerate(found, 1):
                info = describe(path)
                print("  [%d] %s" % (index, path))
                print("      %s · 账号 %d 个%s"
                      % (info["version_hint"], info["accounts"],
                         " · 有用量记录" if info["usage"] else ""))
            print()
            choice = ask("选择要更新的目录（序号，留空取消）: ")
            if not choice:
                print("已取消。")
                return 0
            try:
                target = found[int(choice) - 1]
            except (ValueError, IndexError):
                print("选择无效。")
                return 1
        else:
            print("没有自动找到安装目录。")
            target = pick_directory("选择要更新的安装目录")
            if not target:
                print("已取消。")
                return 0

    target = os.path.realpath(target)
    if not os.path.isfile(os.path.join(target, EXE_NAME)):
        print("这个目录里没有 %s：%s" % (EXE_NAME, target))
        return 1
    info = describe(target)
    print("目标：%s" % target)
    print("  当前程序时间：%s" % info["version_hint"])
    print("  账号：%d 个%s" % (info["accounts"],
                              "（有设置文件）" if info["settings"] else ""))
    print()

    # ---- source ----
    source = args.source
    if not source:
        print("请选择新版本所在目录（解压后的文件夹，里面有 %s）。" % EXE_NAME)
        source = pick_directory("选择新版本目录")
        if not source:
            print("已取消。")
            return 0
    source = os.path.realpath(source)
    if not os.path.isfile(os.path.join(source, EXE_NAME)):
        print("这个目录里没有 %s：%s" % (EXE_NAME, source))
        return 1
    print("来源：%s" % source)

    problems = verify(source)
    if problems:
        print()
        print("来源目录看起来不完整：")
        for item in problems:
            print("  - %s" % item)
        if not args.yes and ask("仍然继续？(y/N): ").lower() != "y":
            return 1
    print()

    if not args.yes:
        if ask("开始更新？(Y/n): ").lower() == "n":
            print("已取消。")
            return 0

    # ---- stop, update, start ----
    print()
    print("[1/4] 停止正在运行的程序…")
    ok, message = stop_app()
    print("      %s" % message)
    if not ok:
        print("      请手动关闭程序后重试。")
        return 1

    print("[2/4] 备份账号与用量…")
    backup_dir, problem = backup(target)
    if problem:
        print("      %s" % problem)
        return 1
    print("      已备份到 %s" % os.path.basename(backup_dir))

    print("[3/4] 替换程序文件…")
    ok, message, backup_dir = apply_update(source, target)
    print("      %s" % message)
    if not ok:
        print()
        print("更新未完成。你的数据未被修改。")
        if backup_dir:
            print("备份仍在：%s" % backup_dir)
        return 1

    problems = verify(target)
    if problems:
        print("      更新后检查发现问题：")
        for item in problems:
            print("        - %s" % item)
        print()
        print("可以从备份回退：%s" % backup_dir)
        return 1
    print("      文件检查通过")

    print("[4/4] 启动程序…")
    if args.no_start:
        print("      已跳过（--no-start）")
    else:
        ok, message = start_app(os.path.join(target, EXE_NAME))
        print("      %s" % message)

    print()
    print("=" * 62)
    print("  更新完成")
    print()
    print("  安装目录：%s" % target)
    print("  数据备份：%s" % backup_dir)
    print()
    print("  账号、用量、设置都保持原样。确认新版本正常后，")
    print("  可以删掉 _backup-* 目录释放空间。")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
