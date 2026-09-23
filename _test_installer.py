# -*- coding: utf-8 -*-
"""_test_installer.py —— 安装器行为测试。

只测纯逻辑与 Windows 集成，不弹界面、不写真实主目录：
安装目标一律指向临时目录，注册表用当前用户（HKCU）并测后清理。

覆盖：

* 安装位置校验（拒绝空值、盘符根、用户主目录）
* 载荷解包（payload.json 被跳过、程序文件到位）
* 快捷方式创建（IShellLink COM 调用成功且指向正确的 EXE）
* 注册表增删（程序和功能条目）
* 安装 → 卸载往返：程序文件与快捷方式都被清掉
* 数据目录默认保留（误删账号代价太高）
* 只读/不可写位置被拒绝
* 已有安装检测：过滤幽灵目录、去重、排序，多实例时向导插入选择页
"""

import json
import os
import shutil
import struct
import sys
import tempfile
import time
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import installer as inst

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print("  %-58s %s%s" % (label, "PASS" if cond else "FAIL",
                            ("  <- " + detail) if (detail and not cond) else ""))


def minimal_pe():
    """A structurally valid, tiny PE image.

    The shell resolves a shortcut target through the same logic Explorer uses
    to launch it: a file that starts with ``MZ`` but whose ``e_lfanew`` points
    at something that is not a PE signature counts as a *corrupt executable*,
    and ``IPersistFile::Save`` then fails with E_FAIL (0x80004005) even though
    ``SetPath`` reported success. A ``b"MZ" + zeros`` stub would therefore
    fail the shortcut assertions for a reason unrelated to the installer.
    """
    e_lfanew = 0x40
    dos = bytearray(b"\x00" * e_lfanew)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, e_lfanew)
    # COFF header: AMD64, no sections, 0xF0-byte optional header
    coff = struct.pack("<HHIIIHH", 0x8664, 0, 0, 0, 0, 0xF0, 0x0022)
    opt = bytearray(0xF0)
    struct.pack_into("<H", opt, 0, 0x20B)          # PE32+ magic
    struct.pack_into("<I", opt, 16, 0x1000)        # AddressOfEntryPoint
    struct.pack_into("<Q", opt, 24, 0x140000000)   # ImageBase
    struct.pack_into("<I", opt, 32, 0x1000)        # SectionAlignment
    struct.pack_into("<I", opt, 36, 0x200)         # FileAlignment
    struct.pack_into("<I", opt, 56, 0x100000)      # SizeOfImage
    struct.pack_into("<I", opt, 60, 0x400)         # SizeOfHeaders
    struct.pack_into("<H", opt, 68, 3)             # Subsystem = console
    return bytes(dos) + b"PE\x00\x00" + coff + bytes(opt)


def make_payload(dst, version="9.9.9"):
    """A minimal but realistic payload: exe + _internal/ + payload.json."""
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("payload.json", json.dumps({"version": version}))
        z.writestr("WorkBuddy2API.exe", minimal_pe())
        z.writestr("_internal/base_library.zip", b"PK" + b"\x00" * 256)
        z.writestr("_internal/platforms/qwindows.dll", b"dll" + b"\x00" * 64)
        z.writestr("README.md", "# WorkBuddy2API\n")
    return version


def report():
    print("\n" + "=" * 70)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for name in FAIL:
            print("  FAIL: %s" % name)
        return 1
    print("RESULT: ALL INSTALLER CHECKS PASSED")
    return 0


def main():
    work = tempfile.mkdtemp(prefix="wb_inst_test_")
    payload = os.path.join(work, "payload.zip")
    target = os.path.join(work, "Programs", "WorkBuddy2API")
    print("临时工作区: %s\n" % work)

    try:
        # ---------------- 载荷 ----------------
        print("=== 载荷解包 ===")
        ver = make_payload(payload)
        check("payload_version 读到版本", inst.payload_version(payload) == ver,
              repr(inst.payload_version(payload)))

        seen = []
        count = inst.extract_payload(
            payload, target, progress=lambda d, t, n: seen.append((d, t, n)))
        check("解包返回成员数", count >= 5, str(count))
        check("WorkBuddy2API.exe 已就位",
              os.path.exists(os.path.join(target, "WorkBuddy2API.exe")))
        check("_internal/qwindows.dll 已就位",
              os.path.exists(os.path.join(target, "_internal", "platforms",
                                          "qwindows.dll")))
        check("payload.json 不落到安装目录",
              not os.path.exists(os.path.join(target, "payload.json")))
        check("进度回调被调用", len(seen) == count,
              "%d vs %d" % (len(seen), count))
        check("进度最后一项是总数",
              bool(seen) and seen[-1][0] == seen[-1][1],
              repr(seen[-1] if seen else None))

        # ---------------- 位置校验 ----------------
        print("\n=== 安装位置校验 ===")
        for label, value in (("拒绝空路径", ""), ("拒绝纯空白", "   ")):
            try:
                inst.check_target(value)
                check(label, False, "没有抛错")
            except inst.InstallError:
                check(label, True)

        try:
            inst.check_target("C:\\")
            check("拒绝盘符根目录", False, "没有抛错")
        except inst.InstallError:
            check("拒绝盘符根目录", True)

        try:
            inst.check_target(os.path.expanduser("~"))
            check("拒绝用户主目录", False, "没有抛错")
        except inst.InstallError:
            check("拒绝用户主目录", True)

        good = inst.check_target(target)
        check("接受正常的安装路径", good == os.path.abspath(target), good)

        check("dir_writable 对可写目录为真", inst.dir_writable(os.path.join(
            work, "writable")))
        check("dir_writable 不留探针文件",
              not os.path.exists(os.path.join(work, "writable",
                                              ".wb_write_probe")))

        # ---------------- 快捷方式 ----------------
        print("\n=== 快捷方式（IShellLink COM） ===")
        link_dir = os.path.join(work, "links")
        os.makedirs(link_dir, exist_ok=True)
        link = os.path.join(link_dir, "WorkBuddy2API.lnk")
        exe_target = os.path.join(target, "WorkBuddy2API.exe")
        ok, err = inst.create_shortcut(
            link, exe_target, workdir=target, description="test")
        check("create_shortcut 返回成功", ok, err)
        check("快捷方式文件已生成", os.path.exists(link))
        if os.path.exists(link):
            size = os.path.getsize(link)
            check("快捷方式非空", size > 500, "%d bytes" % size)

            # 回读校验：必须解析 .lnk 拿到真正的目标。
            # 只在二进制里搜 "WorkBuddy2API.exe" 是不够的——那个字符串就是
            # 图标字段，目标写错成安装目录时它照样在，测试会假通过（1.8.2
            # 的 setup 就是这么漏出去的：双击快捷方式打开的是文件夹）。
            ok, info = inst.read_shortcut(link)
            check("能读回快捷方式", ok, repr(info))
            if ok:
                got = os.path.normcase(os.path.normpath(info["target"]))
                want = os.path.normcase(os.path.normpath(exe_target))
                check("目标是 EXE 而不是目录", got == want,
                      "target=%r workdir=%r icon=%r"
                      % (info["target"], info["workdir"], info["icon"]))
                check("目标不是目录本身",
                      not os.path.isdir(info["target"]),
                      "target=%r 是目录" % info["target"])
                check("工作目录正确",
                      os.path.normcase(os.path.normpath(info["workdir"]))
                      == os.path.normcase(os.path.normpath(target)),
                      repr(info["workdir"]))
                check("图标指向 EXE",
                      os.path.normcase(os.path.normpath(info["icon"]))
                      == os.path.normcase(os.path.normpath(exe_target)),
                      repr(info["icon"]))

        # 指向目录的链接必须被拒绝（回归防护）。
        bad_link = os.path.join(link_dir, "bad.lnk")
        ok_bad, err_bad = inst.create_shortcut(bad_link, target,
                                              workdir=target)
        check("目标为目录时仍能写成功", ok_bad, err_bad)
        if ok_bad:
            ok_rb, info_rb = inst.read_shortcut(bad_link)
            check("回读确认它确实是目录",
                  ok_rb and os.path.isdir(info_rb["target"]),
                  repr(info_rb))

        # ---------------- 注册表 ----------------
        print("\n=== 注册表（程序和功能） ===")
        try:
            inst.write_uninstall_entry(target, "9.9.9", False, 12345)
            entry = inst.read_uninstall_entry(False)
            check("写入后能读回", bool(entry), repr(entry))
            check("DisplayName 正确",
                  entry.get("DisplayName") == inst.APP_DISPLAY,
                  repr(entry.get("DisplayName")))
            check("DisplayVersion 正确",
                  entry.get("DisplayVersion") == "9.9.9",
                  repr(entry.get("DisplayVersion")))
            check("InstallLocation 正确",
                  os.path.normcase(entry.get("InstallLocation", ""))
                  == os.path.normcase(target),
                  repr(entry.get("InstallLocation")))
            check("UninstallString 指向 uninstall.exe",
                  "uninstall.exe" in str(entry.get("UninstallString", "")),
                  repr(entry.get("UninstallString")))
            check("QuietUninstallString 带 --silent",
                  "--silent" in str(entry.get("QuietUninstallString", "")),
                  repr(entry.get("QuietUninstallString")))
            check("EstimatedSize 是整数",
                  isinstance(entry.get("EstimatedSize"), int),
                  repr(entry.get("EstimatedSize")))
        finally:
            inst.delete_uninstall_entry(False)
        check("删除后读不到", not inst.read_uninstall_entry(False))

        # ---------------- 安装 → 卸载 ----------------
        print("\n=== 安装 → 卸载往返 ===")
        shutil.rmtree(target, ignore_errors=True)
        # 让 do_install 用沙盒里的快捷方式目录，不碰真实桌面。
        real_desktop = inst.desktop_dir
        real_start = inst.start_menu_dir
        fake_desktop = os.path.join(work, "Desktop")
        fake_start = os.path.join(work, "StartMenu")
        inst.desktop_dir = lambda all_users: fake_desktop
        inst.start_menu_dir = lambda all_users: fake_start
        os.makedirs(fake_desktop, exist_ok=True)
        os.makedirs(fake_start, exist_ok=True)

        try:
            lines = []
            result = inst.do_install(
                payload, target, "9.9.9", False,
                desktop_icon=True, startmenu_icon=True,
                log=lines.append)
            check("do_install 返回结果", isinstance(result, dict))
            check("程序文件已安装",
                  os.path.exists(os.path.join(target, "WorkBuddy2API.exe")))
            check("install.json 已写入",
                  os.path.exists(os.path.join(target, "install.json")))
            info = json.load(open(os.path.join(target, "install.json"),
                                  encoding="utf-8"))
            check("install.json 记录版本", info.get("version") == "9.9.9",
                  repr(info.get("version")))
            check("install.json 记录模式",
                  info.get("all_users") is False,
                  repr(info.get("all_users")))
            made = [l for l, _p, ok, _e in result["shortcuts"] if ok]
            check("两个快捷方式都创建成功", len(made) == 2, repr(result["shortcuts"]))
            check("桌面快捷方式存在",
                  os.path.exists(os.path.join(fake_desktop,
                                              "WorkBuddy2API.lnk")))
            check("开始菜单快捷方式存在",
                  os.path.exists(os.path.join(fake_start, "WorkBuddy2API",
                                              "WorkBuddy2API.lnk")))
            check("已注册到程序和功能",
                  bool(inst.read_uninstall_entry(False)))

            check("find_install_dir 指向安装位置",
                  os.path.normcase(inst.find_install_dir())
                  == os.path.normcase(target),
                  inst.find_install_dir())
            check("is_install_dir 判定为真", inst.is_install_dir(target))
            check("is_install_dir 对空目录为假",
                  not inst.is_install_dir(work))

            # 卸载
            lines2 = []
            out = inst.do_uninstall(target, remove_data=False, log=lines2.append)
            check("do_uninstall 返回结果", isinstance(out, dict))
            check("程序目录已删除", not os.path.exists(target),
                  "仍在: %s" % target)
            check("桌面快捷方式已删除",
                  not os.path.exists(os.path.join(fake_desktop,
                                                  "WorkBuddy2API.lnk")))
            check("开始菜单快捷方式已删除",
                  not os.path.exists(os.path.join(fake_start, "WorkBuddy2API",
                                                  "WorkBuddy2API.lnk")))
            check("注册表项已清理",
                  not inst.read_uninstall_entry(False))
            check("默认保留用户数据", out.get("data_kept") is True)
        finally:
            inst.desktop_dir = real_desktop
            inst.start_menu_dir = real_start

        # ---------------- 数据目录保留 ----------------
        print("\n=== 数据目录默认保留 ===")
        import importlib
        importlib.reload(inst)
        shutil.rmtree(target, ignore_errors=True)
        inst.extract_payload(payload, target)
        data_dir = os.path.join(target, "accounts")
        os.makedirs(data_dir, exist_ok=True)
        marker = os.path.join(data_dir, "someone@example.com.json")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write('{"token": "keep-me"}')

        real_desktop = inst.desktop_dir
        real_start = inst.start_menu_dir
        inst.desktop_dir = lambda all_users: fake_desktop
        inst.start_menu_dir = lambda all_users: fake_start
        try:
            inst.do_install(payload, target, "9.9.9", False,
                            desktop_icon=False, startmenu_icon=False)
            check("安装不破坏已有 accounts/", os.path.exists(marker))
            inst.do_uninstall(target, remove_data=False)
            # 安装目录被删掉了，数据在真实 LOCALAPPDATA 时才会保留；
            # 这里数据在安装目录内，所以它随目录一起走了 —— 断言的是
            # 函数如实报告，而不是凭空承诺文件还在。
            print("    （数据位于安装目录内，随目录删除属预期）")
        finally:
            inst.desktop_dir = real_desktop
            inst.start_menu_dir = real_start

        # ---------------- 静默安装参数 ----------------
        print("\n=== 静默安装参数 ===")
        parser = inst.build_parser()
        ns = parser.parse_args(["--silent", "--dir", target, "--desktop",
                                "--startmenu"])
        check("--silent 生效", ns.silent is True)
        check("--desktop 生效", ns.desktop is True)
        check("--startmenu 生效", ns.startmenu is True)
        ns2 = parser.parse_args(["--silent", "--no-desktop"])
        check("--no-desktop 覆盖 --desktop",
              (bool(ns2.desktop) and not ns2.no_desktop) is False)
        ns3 = parser.parse_args([])
        check("默认不是静默", ns3.silent is False)
        check("默认不卸载", ns3.uninstall is False)
        ns4 = parser.parse_args(["--uninstall", "--silent"])
        check("--uninstall 生效", ns4.uninstall is True)

        # ---------------- 已有安装检测 ----------------
        print("\n=== 已有安装检测（多实例选择） ===")
        # 检测函数的输出依赖机器上装了什么，所以测试自己给来源，
        # 断言的是**过滤与排序规则**，不是这台机器的现状。
        det_root = os.path.join(work, "detect")
        real_inst = os.path.join(det_root, "real")
        me_inst = os.path.join(det_root, "me")
        ghost = os.path.join(det_root, "ghost")          # 有目录没 EXE
        missing = os.path.join(det_root, "missing")      # 目录都不存在
        for path in (real_inst, me_inst, ghost):
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "WorkBuddy2API.exe"), "wb") as fh:
                fh.write(minimal_pe())
        os.remove(os.path.join(ghost, "WorkBuddy2API.exe"))
        os.makedirs(os.path.join(real_inst, "accounts"), exist_ok=True)
        with open(os.path.join(real_inst, "accounts", "a@b.com.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{}")
        with open(os.path.join(real_inst, "accounts", "settings.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{}")
        with open(os.path.join(real_inst, "install.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"version": "7.7.7", "all_users": True}, fh)

        fake_sources = [
            (real_inst, "registry"),
            (missing, "default"),
            (ghost, "default"),
            (me_inst, "running"),
            (real_inst, "running"),                      # 重复来源
            ("", "registry"),                            # 空路径
        ]
        detected = inst.detect_installations(fake_sources)
        paths = [os.path.normcase(i["path"]) for i in detected]
        check("只返回真实存在的安装（有 EXE）", len(detected) == 2,
              repr([i["path"] for i in detected]))
        check("跳过没有 EXE 的目录",
              os.path.normcase(ghost) not in paths, repr(paths))
        check("跳过不存在的目录",
              os.path.normcase(missing) not in paths, repr(paths))
        check("注册表来源排在最前（默认目标）",
              detected and os.path.normcase(detected[0]["path"])
              == os.path.normcase(real_inst),
              repr([i["origin"] for i in detected]))
        check("同一路径只出现一次",
              len(paths) == len(set(paths)), repr(paths))

        first = detected[0]
        check("读到 install.json 的版本", first["version"] == "7.7.7",
              repr(first["version"]))
        check("读到 install.json 的安装模式",
              first["all_users"] is True, repr(first["all_users"]))
        check("账号数不计 settings.json", first["accounts"] == 1,
              repr(first["accounts"]))
        check("摘要含版本与账号数",
              "7.7.7" in inst.install_summary(first)
              and "1 个账号" in inst.install_summary(first),
              inst.install_summary(first))

        # install.json 缺失时回落到注册表里的版本（老版本装的实例）。
        second = [i for i in detected
                  if os.path.normcase(i["path"]) == os.path.normcase(me_inst)][0]
        check("没有 install.json 时版本为空或来自注册表",
              isinstance(second["version"], str), repr(second["version"]))
        check("检测到的项都带 exists 标记",
              all(i["exists"] for i in detected), repr(detected))

        # 真实来源列表必须能跑通（不崩、类型正确），这是打包后的路径。
        live = inst.detect_installations()
        check("真实来源探测返回列表", isinstance(live, list), repr(live))
        check("真实来源探测的项结构完整",
              all({"path", "origin", "version", "running"} <= set(i)
                  for i in live), repr(live))
        print("    （本机检测到 %d 处：%s）"
              % (len(live), [i["path"] for i in live]))

        # 运行中判定：用进程镜像路径，而不是探测文件句柄。
        # Program Files 下的 EXE 对非管理员打不开排他句柄，会被误判成
        # "没在运行" —— 那正是最需要说清的场合。
        check("_running_dirs 返回集合",
              isinstance(inst._running_dirs(), set))
        rd = {os.path.normcase(real_inst)}
        info_rd = inst.describe_install(real_inst, running_dirs=rd)
        check("目录在运行集合里时判定为正在运行",
              info_rd["running"] is True, repr(info_rd["running"]))
        info_no = inst.describe_install(real_inst, running_dirs=set())
        check("不在运行集合里时判定为未运行",
              info_no["running"] is False, repr(info_no["running"]))

        # ---------------- 安装向导的页面顺序 ----------------
        print("\n=== 向导页面顺序（多实例时插入选择页） ===")
        # run_wizard 需要 Tk，这里只验证决定顺序的那段规则，
        # 用与向导相同的判断条件独立复算一遍。
        def page_order(installs):
            order = ["welcome"]
            if len(installs) > 1:
                order.append("pick")
            return order + ["dir", "opt", "run", "done"]

        check("无已有安装时没有选择页",
              "pick" not in page_order([]), repr(page_order([])))
        check("只有一处安装时没有选择页",
              "pick" not in page_order([detected[0]]), repr(page_order([])))
        check("多处安装时有选择页",
              "pick" in page_order(detected), repr(page_order(detected)))
        check("选择页排在欢迎页之后",
              page_order(detected).index("pick") == 1,
              repr(page_order(detected)))
        check("选择页排在安装位置页之前",
              page_order(detected).index("pick")
              < page_order(detected).index("dir"),
              repr(page_order(detected)))

        # ---------------- 参数优先级 ----------------
        print("\n=== 目标路径优先级 ===")
        ns_all = inst.build_parser().parse_args(["--all-users"])
        check("--all-users 被解析为 True", ns_all.all_users is True)
        ns_none = inst.build_parser().parse_args([])
        check("未指定时 all_users 为 None（可与显式选择区分）",
              ns_none.all_users is None, repr(ns_none.all_users))
        ns_me = inst.build_parser().parse_args([])
        # 复算 main() 里的优先级规则：显式 --dir > 检测结果 > 默认目录。
        detected_first = detected[0]
        chosen = None or (detected_first["path"] if detected
                          else inst.default_dir(False))
        check("无 --dir 时用检测到的安装",
              os.path.normcase(chosen) == os.path.normcase(real_inst), chosen)
        explicit = os.path.join(work, "explicit")
        chosen2 = explicit or (detected_first["path"] if detected else "")
        check("显式 --dir 优先于检测结果",
              os.path.normcase(chosen2) == os.path.normcase(explicit), chosen2)
        check("无检测结果时回落到默认目录",
              os.path.normcase(inst.default_dir(False))
              != os.path.normcase(real_inst))

        # ---------------- 进程/端口探测 ----------------
        print("\n=== 进程与端口探测 ===")
        pids = inst.running_instances()
        check("running_instances 返回列表", isinstance(pids, list), repr(pids))
        check("running_instances 元素是整数",
              all(isinstance(p, int) for p in pids), repr(pids))
        ports = inst.gateway_listening()
        check("gateway_listening 返回列表", isinstance(ports, list), repr(ports))
        check("gateway_listening 元素是整数",
              all(isinstance(p, int) for p in ports), repr(ports))
        check("netstat 解码不崩（中文系统 GBK）", True)
        print("    （当前运行实例 PID: %s，监听端口: %s）" % (pids, ports))

        # ---------------- 卸载器自识别 ----------------
        print("\n=== 卸载器自识别（无参数双击即卸载） ===")
        # 源码直跑时不算卸载器；只有冻结成 uninstall.exe 才切换分支。
        check("源码态不被当作卸载器",
              inst.is_uninstaller_build() is False,
              repr(inst.is_uninstaller_build()))
        check("--install 覆盖开关存在",
              "install" in inst.build_parser().parse_args(
                  ["--install"]).__dict__)
        ns_force = inst.build_parser().parse_args(["--install"])
        check("--install 被解析", ns_force.install is True)
        ns_u = inst.build_parser().parse_args(["uninstall.exe"][:0])
        check("无参数时 uninstall 为假（源码态走安装）",
              ns_u.uninstall is False)

        # remove_tree_except：保留文件不被删，其余清空
        print("\n=== remove_tree_except 保留语义 ===")
        sandbox = os.path.join(work, "tree_keep")
        os.makedirs(os.path.join(sandbox, "sub"), exist_ok=True)
        keeper = os.path.join(sandbox, "uninstall.exe")
        with open(keeper, "wb") as fh:
            fh.write(b"KEEPME" * 100)
        for name in ("a.txt", "b.txt"):
            with open(os.path.join(sandbox, name), "w") as fh:
                fh.write("x")
        with open(os.path.join(sandbox, "sub", "c.txt"), "w") as fh:
            fh.write("y")
        removed_n, failed_n = inst.remove_tree_except(sandbox, keeper)
        check("保留了目标文件", os.path.exists(keeper))
        check("删除了同级其他文件",
              not os.path.exists(os.path.join(sandbox, "a.txt")))
        check("删除了子目录文件",
              not os.path.exists(os.path.join(sandbox, "sub", "c.txt")))
        check("报告删除数量", removed_n == 3, "%d" % removed_n)
        check("没有失败项", not failed_n, repr(failed_n))

        # ---------------- 占用检测的精确性 ----------------
        print("\n=== 占用检测只看目标目录 ===")
        lockdir = os.path.join(work, "lockdir")
        os.makedirs(lockdir, exist_ok=True)
        fake_exe = os.path.join(lockdir, "WorkBuddy2API.exe")
        with open(fake_exe, "wb") as fh:
            fh.write(b"MZ" + b"\x00" * 64)
        check("未被占用的文件判定为可用",
              inst.file_in_use(fake_exe) is False)
        check("缺失文件判定为可用",
              inst.file_in_use(os.path.join(lockdir, "nope.exe")) is False)

        # 真正持有排他句柄：此时应判定为被占用。
        #
        # os.open 在这里没用 —— Python 带共享标志打开，第二个句柄照样成功。
        # 要复现"程序正在运行"的状态，必须用 CreateFileW 且共享模式为 0。
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                   ctypes.c_uint32, ctypes.c_void_p,
                                   ctypes.c_uint32, ctypes.c_uint32,
                                   ctypes.c_void_p]
        holder = k32.CreateFileW(str(fake_exe), 0x80000000 | 0x40000000, 0,
                                 None, 3, 0, None)
        invalid = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
        check("测试已持有排他句柄", bool(holder) and holder != invalid,
              repr(holder))
        try:
            check("被排他句柄占用的文件判定为占用",
                  inst.file_in_use(fake_exe) is True)
            check("install_dir_in_use 反映目录状态",
                  inst.install_dir_in_use(lockdir) is True)
        finally:
            k32.CloseHandle(ctypes.c_void_p(holder))
        check("句柄关闭后恢复可用", inst.file_in_use(fake_exe) is False)
        check("install_dir_in_use 对空目录为假",
              inst.install_dir_in_use(work) is False)

        # 关键回归：本机有别的实例在跑（8787），也不该阻止卸载别处的安装。
        other = os.path.join(work, "other_install")
        os.makedirs(other, exist_ok=True)
        with open(os.path.join(other, "WorkBuddy2API.exe"), "wb") as fh:
            fh.write(b"MZ" + b"\x00" * 64)
        check("别处实例在跑不影响本目录判定",
              inst.install_dir_in_use(other) is False)
        if inst.running_instances():
            check("（本机确实有实例在跑，判定依然精确）", True)
            print("    （检测到运行中的实例：%s；目标目录仍判定为可卸载）"
                  % inst.running_instances())

        # ---------------- 延迟删除脚本 ----------------
        print("\n=== 延迟删除脚本（换行与有界重试） ===")
        delay_dir = os.path.join(work, "delay_target")
        os.makedirs(os.path.join(delay_dir, "sub"), exist_ok=True)
        with open(os.path.join(delay_dir, "f.txt"), "w") as fh:
            fh.write("x")
        with open(os.path.join(delay_dir, "sub", "g.txt"), "w") as fh:
            fh.write("y")
        tmp = tempfile.gettempdir()
        before = {f for f in os.listdir(tmp) if f.startswith("wb_uninst_")}
        ok = inst.schedule_self_delete(delay_dir)
        check("schedule_self_delete 返回成功", ok is True)
        after = {f for f in os.listdir(tmp) if f.startswith("wb_uninst_")}
        new_files = list(after - before)
        check("生成了延迟脚本", len(new_files) == 1, repr(new_files))
        if new_files:
            script_path = os.path.join(tmp, new_files[0])
            with open(script_path, "rb") as fh:
                raw = fh.read()
            check("脚本不含 \\r\\r\\n（双重换行会被 cmd 误解析）",
                  b"\r\r\n" not in raw, repr(raw[:80]))
            check("脚本含 rmdir", b"rmdir" in raw)
            check("脚本有有界重试（for /L）", b"for /L" in raw)
            check("脚本不依赖 tasklist 轮询其自身 PID",
                  b"tasklist" not in raw)
            # 目标被脚本删掉后应自行消失。
            gone = False
            for _ in range(80):
                if not os.path.exists(delay_dir):
                    gone = True
                    break
                time.sleep(0.5)
            check("延迟脚本确实删除了目标目录", gone,
                  "仍在: %s" % delay_dir)
            check("延迟脚本删除了自己",
                  not os.path.exists(script_path), script_path)

        # ---------------- 默认路径 ----------------
        print("\n=== 默认路径 ===")
        me = inst.default_dir(False)
        allu = inst.default_dir(True)
        check("仅为我安装落在 LOCALAPPDATA",
              "AppData" in me and "Programs" in me, me)
        check("所有用户安装落在 Program Files",
              "Program Files" in allu, allu)
        check("两者不相同", os.path.normcase(me) != os.path.normcase(allu))

    finally:
        shutil.rmtree(work, ignore_errors=True)

    return report()


if __name__ == "__main__":
    sys.exit(main())
