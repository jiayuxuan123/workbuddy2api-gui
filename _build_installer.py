#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""_build_installer.py —— 构建安装器与卸载器。

产出：

    dist/payload.zip                      安装载荷（程序目录）
    dist/uninstaller/uninstall.exe        卸载程序
    dist/WorkBuddy2API-<版本>-setup.exe   安装向导（单文件）

步骤：
  1. 读取 wb_proxy.APP_VERSION 作为版本号；
  2. 用 dist/WorkBuddy2API/ 打一个 payload.zip（含 payload.json 记录版本）；
  3. 用 uninstaller.spec 构建 uninstall.exe；
  4. 用 installer.spec 构建 setup.exe（把 payload.zip、uninstall.exe、
     app.ico 一并打进去）。

前置条件：先跑过主程序打包，即 dist/WorkBuddy2API/WorkBuddy2API.exe 存在。

    python -X utf8 _build_installer.py
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
APPDIR = os.path.join(DIST, "WorkBuddy2API")
PAYLOAD = os.path.join(DIST, "payload.zip")
UNINST_DIR = os.path.join(DIST, "uninstaller")

#: 数据目录绝不进载荷 —— 那是用户自己的账号与用量。
FORBIDDEN = ("accounts", "usage", "gateway.json", "install.json")


def version():
    src = open(os.path.join(HERE, "wb_proxy.py"), encoding="utf-8").read()
    found = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', src)
    if not found:
        raise SystemExit("wb_proxy.py 里找不到 APP_VERSION")
    return found.group(1)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human(size):
    return "%.1f MB (%d bytes)" % (size / 1048576.0, size)


def check_inputs():
    exe = os.path.join(APPDIR, "WorkBuddy2API.exe")
    if not os.path.isfile(exe):
        raise SystemExit(
            "缺少主程序：%s\n先运行打包（pyinstaller workbuddy2api.spec）。" % exe)
    internal = os.path.join(APPDIR, "_internal")
    if not os.path.isdir(internal):
        raise SystemExit("缺少 _internal/ 目录，程序无法独立运行：%s" % internal)
    for bad in FORBIDDEN:
        if os.path.exists(os.path.join(APPDIR, bad)):
            raise SystemExit("载荷里出现了数据项 %s，必须移除后再打包" % bad)
    icon = os.path.join(HERE, "app.ico")
    if not os.path.isfile(icon):
        raise SystemExit("缺少 app.ico，先运行 python -X utf8 _make_icon.py")
    return exe


def build_payload(ver):
    """Zip the program directory, recording the version inside."""
    if os.path.exists(PAYLOAD):
        os.remove(PAYLOAD)
    count = 0
    with zipfile.ZipFile(PAYLOAD, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=6) as z:
        z.writestr("payload.json", json.dumps(
            {"version": ver, "app": "WorkBuddy2API",
             "built_at": time.strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False, indent=2))
        count += 1
        for doc in ("README.md", "LICENSE"):
            path = os.path.join(HERE, doc)
            if os.path.isfile(path):
                z.write(path, doc)
                count += 1
        for root, dirs, files in os.walk(APPDIR):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, APPDIR).replace("\\", "/")
                z.write(full, rel)
                count += 1
    print("  payload.zip: %d 个条目  %s" % (count, human(os.path.getsize(PAYLOAD))))
    return count


def run_pyinstaller(spec, label):
    """Invoke PyInstaller on ``spec``; returns True on success."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["WB_SETUP_VERSION"] = version()
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm",
           "--distpath", DIST, "--workpath", os.path.join(HERE, "build"),
           os.path.join(HERE, spec)]
    print("\n[%s] %s" % (label, " ".join(cmd)))
    proc = subprocess.run(cmd, cwd=HERE, env=env,
                          capture_output=True, text=True, errors="replace")
    tail = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()][-12:]
    for line in tail:
        print("    %s" % line)
    if proc.returncode != 0:
        print("\n!! %s 构建失败 (exit %d)" % (label, proc.returncode))
        for line in (proc.stderr or "").splitlines()[-25:]:
            print("    %s" % line)
        return False
    return True


def main():
    ver = version()
    print("=== 构建安装器 v%s ===" % ver)
    check_inputs()

    print("\n[1/3] 打载荷")
    build_payload(ver)

    print("\n[2/3] 构建卸载程序")
    # 卸载器必须由 uninstaller.spec 产出，且要先放进 dist/uninstaller/，
    # 因为 installer.spec 会把 dist/uninstaller/uninstall.exe 打进去。
    if not run_pyinstaller("uninstaller.spec", "uninstaller"):
        return 1
    built = os.path.join(DIST, "uninstall.exe")
    if not os.path.isfile(built):
        print("!! 没有找到 %s" % built)
        return 1
    os.makedirs(UNINST_DIR, exist_ok=True)
    target = os.path.join(UNINST_DIR, "uninstall.exe")
    shutil.copy2(built, target)
    print("  卸载程序: %s  %s" % (target, human(os.path.getsize(target))))

    # installer.spec 在 spec 目录里找 uninstall.exe，这里放一份。
    shutil.copy2(built, os.path.join(HERE, "uninstall.exe"))

    print("\n[3/3] 构建安装向导")
    if not run_pyinstaller("installer.spec", "installer"):
        return 1

    setup = os.path.join(DIST, "WorkBuddy2API-%s-setup.exe" % ver)
    if not os.path.isfile(setup):
        print("!! 没有找到 %s" % setup)
        return 1

    print("\n=== 产出 ===")
    for path in (PAYLOAD, target, setup):
        print("  %-58s %s" % (os.path.basename(path), human(os.path.getsize(path))))
    print("\nsetup.exe sha256 = %s" % sha256(setup))
    print("payload  版本     = %s" % ver)
    return 0


if __name__ == "__main__":
    sys.exit(main())
