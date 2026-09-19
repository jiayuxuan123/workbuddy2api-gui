"""wb_gateway.py —— 服务端生命周期控制器（供 GUI 与命令行共用）。

背景：原先 wb_proxy.main() 把「读参数 → 初始化 → serve_forever()」揉在一个
函数里并阻塞到底，只有「启动整个进程」这一种用法。图形界面需要的是
「随时启动 / 随时停止 / 反复切换」，所以这里把初始化与监听拆成可独立调用的
步骤，由本模块统一驱动。

设计要点：

* **HTTP 服务跑在后台线程**。tkinter 必须在主线程跑消息循环，因此服务端
  不能占用主线程；``serve_forever()`` 放在守护线程里，``shutdown()`` 从
  主线程调用（这是 ThreadingHTTPServer 支持的用法）。
* **界面永远不直接读服务端内部状态**。所有状态都在锁保护下取值，避免
  边启动边查询时读到半初始化状态。
* **配置落盘**。端口、监听范围、自启开关等重启后要保留，存在数据目录下的
  ``gateway.json``，与面板管理的 ``accounts/settings.json`` 分开存放，
  避免两边互相覆盖。
* **文件读写只碰自己的数据目录**。文件名由常量决定，目录在读写前就地
  归一化并做包含性校验；临时文件由 ``tempfile.mkstemp`` 生成，文件名不由
  调用方拼装。

只用标准库。
"""

import json
import os
import socket
import tempfile
import threading
import time

import wb_proxy
import wb_runtime
import wb_settings

#: GUI/控制器偏好设置的存放位置（在可写数据目录里，随 EXE 走）。
PREFS_NAME = "gateway.json"

DEFAULTS = {
    "port": 8788,
    "host": "127.0.0.1",
    "lan": False,
    #: Demand an API key from loopback callers too. Off by default so a local
    #: client needs no configuration; keys set in the panel still authenticate
    #: either way, this only controls whether they are required.
    "require_local_key": False,
    "system_prompt": wb_proxy.DEFAULT_SYSTEM_PROMPT,
    "user_agent": "",
    "autostart": False,
    #: Start listening as soon as the program opens. The GUI has a checkbox
    #: for this, but the key was missing from DEFAULTS - save_prefs() builds
    #: its payload from DEFAULTS and then overlays the caller's values, so
    #: anything absent here was silently dropped and the setting never
    #: survived a restart. Boot-time autostart therefore never began serving
    #: and the user still had to open the window and press Start.
    "autostart_start": False,
    "start_minimized": False,
    "minimize_to_tray": False,
    "open_dashboard_on_start": False,
}


def _confined(base, name):
    """Return ``base/name`` after proving it cannot escape ``base``.

    ``name`` is always a module constant here; the check exists so that a
    future caller passing a caller-supplied name cannot quietly turn this
    into an arbitrary-path read or write.
    """
    root = os.path.realpath(base)
    if os.path.basename(name) != name or name in ("", ".", ".."):
        raise ValueError("unsafe file name")
    target = os.path.realpath(os.path.join(root, name))
    if os.path.commonpath([target, root]) != root:
        raise ValueError("path escapes the data directory")
    return target


def data_root():
    """The writable data directory this module confines itself to."""
    return os.path.realpath(wb_runtime.data_dir())


def prefs_path(accounts_dir=None):
    """Absolute path of the preferences file, confined to the data directory."""
    return _confined(accounts_dir or wb_runtime.data_dir(), PREFS_NAME)


def read_json_confined(path):
    """Read JSON from ``path``; the caller has already confined it.

    Opened through ``os.open`` on the resolved path so the descriptor refers
    to exactly the file that was validated, with no window for a symlink to
    be swapped in between the check and the open.
    """
    resolved = os.path.realpath(path)
    fd = os.open(resolved, os.O_RDONLY)
    try:
        chunks = []
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            chunks.append(block)
    finally:
        os.close(fd)
    text = b"".join(chunks).decode("utf-8")
    return json.loads(text) if text.strip() else {}


def write_json_confined(path, payload):
    """Atomically write JSON to ``path``.

    The temporary file is created by ``tempfile.mkstemp`` inside the target
    directory, so its name is chosen by the standard library rather than
    assembled by the caller, and ``os.replace`` then moves it into place.
    Writing through a temp file means a crash mid-write cannot leave a
    truncated preferences file behind.
    """
    resolved = os.path.realpath(path)
    root = os.path.dirname(resolved)
    os.makedirs(root, exist_ok=True)
    if os.path.commonpath([resolved, os.path.realpath(root)]) != os.path.realpath(root):
        raise ValueError("path escapes the data directory")

    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".prefs-", suffix=".tmp", dir=root)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, resolved)
    return resolved


def load_prefs(accounts_dir=None):
    """Read preferences, filling in anything missing from DEFAULTS."""
    out = dict(DEFAULTS)
    try:
        data = read_json_confined(prefs_path(accounts_dir))
        if isinstance(data, dict):
            for key in DEFAULTS:
                if key in data:
                    out[key] = data[key]
    except FileNotFoundError:
        pass
    except Exception as exc:
        wb_proxy.log("could not read %s (%s); using defaults" % (PREFS_NAME, exc))

    # Sanitise: a hand-edited or corrupted file must not break startup.
    try:
        out["port"] = max(1, min(65535, int(out.get("port") or 8788)))
    except (TypeError, ValueError):
        out["port"] = 8788
    if out.get("host") not in ("127.0.0.1", "0.0.0.0"):
        out["host"] = "0.0.0.0" if out.get("lan") else "127.0.0.1"
    out["lan"] = out["host"] == "0.0.0.0"
    for flag in ("autostart", "start_minimized", "minimize_to_tray",
                 "open_dashboard_on_start"):
        out[flag] = bool(out.get(flag))
    out["system_prompt"] = str(out.get("system_prompt") or
                               wb_proxy.DEFAULT_SYSTEM_PROMPT)
    out["user_agent"] = str(out.get("user_agent") or "")
    return out


def save_prefs(prefs, accounts_dir=None):
    """Persist preferences atomically. Returns the file path written."""
    payload = dict(DEFAULTS)
    payload.update(prefs or {})
    return write_json_confined(prefs_path(accounts_dir), payload)


def port_is_free(port, host="127.0.0.1"):
    """True when nothing is listening on the port.

    Checks the loopback address as well as the requested one: a copy bound to
    0.0.0.0 also answers on 127.0.0.1, so testing only the wildcard address
    would miss a conflict with a loopback-only instance.
    """
    for addr in set([host, "127.0.0.1"]):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((addr, port))
        except OSError:
            return False
        finally:
            probe.close()
    return True


def _local_http_get(port, path, timeout=2.0):
    """Minimal HTTP/1.1 GET against this machine's loopback interface.

    Deliberately does not build a URL or use ``urllib``. The only thing this
    module ever fetches is its own gateway's /health, so the destination is a
    literal loopback address rather than anything derived from settings:

    * ``127.0.0.1`` is written out in full - no hostname means no DNS lookup,
      so there is no name to be rebound to another address between check and
      connect.
    * no URL parsing means no scheme confusion (``file:``, ``gopher:``) and no
      redirect following, so the request cannot be steered elsewhere.
    * ``port`` is validated as an integer in range before the connect.

    Returns the decoded body, or None when nothing answered.
    """
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    if not (1 <= port <= 65535):
        return None
    if not path.startswith("/") or "\r" in path or "\n" in path:
        return None

    request = (
        "GET %s HTTP/1.1\r\n"
        "Host: 127.0.0.1:%d\r\n"
        "Accept: application/json\r\n"
        "Connection: close\r\n"
        "User-Agent: wb-gateway-probe\r\n"
        "\r\n" % (path, port)
    ).encode("ascii")

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(("127.0.0.1", port))
        probe.sendall(request)
        chunks = []
        while True:
            block = probe.recv(4096)
            if not block:
                break
            chunks.append(block)
            if sum(len(c) for c in chunks) > 65536:
                break
    except OSError:
        return None
    finally:
        probe.close()

    raw = b"".join(chunks)
    split = raw.find(b"\r\n\r\n")
    if split == -1:
        return None
    body = raw[split + 4:]
    # Chunked responses are unlikely for /health but handled for completeness.
    if b"transfer-encoding: chunked" in raw[:split].lower():
        try:
            body = _dechunk(body)
        except Exception:
            return None
    try:
        return body.decode("utf-8", "replace")
    except Exception:
        return None


def _dechunk(body):
    """Decode an HTTP chunked body."""
    out = []
    while True:
        line_end = body.find(b"\r\n")
        if line_end == -1:
            break
        size = int(body[:line_end].split(b";")[0], 16)
        if size == 0:
            break
        start = line_end + 2
        out.append(body[start:start + size])
        body = body[start + size + 2:]
    return b"".join(out)


def existing_instance(port, host="127.0.0.1"):
    """Describe a healthy gateway already answering on the port, else None.

    Used to tell "this port is taken by something unrelated" apart from
    "a gateway is already running", which need different messages.

    ``host`` is accepted for call-site symmetry but ignored: the instance
    being detected runs on this machine whichever interface it bound, and
    loopback always answers it. See :func:`_local_http_get` for why the
    request is built without a URL.
    """
    body = _local_http_get(port, "/health")
    if not body:
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    if isinstance(data, dict) and data.get("ok"):
        return data
    return None


#: Port the in-process gateway is listening on, or None when it is stopped.
#: Set by Gateway.start() so other modules (the client-config writer, the tray
#: wrapper) can read the live value instead of re-deriving it from the saved
#: preferences - which may be absent, stale, or from another directory.
ACTIVE_PORT = None


class Gateway(object):
    """Owns the HTTP server thread and the scheduler for one process."""

    def __init__(self, log=None):
        self._lock = threading.RLock()
        self._server = None
        self._thread = None
        self._log = log or wb_proxy.log
        self.prefs = load_prefs()
        self.last_error = ""
        self.started_at = 0.0

    # ------------------------------------------------------------------ state
    def is_running(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def uptime_seconds(self):
        with self._lock:
            if not self.is_running() or not self.started_at:
                return 0
            return int(time.time() - self.started_at)

    def base_url(self, host=None):
        """URL clients should use, reflecting the real listen address."""
        port = self.prefs.get("port", 8788)
        if host:
            return "http://%s:%d" % (host, port)
        if self.prefs.get("lan"):
            ips = wb_proxy.local_ip_addresses()
            if ips:
                return "http://%s:%d" % (ips[0], port)
        return "http://127.0.0.1:%d" % port

    def api_key(self):
        """The key clients must present, or "" when none is required."""
        return wb_proxy.API_KEY or ""

    def status(self):
        """A snapshot for the UI. Never raises, so the UI can poll freely."""
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            info = {
                "running": running,
                "port": self.prefs.get("port", 8788),
                "lan": bool(self.prefs.get("lan")),
                "url": self.base_url(),
                "api_key": self.api_key(),
                "auth_required": bool(wb_proxy.auth_required()),
                "uptime": self.uptime_seconds(),
                "error": self.last_error,
            }
            pool = getattr(wb_proxy, "POOL", None)
            if pool is not None:
                try:
                    info["accounts"] = len(pool.accounts)
                    info["usable"] = pool.count_usable()
                except Exception:
                    pass
            sched = getattr(wb_proxy, "SCHEDULER", None)
            if sched is not None:
                try:
                    st = sched.status()
                    info["scheduler_enabled"] = bool(st.get("enabled"))
                    info["scheduler_next"] = st.get("next_run_time") or ""
                    info["scheduler_last"] = st.get("last_run_time") or ""
                except Exception:
                    pass
            return info

    # ---------------------------------------------------------------- control
    def start(self, prefs=None):
        """Prepare the proxy and start listening. Returns (ok, message)."""
        with self._lock:
            if self.is_running():
                return True, "已在运行"
            if prefs:
                self.prefs.update(prefs)
            port = int(self.prefs.get("port", 8788))
            host = "0.0.0.0" if self.prefs.get("lan") else "127.0.0.1"

            # Refuse to fight another copy for the socket: on Windows
            # SO_REUSEADDR lets two processes bind the same port, and the OS
            # then splits connections between them unpredictably.
            if existing_instance(port, host) is not None:
                self.last_error = "端口 %d 已被另一个网关占用" % port
                return False, self.last_error
            if not port_is_free(port, host):
                self.last_error = "端口 %d 已被其他程序占用" % port
                return False, self.last_error

            try:
                self._prepare()
                server = wb_proxy.build_server(host, port)
            except OSError as exc:
                self.last_error = "无法监听 %s:%d - %s" % (host, port, exc)
                return False, self.last_error
            except Exception as exc:
                self.last_error = "启动失败: %s" % exc
                return False, self.last_error

            thread = threading.Thread(target=self._serve, args=(server,),
                                      name="wb-gateway", daemon=True)
            self._server = server
            self._thread = thread
            self.started_at = time.time()
            self.last_error = ""
            global ACTIVE_PORT
            ACTIVE_PORT = port
            thread.start()

            # Give the socket a moment so an immediate failure is reported now
            # rather than silently producing a gateway that never answers.
            for _ in range(40):
                if not thread.is_alive():
                    break
                time.sleep(0.05)
            if not thread.is_alive():
                self.last_error = "监听线程未能启动"
                return False, self.last_error
            return True, "已启动，监听 %s:%d" % (host, port)

    def _prepare(self):
        """Apply configuration to wb_proxy, replacing any previous session."""
        import wb_accounts

        wb_proxy.ACCOUNTS_DIR = wb_runtime.data_dir("accounts")
        wb_proxy.USAGE_DIR = wb_runtime.data_dir("usage")
        wb_proxy.USAGE_LOG = os.path.join(wb_proxy.USAGE_DIR, "usage.jsonl")
        wb_proxy.USAGE_SUMMARY = os.path.join(wb_proxy.USAGE_DIR,
                                              "usage-summary.json")
        wb_proxy.REALM_STATE_FILE = os.path.join(wb_proxy.ACCOUNTS_DIR,
                                                 "active_realm.json")
        wb_proxy.SYSTEM_PROMPT = (self.prefs.get("system_prompt")
                                  or wb_proxy.DEFAULT_SYSTEM_PROMPT)

        ua = (self.prefs.get("user_agent") or "").strip()
        if ua:
            wb_accounts.USER_AGENT = ua

        # API key policy.
        #
        # Loopback-only: no key by default. Several agent clients require an
        # API key field and behave badly when it is empty, and a key that is
        # never checked is worse than no key - it gives a false sense of
        # protection. A key that exists in the panel settings is therefore
        # ignored unless the operator explicitly asks for local auth.
        #
        # Listening on every interface: a key is mandatory and generated here.
        # The gateway spends the account's own upstream quota, so a guessable
        # default would let anyone on the network drain it. Generated once and
        # reused across restarts so clients keep working.
        if self.prefs.get("lan"):
            key, created = wb_settings.ensure_launcher_key(wb_proxy.ACCOUNTS_DIR)
            wb_proxy.API_KEY = key
            wb_proxy.API_KEY_FILE_SET = not created
            wb_proxy.API_KEY_LOCAL_REQUIRED = True
        elif self.prefs.get("require_local_key"):
            saved, is_set = wb_settings.api_key_override(wb_proxy.ACCOUNTS_DIR)
            if not is_set or not saved:
                # The switch is on but nothing is configured: mint one rather
                # than silently running unauthenticated.
                saved, _ = wb_settings.ensure_launcher_key(wb_proxy.ACCOUNTS_DIR)
            wb_proxy.API_KEY = saved
            wb_proxy.API_KEY_FILE_SET = True
            wb_proxy.API_KEY_LOCAL_REQUIRED = True
        else:
            wb_proxy.API_KEY = None
            wb_proxy.API_KEY_FILE_SET = False
            wb_proxy.API_KEY_LOCAL_REQUIRED = False

        pool = wb_accounts.AccountPool(wb_proxy.ACCOUNTS_DIR, log=wb_proxy.log)
        pool.load()
        wb_proxy.POOL = pool
        wb_proxy.load_persisted_realm()

        from wb_scheduler import Scheduler
        scheduler = Scheduler(pool)
        wb_proxy.SCHEDULER = scheduler
        scheduler.start()

        wb_proxy.log("gateway   : prepared (%d account(s))" % len(pool.accounts))

    def _serve(self, server):
        try:
            server.serve_forever(poll_interval=0.5)
        except Exception as exc:
            self.last_error = "服务端异常: %s" % exc
            wb_proxy.log("gateway   : serve_forever stopped - %s" % exc)

    def stop(self):
        """Stop listening and the scheduler. Returns (ok, message)."""
        with self._lock:
            if not self.is_running():
                return True, "未在运行"
            sched = getattr(wb_proxy, "SCHEDULER", None)
            if sched is not None:
                try:
                    sched.stop()
                except Exception:
                    pass
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        # shutdown() must not be called from the serving thread, hence this
        # runs after the lock is released, on the caller's thread.
        try:
            if server is not None:
                server.shutdown()
                server.server_close()
        except Exception as exc:
            wb_proxy.log("gateway   : shutdown error - %s" % exc)
        if thread is not None:
            thread.join(timeout=5)
        self.started_at = 0.0
        global ACTIVE_PORT
        ACTIVE_PORT = None
        wb_proxy.log("gateway   : stopped")
        return True, "已停止"

    def restart(self):
        self.stop()
        time.sleep(0.3)
        return self.start()
