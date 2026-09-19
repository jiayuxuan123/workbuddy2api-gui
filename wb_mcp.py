"""wb_mcp.py —— MCP 服务器统一管理（对齐 CC Switch 的真实逻辑）。

一个 MCP 服务器在库里存一份，勾选要给哪些客户端用；本模块负责把同一份
定义**翻译成各客户端自己的格式**写进它们各自的文件。这样用户只维护一份
配置，不用在三个工具的三种格式之间手工同步。

三个客户端的落盘位置和格式都不一样，下面每一条都来自 CC Switch 的实际
实现（`claude_mcp.rs`、`gemini_mcp.rs`、`mcp/codex.rs`）：

| 客户端 | 文件 | 容器 | 差异 |
|---|---|---|---|
| Claude Code | `~/.claude.json` | 根 `mcpServers` | Windows 下 `npx` 一类命令要包成 `cmd /c` |
| Codex CLI | `~/.codex/config.toml` | `[mcp_servers.<id>]` | `headers` 要改名成 `http_headers`，还要拆 `[....env]` 子表 |
| Gemini CLI | `~/.gemini/settings.json` | 根 `mcpServers` | **没有 type 字段**：`httpUrl`=http、`url`=sse、`command`=stdio；且必须写 `timeout` |

两个容易踩的坑：

* **Claude 的 MCP 不在 `~/.claude/settings.json`**，而在 `~/.claude.json` ——
  那是两个不同的文件。写错地方的表现是"配置看起来写进去了，但 CLI 读不到"。
* **不新建客户端目录**。目标客户端没装（目录不存在）就跳过，绝不为了写
  MCP 而凭空造一个 `~/.gemini/` 出来。

开关的语义：库里记 `enabled_<app>` 布尔值；**开启 = 往目标文件写入该条目，
关闭 = 从目标文件里删掉该键**。没有"禁用列表"这种东西。

只用标准库。
"""

import json
import os
import re
import tempfile

from wb_providers import ProviderError, _inside, _read_text, _write_atomic

#: 支持的客户端。注意 claude 的 MCP 文件与它的供应商配置不是同一个。
APPS = {
    "claude": {
        "name": "Claude Code",
        "home": ".claude",
        "file": os.path.join("..", ".claude.json"),
        "container": "mcpServers",
        "format": "claude-json",
    },
    "codex": {
        "name": "Codex CLI",
        "home": ".codex",
        "file": "config.toml",
        "container": "mcp_servers",
        "format": "codex-toml",
    },
    "gemini": {
        "name": "Gemini CLI",
        "home": ".gemini",
        "file": "settings.json",
        "container": "mcpServers",
        "format": "gemini-json",
    },
}

#: 合法传输类型。缺省（不写）等于 stdio。
TRANSPORTS = ("stdio", "http", "sse")

#: 这些键只是界面上的辅助信息，不能写进客户端的配置文件。
UI_KEYS = ("server", "enabled", "source", "id", "name", "description",
           "tags", "homepage", "docs")

#: Claude 在 Windows 上需要包一层 cmd /c 的命令。
_WRAP_COMMANDS = ("npx", "npm", "yarn", "pnpm", "node", "bun", "deno")

#: Gemini MCP 条目的默认超时（毫秒），与 CC Switch 的取值一致。
GEMINI_DEFAULT_TIMEOUT_MS = 60000

#: 库文件名（位于数据目录）。
LIBRARY_NAME = "mcp.json"


class McpError(ProviderError):
    """A failure with a message worth showing to the user."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def app_file(app_id, base=None):
    """The file this app keeps its MCP servers in."""
    spec = APPS.get(app_id)
    if not spec:
        raise McpError("未知的客户端：%s" % app_id)
    root = base or os.path.expanduser("~")
    home = os.path.join(root, spec["home"])
    # Claude's file sits beside its directory, not inside it: ~/.claude.json
    # next to ~/.claude/. normpath resolves the leading ".." for that case
    # while every other app stays inside its own directory.
    return os.path.normpath(os.path.join(home, spec["file"]))


def _guarded(app_id, base=None):
    """Resolve an app's MCP file and prove it stays where it belongs.

    Claude's file is deliberately one level above its own directory, so the
    containment check is against the *home* the caller supplied rather than the
    app directory - otherwise a legitimate ``~/.claude.json`` would be rejected.
    """
    root = os.path.realpath(base or os.path.expanduser("~"))
    target = os.path.realpath(app_file(app_id, base))
    if not _inside(target, root):
        raise McpError("配置文件路径超出主目录")
    return target


def app_installed(app_id, base=None):
    """True when the client is present.

    A client that is not installed is skipped entirely: writing config into a
    directory we invented would be invisible at best and confusing at worst.
    """
    spec = APPS.get(app_id)
    if not spec:
        return False
    root = base or os.path.expanduser("~")
    return os.path.isdir(os.path.join(root, spec["home"]))


def mcp_file_exists(app_id, base=None):
    """True when the app's own MCP file already exists."""
    return os.path.isfile(_guarded(app_id, base))


def should_sync(app_id, base=None):
    """Whether it is safe to write this app's MCP config at all."""
    return app_installed(app_id, base) or mcp_file_exists(app_id, base)


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------
def data_dir():
    try:
        import wb_runtime
        return wb_runtime.data_dir()
    except Exception:
        return os.path.expanduser("~")


def library_path():
    return os.path.join(data_dir(), LIBRARY_NAME)


def _blank():
    return {"version": 1, "servers": {}}


def load_library(path=None):
    target = path or library_path()
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return _blank()
    if not isinstance(data, dict):
        return _blank()
    if not isinstance(data.get("servers"), dict):
        data["servers"] = {}
    return data


def save_library(library, path=None):
    target = path or library_path()
    folder = os.path.dirname(target)
    if folder:
        os.makedirs(folder, exist_ok=True)
    _write_atomic(target, json.dumps(library, ensure_ascii=False, indent=2))
    return target


def _clean_server(raw, sid):
    """Normalize one stored server; returns None when unusable."""
    if not isinstance(raw, dict):
        return None
    spec = raw.get("server")
    if isinstance(spec, dict) and isinstance(spec.get("server"), dict):
        spec = spec["server"]        # tolerate the old {server: {...}} wrapper
    if not isinstance(spec, dict):
        return None
    transport = str(spec.get("type") or "").strip().lower()
    if transport and transport not in TRANSPORTS:
        transport = ""
    if not transport:
        transport = "http" if spec.get("url") else "stdio"
    if transport == "stdio" and not str(spec.get("command") or "").strip():
        return None
    if transport in ("http", "sse") and not str(spec.get("url") or "").strip():
        return None
    apps = raw.get("apps")
    enabled = {}
    for app_id in APPS:
        enabled[app_id] = bool((apps or {}).get(app_id)) if isinstance(apps, dict) else False
    return {
        "id": sid,
        "name": str(raw.get("name") or sid).strip() or sid,
        "server": dict(spec),
        "apps": enabled,
        "description": str(raw.get("description") or ""),
        "homepage": str(raw.get("homepage") or ""),
        "tags": [str(t) for t in (raw.get("tags") or []) if str(t).strip()],
    }


def list_servers(library=None, path=None):
    """Every MCP server in the library, in name order."""
    lib = library if library is not None else (load_library(path) if path else load_library())
    out = []
    for sid, raw in (lib.get("servers") or {}).items():
        cleaned = _clean_server(raw, sid)
        if cleaned:
            out.append(cleaned)
    out.sort(key=lambda s: (s["name"].lower(), s["id"]))
    return out


def upsert_server(server, library=None, path=None, base=None):
    """Insert or replace one server, then project it to every enabled app."""
    sid = str((server or {}).get("id") or "").strip()
    if not sid:
        raise McpError("MCP 服务器缺少 id")
    cleaned = _clean_server(server, sid)
    if not cleaned:
        raise McpError("MCP 服务器缺少 command 或 url")
    lib = library if library is not None else (load_library(path) if path else load_library())
    previous = _clean_server(lib["servers"].get(sid), sid)
    lib["servers"][sid] = cleaned
    save_library(lib, path)

    # An app that was switched off in this edit has to lose the entry from its
    # file; the others just get the new definition.
    if previous:
        for app_id, was in previous["apps"].items():
            if was and not cleaned["apps"].get(app_id):
                remove_from_app(app_id, sid, library=lib, base=base)
    for app_id, on in cleaned["apps"].items():
        if on:
            write_to_app(app_id, cleaned, library=lib, base=base)
    return cleaned


def delete_server(sid, library=None, path=None, base=None):
    """Remove a server from the library and from every app that had it."""
    lib = library if library is not None else (load_library(path) if path else load_library())
    if sid not in (lib.get("servers") or {}):
        raise McpError("没有找到 MCP 服务器：%s" % sid)
    for app_id in APPS:
        remove_from_app(app_id, sid, library=lib, base=base)
    lib["servers"].pop(sid, None)
    save_library(lib, path)
    return True


def set_app_enabled(sid, app_id, enabled, library=None, path=None, base=None):
    """Turn one server on or off for one app, and update that app's file."""
    if app_id not in APPS:
        raise McpError("未知的客户端：%s" % app_id)
    lib = library if library is not None else (load_library(path) if path else load_library())
    raw = (lib.get("servers") or {}).get(sid)
    cleaned = _clean_server(raw, sid)
    if not cleaned:
        raise McpError("没有找到 MCP 服务器：%s" % sid)
    cleaned["apps"][app_id] = bool(enabled)
    lib["servers"][sid] = cleaned
    save_library(lib, path)
    if enabled:
        write_to_app(app_id, cleaned, library=lib, base=base)
    else:
        remove_from_app(app_id, sid, library=lib, base=base)
    return cleaned


# ---------------------------------------------------------------------------
# Per-app serialization
# ---------------------------------------------------------------------------
def _spec_for_app(server, app_id):
    """Translate a server spec into the app's own shape.

    The differences are the whole point of this module:

    * Codex renames ``headers`` to ``http_headers``;
    * Gemini has no ``type`` field at all - the transport is implied by which
      key is present (``httpUrl`` / ``url`` / ``command``) - and wants a
      ``timeout``;
    * Claude passes the spec through, after dropping the UI-only keys.
    """
    spec = {k: v for k, v in (server.get("server") or {}).items()
            if k not in UI_KEYS}
    transport = str(spec.pop("type", "") or "").strip().lower()
    if not transport:
        transport = "http" if spec.get("url") else "stdio"
    spec["type"] = transport

    out = {}
    if app_id == "gemini":
        spec.pop("type", None)
        if transport == "http":
            out["httpUrl"] = spec.get("url", "")
        elif transport == "sse":
            out["url"] = spec.get("url", "")
        else:
            out["command"] = spec.get("command", "")
            if spec.get("args"):
                out["args"] = list(spec["args"])
            if spec.get("env"):
                out["env"] = dict(spec["env"])
            if spec.get("cwd"):
                out["cwd"] = spec["cwd"]
        if spec.get("headers"):
            out["headers"] = dict(spec["headers"])
        out["timeout"] = _gemini_timeout(spec)
        return out

    if app_id == "codex":
        if transport == "stdio":
            out["type"] = "stdio"
            out["command"] = spec.get("command", "")
            if spec.get("args"):
                out["args"] = list(spec["args"])
            if spec.get("env"):
                out["env"] = dict(spec["env"])
            if spec.get("cwd"):
                out["cwd"] = spec["cwd"]
        else:
            out["type"] = transport
            out["url"] = spec.get("url", "")
            if spec.get("headers"):
                # Codex calls it http_headers; emitting both would be a bug.
                out["http_headers"] = dict(spec["headers"])
        return out

    # Claude Code: pass through, with Windows command wrapping.
    out = dict(spec)
    if transport == "stdio":
        out["command"], out["args"] = _wrap_command(
            spec.get("command", ""), list(spec.get("args") or []))
        if not out["args"]:
            out.pop("args", None)
    return out


def _gemini_timeout(spec):
    """Gemini always gets a timeout; explicit values win over the default."""
    for key, factor in (("timeout", 1), ("tool_timeout_ms", 1),
                        ("tool_timeout_sec", 1000), ("startup_timeout_ms", 1),
                        ("startup_timeout_sec", 1000)):
        value = spec.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value * factor)
    return GEMINI_DEFAULT_TIMEOUT_MS


def _wrap_command(command, args):
    """On Windows, wrap npx/npm/... in ``cmd /c``, the way Claude expects.

    A path into WSL is left alone: those are not Windows executables and
    ``cmd /c`` cannot run them.
    """
    name = str(command or "").strip()
    if os.name != "nt":
        return name, args
    bare = name.lower()
    if bare.endswith(".cmd"):
        bare = bare[:-4]
    if bare not in _WRAP_COMMANDS:
        return name, args
    lowered = name.replace("/", "\\").lower()
    if lowered.startswith("\\\\wsl$") or lowered.startswith("\\\\wsl.localhost"):
        return name, args
    return "cmd", ["/c", name] + list(args)


# ---------------------------------------------------------------------------
# Per-app read / write
# ---------------------------------------------------------------------------
def read_from_app(app_id, base=None):
    """The MCP servers currently in an app's own file, keyed by name."""
    if app_id not in APPS:
        raise McpError("未知的客户端：%s" % app_id)
    target = _guarded(app_id, base)
    text = _read_text(target)
    if not text.strip():
        return {}
    spec = APPS[app_id]
    if spec["format"] == "codex-toml":
        return _read_codex_mcp(text)
    try:
        data = json.loads(text)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    servers = data.get(spec["container"])
    if not isinstance(servers, dict):
        return {}
    out = {}
    for name, entry in servers.items():
        if isinstance(entry, dict):
            out[name] = _to_library_spec(entry, app_id)
    return out


def _to_library_spec(entry, app_id):
    """Turn an app's own MCP shape back into the library's shape."""
    spec = dict(entry)
    if app_id == "gemini":
        # Transport is implied by the key that is present.
        if spec.pop("httpUrl", None) is not None:
            spec["type"] = "http"
        elif "url" in spec and "command" not in spec:
            spec["type"] = "sse"
        else:
            spec["type"] = "stdio"
        spec.pop("timeout", None)
    elif app_id == "codex":
        if "http_headers" in spec:
            spec["headers"] = spec.pop("http_headers")
        if not spec.get("type"):
            spec["type"] = "http" if spec.get("url") else "stdio"
    else:
        if not spec.get("type"):
            spec["type"] = "http" if spec.get("url") else "stdio"
    return spec


def write_to_app(app_id, server, library=None, base=None):
    """Upsert one server into an app's file. Returns the path, or "" if skipped."""
    if app_id not in APPS:
        raise McpError("未知的客户端：%s" % app_id)
    if not should_sync(app_id, base):
        return ""
    target = _guarded(app_id, base)
    spec = APPS[app_id]
    entry = _spec_for_app(server, app_id)
    text = _read_text(target)

    if spec["format"] == "codex-toml":
        text = _set_codex_mcp(text, server["id"], entry)
        _write_atomic(target, text)
        return target

    data = {}
    if text.strip():
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}
    servers = data.get(spec["container"])
    if not isinstance(servers, dict):
        servers = {}
    servers[server["id"]] = entry
    data[spec["container"]] = servers
    _write_atomic(target, json.dumps(_sorted(data), ensure_ascii=False, indent=2))
    return target


def remove_from_app(app_id, sid, library=None, base=None):
    """Delete one server's key from an app's file. Returns the path, or ""."""
    if app_id not in APPS:
        raise McpError("未知的客户端：%s" % app_id)
    if not should_sync(app_id, base):
        return ""
    target = _guarded(app_id, base)
    spec = APPS[app_id]
    text = _read_text(target)
    if not text.strip():
        return ""

    if spec["format"] == "codex-toml":
        updated = _drop_codex_mcp(text, sid)
        if updated != text:
            _write_atomic(target, updated)
        return target

    try:
        data = json.loads(text)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    servers = data.get(spec["container"])
    if not isinstance(servers, dict) or sid not in servers:
        return ""
    servers.pop(sid, None)
    if servers:
        data[spec["container"]] = servers
    else:
        # Leave no empty container behind; an empty object is not the same as
        # "this file has no MCP section".
        data.pop(spec["container"], None)
    _write_atomic(target, json.dumps(_sorted(data), ensure_ascii=False, indent=2))
    return target


def sync_app(app_id, library=None, base=None, path=None):
    """Make an app's file match the library exactly for this app."""
    if app_id not in APPS:
        raise McpError("未知的客户端：%s" % app_id)
    lib = library if library is not None else (load_library(path) if path else load_library())
    written = []
    for server in list_servers(lib):
        if server["apps"].get(app_id):
            target = write_to_app(app_id, server, library=lib, base=base)
            if target:
                written.append(target)
    live = read_from_app(app_id, base=base)
    known = {s["id"] for s in list_servers(lib)}
    for name in live:
        if name not in known:
            remove_from_app(app_id, name, library=lib, base=base)
    return written


def sync_all(library=None, base=None, path=None):
    """Sync every installed app; returns a per-app summary."""
    lib = library if library is not None else (load_library(path) if path else load_library())
    out = {}
    for app_id in APPS:
        if not should_sync(app_id, base):
            out[app_id] = {"skipped": True, "reason": "客户端未安装"}
            continue
        try:
            out[app_id] = {"skipped": False,
                           "written": sync_app(app_id, library=lib, base=base),
                           "servers": sorted(read_from_app(app_id, base=base))}
        except Exception as exc:
            out[app_id] = {"skipped": False, "error": str(exc)}
    return out


def status(base=None, path=None):
    """What each app currently has, plus the library view."""
    lib = load_library(path) if path else load_library()
    out = {"servers": [], "apps": {}}
    for server in list_servers(lib):
        entry = dict(server)
        out["servers"].append(entry)
    for app_id, spec in APPS.items():
        try:
            live = read_from_app(app_id, base=base)
            err = ""
        except Exception as exc:
            live, err = {}, str(exc)
        out["apps"][app_id] = {
            "name": spec["name"],
            "file": _guarded(app_id, base),
            "installed": app_installed(app_id, base),
            "synced": should_sync(app_id, base),
            "servers": sorted(live),
            "error": err,
        }
    return out


# ---------------------------------------------------------------------------
# Codex TOML helpers
# ---------------------------------------------------------------------------
# Codex keeps MCP servers under [mcp_servers.<id>], with env as its own
# [mcp_servers.<id>.env] sub-table. The older, wrong form [mcp.servers] is
# tolerated on read and dropped on write.

def _toml_string(value):
    return '"%s"' % str(value).replace("\\", "\\\\").replace('"', '\\"')


def _toml_inline(value):
    """Render a JSON value as TOML, or None when it has no TOML equivalent."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        items = [_toml_inline(item) for item in value]
        if any(item is None for item in items):
            return None
        return "[%s]" % ", ".join(items)
    if isinstance(value, dict):
        pairs = []
        for key, item in value.items():
            rendered = _toml_inline(item)
            if rendered is None:
                return None
            pairs.append("%s = %s" % (key, rendered))
        return "{%s}" % ", ".join(pairs)
    return None


def _drop_table(text, header):
    """Remove a ``[header]`` table and its body, keeping everything else."""
    lines = (text or "").splitlines()
    out = []
    index = 0
    removed = False
    while index < len(lines):
        if lines[index].strip() == header:
            removed = True
            index += 1
            while index < len(lines):
                nxt = lines[index].strip()
                if nxt.startswith("[") and nxt.endswith("]"):
                    break
                index += 1
            continue
        out.append(lines[index])
        index += 1
    if not removed:
        return text
    return "\n".join(out).rstrip("\n") + "\n" if out else ""


def _drop_codex_mcp(text, sid):
    """Remove one server and every sub-table of it from a Codex config.

    Matching is by table-name prefix, so a server with several nested tables
    (env, http_headers, ...) is removed completely rather than leaving an
    orphaned sub-table behind.
    """
    prefixes = ("mcp_servers.%s" % sid, "mcp.servers.%s" % sid)
    lines = (text or "").splitlines()
    out = []
    index = 0
    removed = False
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped[1:-1].strip()
            if any(name == p or name.startswith(p + ".") for p in prefixes):
                removed = True
                index += 1
                while index < len(lines):
                    nxt = lines[index].strip()
                    if nxt.startswith("[") and nxt.endswith("]"):
                        break
                    index += 1
                continue
        out.append(lines[index])
        index += 1
    if not removed:
        return text
    joined = "\n".join(out).rstrip("\n")
    if joined:
        return joined + "\n"
    return ""


def _codex_mcp_body(text):
    """True when any server still lives under mcp_servers."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("[mcp_servers.") and stripped.endswith("]"):
            return True
    return False


def _set_codex_mcp(text, sid, entry):
    """Write one server as [mcp_servers.<sid>], with maps as nested tables.

    Nested maps - env, http_headers - become their own sub-tables rather than
    inline tables. Both are valid TOML, but a sub-table reads back
    unambiguously, which inline tables do not without a full TOML parser.
    """
    text = _drop_codex_mcp(text, sid)
    lines = [text.rstrip("\n")] if text.strip() else []

    lines.append("")
    lines.append("[mcp_servers.%s]" % sid)
    nested = []
    for key, value in entry.items():
        if isinstance(value, dict):
            if value:
                nested.append((key, value))
            continue
        rendered = _toml_inline(value)
        if rendered is None or value in ("", None) or value == []:
            continue
        lines.append("%s = %s" % (key, rendered))
    for key, value in nested:
        lines.append("")
        lines.append("[mcp_servers.%s.%s]" % (sid, key))
        for sub_key, sub_value in value.items():
            rendered = _toml_inline(sub_value)
            if rendered is not None:
                lines.append("%s = %s" % (sub_key, rendered))
    return "\n".join(lines).rstrip("\n") + "\n"


def _read_codex_mcp(text):
    """Read every server under [mcp_servers.*] (and the legacy [mcp.servers.*])."""
    out = {}
    current = None
    sub = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            parts = [p.strip() for p in name.split(".")]
            if len(parts) >= 2 and parts[0] == "mcp_servers":
                current, sub = parts[1], (parts[2] if len(parts) > 2 else None)
            elif len(parts) >= 3 and parts[0] == "mcp" and parts[1] == "servers":
                current, sub = parts[2], (parts[3] if len(parts) > 3 else None)
            else:
                current, sub = None, None
            if current:
                out.setdefault(current, {})
            continue
        if not current or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = _toml_parse_value(value.strip())
        if sub:
            # Any nested table - env, http_headers, ... - belongs under its own
            # key, not flattened into the server's. Flattening loses the map.
            out[current].setdefault(sub, {})[key] = value
        else:
            out[current][key] = value
    for entry in out.values():
        entry.setdefault("type", "http" if "url" in entry else "stdio")
        if "http_headers" in entry:
            entry["headers"] = entry.pop("http_headers")
    return out


def _toml_parse_value(raw):
    raw = raw.split("#", 1)[0].strip() if not raw.startswith('"') else raw
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if raw in ("true", "false"):
        return raw == "true"
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_toml_parse_value(part.strip()) for part in inner.split(",")]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _sorted(value):
    """Recursively sort keys so two writes of the same data are identical."""
    if isinstance(value, dict):
        return {key: _sorted(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sorted(item) for item in value]
    return value
