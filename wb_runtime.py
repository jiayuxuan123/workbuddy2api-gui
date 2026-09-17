"""wb_runtime.py —— 运行环境探测：区分「只读资源」与「可写数据」目录。

脚本方式运行与 PyInstaller 打包成 EXE 后运行的路径语义完全不同，
本模块把这层差异集中到一处，其余模块只调用这里的函数。

两种打包形态的区别（关键）：

* onefile：所有文件被塞进单个 EXE，运行时解压到临时目录
  （``sys._MEIPASS``），**进程退出后该目录会被删除**。任何写在
  那里的账号、用量数据都会丢失。
* onedir：EXE 与依赖文件放在同一目录，``sys._MEIPASS`` 指向该目录。

因此本模块给出的两条路径规则是：

* ``resource_path()``  —— 只读随包资源（如 dashboard.html）。
  onefile 下必须在 ``_MEIPASS`` 里找，EXE 旁边是找不到的。
* ``data_dir()``       —— 可写数据（accounts/ 与 usage/）。
  **永远放在 EXE 所在目录**，绝不放 ``_MEIPASS``，
  否则打包后账号与统计会在重启时蒸发。

Windows 的 ``.exe`` 可能被放在 ``C:\\Program Files`` 等无写权限的位置，
故 ``data_dir()`` 会做一次写权限探测，失败时回退到
``%LOCALAPPDATA%``，保证首次运行就有一个能落盘的位置。
"""

import os
import sys
import tempfile

_IS_FROZEN = bool(getattr(sys, "frozen", False))


def is_frozen():
    """True when running from a PyInstaller bundle (onefile or onedir)."""
    return _IS_FROZEN


def bundle_dir():
    """Directory the read-only resources live in.

    * frozen  -> ``sys._MEIPASS`` (onefile: a temp dir; onedir: the app dir)
    * source  -> the directory containing this file
    """
    if _IS_FROZEN:
        return getattr(sys, "_MEIPASS", None) or os.path.dirname(
            os.path.abspath(sys.executable)
        )
    return os.path.dirname(os.path.abspath(__file__))


def app_dir():
    """Directory the user sees: where the EXE (or the .py) lives.

    This is where writable data belongs. It is deliberately NOT
    ``bundle_dir()``: under onefile those differ, and only this one
    survives the process.
    """
    if _IS_FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    """Absolute path to a read-only bundled resource."""
    return os.path.join(bundle_dir(), name)


def _writable(path):
    """True when we can create and remove a file in ``path``.

    Writability is probed by creating a real temp file in the directory
    rather than by ``os.access()``, because on Windows ``os.access(W_OK)``
    reports success in cases where the write then fails (per-machine ACLs).

    ``tempfile.mkstemp`` picks the filename itself, so the path handed to the
    OS is always inside ``dir`` and cannot be influenced by ``..`` segments in
    the configured value. Candidates come from ``WB_DATA_DIR``, the
    executable's own folder and ``%LOCALAPPDATA%`` - never from a request -
    so this guards against a malformed or symlinked configuration rather than
    against remote input.
    """
    try:
        target = os.path.realpath(path)
        os.makedirs(target, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".wb_write_probe", dir=target)
        try:
            os.write(fd, b"ok")
        finally:
            os.close(fd)
            os.unlink(probe)
        return True
    except Exception:
        return False


def data_dir(sub=None):
    """Writable base directory for accounts/ and usage/.

    Preference order:
      1. ``WB_DATA_DIR`` environment variable (explicit override)
      2. the directory next to the EXE / script  (portable layout)
      3. ``%LOCALAPPDATA%\\WorkBuddy2API``         (read-only install dir)

    ``sub`` optionally joins a child folder (e.g. ``"accounts"``).
    """
    candidates = []
    env = (os.environ.get("WB_DATA_DIR") or "").strip()
    if env:
        candidates.append(os.path.abspath(env))
    candidates.append(app_dir())
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    candidates.append(os.path.join(local, "WorkBuddy2API"))

    base = None
    for cand in candidates:
        if _writable(cand):
            base = cand
            break
    if base is None:
        base = candidates[-1]          # last resort: let the error surface
    return os.path.join(base, sub) if sub else base


def data_root():
    """The writable base directory itself (without a child folder)."""
    return data_dir()


# ---------------------------------------------------------------------------
# Console stream safety
# ---------------------------------------------------------------------------
# A PyInstaller --noconsole build has no console: sys.stdout / sys.stderr are
# None, and every write raises AttributeError. The gateway logs on nearly every
# code path, so an unguarded write would take the whole process down the first
# time it logged anything.

def safe_stream(stream):
    """Return a usable text stream: the real one, or a throwaway sink."""
    if stream is not None:
        try:
            stream.write("")
            return stream
        except Exception:
            pass
    import io
    return io.StringIO()


def out():
    """sys.stdout, or a sink when there is no console."""
    return safe_stream(sys.stdout)


def err():
    """sys.stderr, or a sink when there is no console."""
    return safe_stream(sys.stderr)


def write_out(text):
    """Write to stdout, silently doing nothing when no console exists."""
    try:
        out().write(text)
    except Exception:
        pass


def write_err(text):
    """Write to stderr, silently doing nothing when no console exists."""
    try:
        err().write(text)
    except Exception:
        pass


def flush_out():
    try:
        out().flush()
    except Exception:
        pass


def flush_err():
    try:
        err().flush()
    except Exception:
        pass


def configure_streams():
    """Make stdout/stderr UTF-8 tolerant; no-op without a console.

    Returns True when real streams were configured, False when absent.
    """
    ok = False
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            ok = True
        except Exception:
            pass
    return ok


def has_console():
    """True when this process has a usable console/stdout."""
    return sys.stdout is not None and sys.stderr is not None
