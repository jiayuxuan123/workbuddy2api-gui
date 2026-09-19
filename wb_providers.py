"""wb_providers.py —— 把本网关写成各家 AI 客户端能读的配置。

这是 CC Switch 那类工具的核心能力：不必手工编辑各家的 JSON / TOML / .env，
在这里点一下就把当前网关写进目标客户端，并在多个供应商之间切换。

支持的客户端与它们的配置文件（均为各工具官方约定）：

| 客户端 | 配置文件 | 格式 |
|---|---|---|
| Claude Code | `~/.claude/settings.json` | JSON，`env` 里放 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` |
| Codex CLI | `~/.codex/config.toml` + `auth.json` | TOML 选 provider，JSON 存 key |
| Gemini CLI | `~/.gemini/.env` | 环境变量 `GEMINI_API_KEY` |

安全约定：

* **改动前先备份**。每个文件第一次被本程序修改时存一份 `.wb-backup`，
  用户可以随时还原。
* **只动自己管的字段**。已有的其它键原样保留，不整file覆写 —— 用户自己的
  模型偏好、主题设置不该被我们清掉。
* **不猜测路径**。所有路径来自已知约定或用户显式指定；写入前规范化并
  确认落在该配置目录内。

只用标准库。
"""

import json
import os
import re
import shutil
import tempfile
import time

#: 本网关在配置文件里的显示名。
GATEWAY_NAME = "WorkBuddy2API"

#: 支持的客户端定义。`kind` 决定用哪种读写方式。
CLIENTS = {
    "claude": {
        "name": "Claude Code",
        "home": ".claude",
        "file": "settings.json",
        "format": "claude-json",
        "note": "写入 settings.json 的 env.ANTHROPIC_BASE_URL 与 ANTHROPIC_AUTH_TOKEN",
    },
    "codex": {
        "name": "Codex CLI",
        "home": ".codex",
        "file": "config.toml",
        "format": "codex-toml",
        "note": "写入 config.toml 的 model_provider 与 model_providers 段，key 存 auth.json",
    },
    "gemini": {
        "name": "Gemini CLI",
        "home": ".gemini",
        "file": ".env",
        "format": "gemini-env",
        "note": "写入 .env 的 GOOGLE_GEMINI_BASE_URL 与 GEMINI_API_KEY",
    },
}

#: 备份后缀。还原时据此找回原文件。
BACKUP_SUFFIX = ".wb-backup"


class ProviderError(Exception):
    """A failure with a message worth showing to the user."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def client_home(client_id, base=None):
    """Directory the client keeps its configuration in."""
    spec = CLIENTS.get(client_id)
    if not spec:
        raise ProviderError("未知的客户端：%s" % client_id)
    root = base or os.path.expanduser("~")
    return os.path.join(root, spec["home"])


def client_file(client_id, base=None):
    """The file this module reads and writes for a client."""
    spec = CLIENTS[client_id]
    return os.path.join(client_home(client_id, base), spec["file"])


def _inside(path, root):
    """True when ``path`` resolves inside ``root``."""
    resolved = os.path.realpath(path)
    root = os.path.realpath(root)
    if resolved == root:
        return True
    try:
        return os.path.commonpath([resolved, root]) == root
    except ValueError:
        return False


def _guarded(client_id, base=None):
    """Resolve a client's config file and prove it stays in its own folder.

    Every write below goes through this: the path comes from a fixed table plus
    the user's home, but a malformed value should fail loudly rather than write
    somewhere unexpected.
    """
    root = os.path.realpath(client_home(client_id, base))
    target = os.path.realpath(client_file(client_id, base))
    if not _inside(target, root):
        raise ProviderError("配置文件路径超出该客户端的配置目录")
    for part in os.path.normpath(target).split(os.sep):
        if part == "..":
            raise ProviderError("配置文件路径含上级引用")
    return target


def _backup_once(target):
    """Keep the original file the first time we modify it."""
    backup = target + BACKUP_SUFFIX
    if os.path.isfile(target) and not os.path.exists(backup):
        try:
            shutil.copy2(target, backup)
        except Exception as exc:
            raise ProviderError("备份失败：%s" % exc)
    return backup


def _write_atomic(target, text):
    """Write through a temp file in the same folder, then rename over."""
    folder = os.path.dirname(target)
    os.makedirs(folder, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=".wbcfg-", suffix=".tmp", dir=folder)
    try:
        os.write(handle, text.encode("utf-8"))
    finally:
        os.close(handle)
    os.replace(temp, target)


def _read_text(target):
    try:
        with open(target, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""
    except Exception as exc:
        raise ProviderError("读取失败：%s" % exc)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def gateway_endpoint(port=None, host=None):
    """The base URL a client should be pointed at.

    Reads the running gateway's port when it is up, so the value written to a
    client matches what the gateway is actually listening on. Falls back to the
    saved preference, then to the default.
    """
    if port is None:
        try:
            import wb_gateway as _gw
            port = getattr(_gw, "ACTIVE_PORT", None)
            if not port:
                port = _gw.load_prefs().get("port")
        except Exception:
            port = None
        if not port:
            port = 8788
    host = host or "127.0.0.1"
    return "http://%s:%d" % (host, port)


def gateway_key():
    """The API key a client should present, or "" when none is required."""
    try:
        import wb_proxy
        return wb_proxy.API_KEY or ""
    except Exception:
        return ""


def current_provider(client_id, base=None, port=None):
    """Which provider the client is currently configured to use.

    Returns a dict with ``base_url``, ``has_key`` and ``is_gateway``. Absent or
    unreadable config yields empty values rather than an error: "not
    configured" is a normal state, not a failure.

    ``port`` must match whatever apply() wrote. Detection and writing resolve
    the port the same way - the live value first, then the saved preference -
    so a client pointed at our gateway is never reported as pointing elsewhere.
    """
    spec = CLIENTS.get(client_id)
    if not spec:
        raise ProviderError("未知的客户端：%s" % client_id)
    target = _guarded(client_id, base)
    info = {"base_url": "", "has_key": False, "is_gateway": False,
            "file": target, "exists": os.path.isfile(target)}
    text = _read_text(target)
    if not text:
        return info

    fmt = spec["format"]
    if fmt == "claude-json":
        try:
            data = json.loads(text)
        except Exception:
            info["error"] = "配置文件不是合法 JSON"
            return info
        env = data.get("env") or {}
        info["base_url"] = str(env.get("ANTHROPIC_BASE_URL") or "")
        info["has_key"] = bool(env.get("ANTHROPIC_AUTH_TOKEN")
                               or env.get("ANTHROPIC_API_KEY"))
    elif fmt == "codex-toml":
        block = _toml_section(text, "model_providers." + GATEWAY_NAME.lower())
        if not block:
            # Fall back to whatever the active provider points at.
            active = _toml_value(text, "model_provider")
            if active:
                block = _toml_section(text, "model_providers." + active)
        info["base_url"] = _toml_value(block or "", "base_url")
        auth = _read_text(os.path.join(client_home(client_id, base), "auth.json"))
        info["has_key"] = bool(auth.strip("{} \n"))
    elif fmt == "gemini-env":
        info["base_url"] = _env_value(text, "GOOGLE_GEMINI_BASE_URL")
        info["has_key"] = bool(_env_value(text, "GEMINI_API_KEY"))

    # Compare host:port rather than the whole string, so a trailing slash or a
    # /v1 suffix on the client side still counts as pointing at this gateway.
    # The port is resolved exactly as apply() resolves it, so the two halves of
    # this feature cannot disagree about which port "us" means.
    from urllib.parse import urlparse
    mine = urlparse(gateway_endpoint(port=port))
    theirs = urlparse(info["base_url"] or "")
    info["gateway_host"] = mine.hostname
    info["gateway_port"] = mine.port
    info["is_gateway"] = bool(
        theirs.hostname and mine.hostname
        and theirs.hostname == mine.hostname
        and (theirs.port or 80) == (mine.port or 80))
    return info


def status(base=None, port=None):
    """Status for every supported client."""
    out = {}
    for client_id in CLIENTS:
        try:
            out[client_id] = current_provider(client_id, base=base, port=port)
        except Exception as exc:
            out[client_id] = {"error": str(exc), "is_gateway": False,
                              "base_url": "", "has_key": False}
        out[client_id]["name"] = CLIENTS[client_id]["name"]
        out[client_id]["note"] = CLIENTS[client_id]["note"]
        out[client_id]["installed"] = bool(
            os.path.isdir(client_home(client_id, base)))
    return out


# ---------------------------------------------------------------------------
# Minimal TOML / .env helpers
# ---------------------------------------------------------------------------
# These handle only the shapes the clients use: flat key = value lines, string
# literals, and [dotted.table] headers. A full TOML parser is out of scope; the
# aim is to preserve every line we do not own and edit the few we do.

def _toml_section(text, name):
    """Return the body of ``[name]``, or "" when absent."""
    header = "[%s]" % name
    lines = text.splitlines()
    collected = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if inside:
                break
            inside = (stripped == header)
            continue
        if inside:
            collected.append(line)
    return "\n".join(collected)


def _toml_value(text, key):
    """Return the value of a top-level ``key = "value"`` line."""
    pattern = re.compile(r'^\s*%s\s*=\s*(.+?)\s*(?:#.*)?$' % re.escape(key),
                         re.MULTILINE)
    match = pattern.search(text or "")
    if not match:
        return ""
    raw = match.group(1).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    return raw


def _set_toml_value(text, key, value):
    """Set a TOP-LEVEL key, preserving everything else.

    TOML keys belong to the table they sit under, so a top-level key has to be
    written before the first [table] header. Appending at the end of the file -
    which is what an earlier version did - puts it inside whatever table comes
    last, where the client never reads it.
    """
    line = '%s = "%s"' % (key, value)
    pattern = re.compile(r'^\s*%s\s*=\s*.*$' % re.escape(key), re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(line, text, count=1)

    lines = text.splitlines()
    insert_at = len(lines)
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            insert_at = index
            break
    lines.insert(insert_at, line)
    return "\n".join(lines).rstrip("\n") + "\n"


def _set_toml_section(text, name, body):
    """Replace or append a ``[name]`` table, leaving other tables alone."""
    header = "[%s]" % name
    lines = text.splitlines()
    out = []
    index = 0
    replaced = False
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped == header:
            out.append(header)
            out.extend(body.splitlines())
            replaced = True
            index += 1
            # Skip the old body.
            while index < len(lines):
                nxt = lines[index].strip()
                if nxt.startswith("[") and nxt.endswith("]"):
                    break
                index += 1
            continue
        out.append(lines[index])
        index += 1
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(header)
        out.extend(body.splitlines())
    return "\n".join(out).rstrip("\n") + "\n"


def _env_value(text, key):
    """Return an ``KEY=value`` value from a .env file."""
    pattern = re.compile(r'^\s*(?:export\s+)?%s\s*=\s*(.*)$' % re.escape(key),
                         re.MULTILINE)
    match = pattern.search(text or "")
    if not match:
        return ""
    raw = match.group(1).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    return raw


def _set_env_value(text, key, value):
    """Set a key in a .env file, preserving comments and other keys."""
    line = '%s=%s' % (key, value)
    pattern = re.compile(r'^\s*(?:export\s+)?%s\s*=.*$' % re.escape(key),
                         re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    prefix = text
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + line + "\n"


def _drop_env_value(text, key):
    """Remove a key from a .env file."""
    pattern = re.compile(r'^\s*(?:export\s+)?%s\s*=.*\n?' % re.escape(key),
                         re.MULTILINE)
    return pattern.sub("", text)


# ---------------------------------------------------------------------------
# Apply / revert
# ---------------------------------------------------------------------------
def preview(client_id, port=None, key=None, base=None):
    """Show what applying the gateway would change, without touching anything."""
    spec = CLIENTS.get(client_id)
    if not spec:
        raise ProviderError("未知的客户端：%s" % client_id)
    endpoint = "http://%s:%d" % (("127.0.0.1"), port or _port())
    key = key if key is not None else gateway_key()
    before = current_provider(client_id, base=base)
    return {
        "client": client_id,
        "name": spec["name"],
        "file": before.get("file"),
        "exists": before.get("exists"),
        "before_base_url": before.get("base_url") or "(未设置)",
        "after_base_url": endpoint,
        "before_has_key": before.get("has_key"),
        "after_has_key": bool(key),
        "note": spec["note"],
    }


def _port():
    """Port the gateway is on: live value first, then the saved preference."""
    return int(urlparse_port(gateway_endpoint()) or 8788)


def urlparse_port(url):
    from urllib.parse import urlparse
    return urlparse(url).port


def apply(client_id, port=None, key=None, base=None):
    """Point a client at this gateway.

    Only the gateway's own keys are written; everything else in the file is
    preserved. Returns a dict describing what changed.
    """
    spec = CLIENTS.get(client_id)
    if not spec:
        raise ProviderError("未知的客户端：%s" % client_id)
    target = _guarded(client_id, base)
    endpoint = "http://127.0.0.1:%d" % (port or _port())
    key = key if key is not None else gateway_key()
    backup = _backup_once(target)
    text = _read_text(target)
    changed = []

    fmt = spec["format"]
    if fmt == "claude-json":
        try:
            data = json.loads(text) if text.strip() else {}
        except Exception:
            raise ProviderError("现有 settings.json 不是合法 JSON，"
                                "为避免破坏已备份到 %s" % backup)
        if not isinstance(data, dict):
            raise ProviderError("settings.json 的顶层不是对象")
        env = data.get("env")
        if not isinstance(env, dict):
            env = {}
        env["ANTHROPIC_BASE_URL"] = endpoint
        if key:
            env["ANTHROPIC_AUTH_TOKEN"] = key
        else:
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        data["env"] = env
        _write_atomic(target, json.dumps(data, ensure_ascii=False, indent=2))
        changed.append("env.ANTHROPIC_BASE_URL")
        if key:
            changed.append("env.ANTHROPIC_AUTH_TOKEN")

    elif fmt == "codex-toml":
        provider_id = GATEWAY_NAME.lower()
        body = 'name = "%s"\nbase_url = "%s"\nwire_api = "responses"\n' % (
            GATEWAY_NAME, endpoint)
        if key:
            body += 'env_key = "WORKBUDDY_API_KEY"\n'
        text = _set_toml_section(text, "model_providers." + provider_id, body)
        text = _set_toml_value(text, "model_provider", provider_id)
        _write_atomic(target, text)
        changed.append("model_provider")
        changed.append("model_providers.%s" % provider_id)
        if key:
            auth_path = os.path.join(client_home(client_id, base), "auth.json")
            _backup_once(auth_path)
            auth = _read_text(auth_path)
            try:
                auth_data = json.loads(auth) if auth.strip() else {}
            except Exception:
                auth_data = {}
            if not isinstance(auth_data, dict):
                auth_data = {}
            auth_data[provider_id] = {"api_key": key}
            _write_atomic(auth_path, json.dumps(auth_data, ensure_ascii=False,
                                                indent=2))
            changed.append("auth.json")

    elif fmt == "gemini-env":
        text = _set_env_value(text, "GOOGLE_GEMINI_BASE_URL", endpoint)
        if key:
            text = _set_env_value(text, "GEMINI_API_KEY", key)
        _write_atomic(target, text)
        changed.append("GOOGLE_GEMINI_BASE_URL")
        if key:
            changed.append("GEMINI_API_KEY")

    return {"client": client_id, "name": spec["name"], "file": target,
            "endpoint": endpoint, "changed": changed, "backup": backup}


def revert(client_id, base=None):
    """Restore a client's file from the backup this module made."""
    target = _guarded(client_id, base)
    backup = target + BACKUP_SUFFIX
    if not os.path.isfile(backup):
        raise ProviderError("没有找到备份，无法还原（%s）" % backup)
    try:
        shutil.copy2(backup, target)
    except Exception as exc:
        raise ProviderError("还原失败：%s" % exc)
    return {"client": client_id, "restored_from": backup, "file": target}


def has_backup(client_id, base=None):
    """True when a restorable backup exists for this client."""
    try:
        return os.path.isfile(_guarded(client_id, base) + BACKUP_SUFFIX)
    except Exception:
        return False
