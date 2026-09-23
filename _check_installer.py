# -*- coding: utf-8 -*-
"""语法/导入自检：安装器模块能否编译与导入。"""
import importlib.util
import os
import py_compile
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(BASE, "installer.py")

print("=== 编译 installer.py ===")
try:
    code = py_compile.compile(TARGET, doraise=True, cfile=os.path.join(
        BASE, "__pycache__", "_syncheck_installer.pyc"))
    print("  编译通过: %s" % code)
except Exception as exc:
    print("  编译失败: %s" % exc)
    sys.exit(1)

print("\n=== AST 结构检查 ===")
import ast
tree = ast.parse(open(TARGET, encoding="utf-8").read(), TARGET)
funcs = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
print("  顶层函数 %d 个:" % len(funcs))
for i in range(0, len(funcs), 4):
    print("    %s" % ", ".join(funcs[i:i + 4]))
print("  类: %s" % classes)

print("\n=== 关键能力存在性 ===")
need = ["create_shortcut", "write_uninstall_entry", "delete_uninstall_entry",
        "do_install", "do_uninstall", "run_wizard", "run_uninstaller",
        "extract_payload", "check_target", "running_instances",
        "gateway_listening", "default_dir", "is_admin", "launch_app",
        "schedule_self_delete", "find_install_dir", "main"]
for name in need:
    print("  %-24s %s" % (name, "OK" if name in funcs else "MISSING"))

print("\n=== main() 参数解析自检（不执行安装） ===")
spec = importlib.util.spec_from_file_location("wb_installer", TARGET)
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
    print("  导入成功")
    parser = mod.build_parser()
    cases = [
        [],
        ["--silent", "--dir", r"C:\temp\x", "--desktop"],
        ["--uninstall", "--silent"],
        ["--all-users", "--startmenu"],
    ]
    for argv in cases:
        ns = parser.parse_args(argv)
        print("    %-46s -> dir=%r all_users=%s silent=%s desktop=%s uninstall=%s"
              % (" ".join(argv) or "(无参数)", ns.dir, ns.all_users,
                 ns.silent, ns.desktop, ns.uninstall))
    print("  payload_path() = %r" % mod.payload_path())
    print("  default_dir(False) = %s" % mod.default_dir(False))
    print("  default_dir(True)  = %s" % mod.default_dir(True))
    print("  appdata_data_dir() = %s" % mod.appdata_data_dir())
    print("  is_admin() = %s" % mod.is_admin())
    print("  running_instances() = %s" % mod.running_instances())
    print("  gateway_listening() = %s" % mod.gateway_listening())
except Exception as exc:
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n结果: OK")
