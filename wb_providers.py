"""wb_providers.py —— 供应商库与一键切换（对齐 CC Switch 的真实写入逻辑）。

这里做的是 CC Switch 的核心能力：为每个 AI 客户端维护一个**供应商库**，
点一下就把选中的供应商写进该客户端的配置文件，并可在供应商之间来回切换。

三种客户端的落盘方式各不相同，下面每一条都是照着 CC Switch 的实际实现
（`src-tauri/src/services/provider/live.rs`、`codex_config.rs`、
`gemini_config.rs`）复刻的，不是凭感觉写的：

| 客户端 | 落盘文件 | 关键点 |
|---|---|---|
| Claude Code | `~/.claude/settings.json` | 整文件替换，键按字母排序；key 写进 `ANTHROPIC_AUTH_TOKEN`（或 `ANTHROPIC_API_KEY`） |
| Codex CLI | `~/.codex/config.toml` | **只写 config.toml**；key 以 `experimental_bearer_token` 注入到当前 `[model_providers.<id>]` 表里 |
| Gemini CLI | `~/.gemini/.env` + `~/.gemini/settings.json` | `.env` 排序写 base_url/key；`settings.json` 必须盖 `security.auth.selectedType` |

几个容易踩的坑，都是复刻时确认过的：

* **Codex 的 key 不写 auth.json。** 早先的版本往 `auth.json` 里塞
  `{"provider": {"api_key": ...}}`，Codex 根本不读这个形状；正确做法是把
  token 放进 provider 表自己的 `experimental_bearer_token`。另外默认**不动**
  用户的 `auth.json`（那是他们的 ChatGPT 登录态）。
* **Gemini 只写 `.env` 不够。** 不把 `settings.json` 的
  `security.auth.selectedType` 改成 `gemini-api-key`，CLI 会继续走 OAuth，
  于是 base_url 明明写对了却仍然打去 Google。
* **切换不是覆盖用户设置。** 切换前先把目标客户端当前的线上配置**回填**到
  正要离开的那个供应商槽位里（CC Switch 的 backfill 机制），所以用户原有的
  theme / permissions / mcpServers 只是跟着供应商走，不会丢。

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

#: 本网关供应商的固定 id。
GATEWAY_ID = "workbuddy2api"

#: 支持的客户端。`format` 决定用哪种读写方式。
CLIENTS = {
    "claude": {
        "name": "Claude Code",
        "home": ".claude",
        "file": "settings.json",
        "legacy_file": "claude.json",
        "format": "claude-json",
        "note": "写入 settings.json 的 env.ANTHROPIC_BASE_URL 与 ANTHROPIC_AUTH_TOKEN",
    },
    "codex": {
        "name": "Codex CLI",
        "home": ".codex",
        "file": "config.toml",
        "auth_file": "auth.json",
        "format": "codex-toml",
        "note": "写入 config.toml 的 model_provider 与 model_providers 表，token 放在表内 experimental_bearer_token",
    },
    "gemini": {
        "name": "Gemini CLI",
        "home": ".gemini",
        "file": ".env",
        "settings_file": "settings.json",
        "format": "gemini-env",
        "note": "写入 .env 的 GOOGLE_GEMINI_BASE_URL 与 GEMINI_API_KEY，并在 settings.json 标记 gemini-api-key",
    },
}

#: 备份后缀。还原时据此找回原文件。
BACKUP_SUFFIX = ".wb-backup"

#: 供应商库文件名（位于数据目录）。
LIBRARY_NAME = "providers.json"

#: 分类，与 CC Switch 的 ProviderCategory 对齐。
CATEGORIES = ("official", "cn_official", "cloud_provider", "aggregator",
              "third_party", "custom")


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


def _sidecar(client_id, name, base=None):
    """A sibling file (auth.json / settings.json) proven to be in the client dir."""
    root = os.path.realpath(client_home(client_id, base))
    target = os.path.realpath(os.path.join(client_home(client_id, base), name))
    if not _inside(target, root):
        raise ProviderError("路径超出该客户端的配置目录")
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
# Library: the providers the user can switch between
# ---------------------------------------------------------------------------
def data_dir():
    try:
        import wb_runtime
        return wb_runtime.data_dir()
    except Exception:
        return os.path.expanduser("~")


def library_path():
    return os.path.join(data_dir(), LIBRARY_NAME)


def _blank_library():
    return {"version": 2, "providers": {}, "current": {}}


def load_library(path=None):
    """Read the provider library, tolerating a missing or damaged file."""
    target = path or library_path()
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return _blank_library()
    except Exception:
        return _blank_library()
    if not isinstance(data, dict):
        return _blank_library()
    data.setdefault("version", 2)
    if not isinstance(data.get("providers"), dict):
        data["providers"] = {}
    if not isinstance(data.get("current"), dict):
        data["current"] = {}
    for client_id in CLIENTS:
        bucket = data["providers"].get(client_id)
        data["providers"][client_id] = bucket if isinstance(bucket, list) else []
    return data


def save_library(library, path=None):
    """Write the library atomically so a crash cannot truncate it."""
    target = path or library_path()
    folder = os.path.dirname(target)
    if folder:
        os.makedirs(folder, exist_ok=True)
    _write_atomic(target, json.dumps(library, ensure_ascii=False, indent=2))
    return target


def _clean_provider(raw, client_id):
    """Normalize one stored provider; returns None when unusable."""
    if not isinstance(raw, dict):
        return None
    pid = str(raw.get("id") or "").strip()
    if not pid:
        return None
    config = raw.get("settings_config")
    if not isinstance(config, dict):
        config = {}
    category = str(raw.get("category") or "custom").strip().lower()
    if category not in CATEGORIES:
        category = "custom"
    try:
        sort_index = int(raw.get("sort_index") or 0)
    except Exception:
        sort_index = 0
    out = {
        "id": pid,
        "client": client_id,
        "name": str(raw.get("name") or pid).strip() or pid,
        "settings_config": config,
        "website_url": str(raw.get("website_url") or ""),
        "category": category,
        "sort_index": sort_index,
        "notes": str(raw.get("notes") or ""),
        "in_failover_queue": raw.get("in_failover_queue") is True,
        "builtin": raw.get("builtin") is True,
    }
    field = str(raw.get("api_key_field") or "").strip()
    if field in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        out["api_key_field"] = field
    return out


def _resolve(library, path):
    """Load the library the caller meant: an explicit object wins, then the path.

    Passing ``path`` without honouring it on load is how a save silently
    replaces one library with another, so every reader goes through here.
    """
    if library is not None:
        return library
    return load_library(path) if path else load_library()


def list_providers(client_id, library=None, base=None, path=None):
    """Every provider configured for a client, in display order."""
    if client_id not in CLIENTS:
        raise ProviderError("未知的客户端：%s" % client_id)
    lib = _resolve(library, path)
    bucket = lib["providers"].get(client_id) or []
    out = []
    for raw in bucket:
        cleaned = _clean_provider(raw, client_id)
        if cleaned:
            out.append(cleaned)
    out.sort(key=lambda p: (p["sort_index"], p["id"]))
    return out


def _put_providers(client_id, providers, library=None, path=None):
    lib = _resolve(library, path)
    lib["providers"][client_id] = providers
    save_library(lib, path)
    return lib


def upsert_provider(client_id, provider, library=None, path=None):
    """Insert or replace one provider entry."""
    if client_id not in CLIENTS:
        raise ProviderError("未知的客户端：%s" % client_id)
    cleaned = _clean_provider(provider, client_id)
    if not cleaned:
        raise ProviderError("供应商缺少 id")
    lib = _resolve(library, path)
    items = _bucket(client_id, lib)
    replaced = False
    for index, existing in enumerate(items):
        if existing["id"] == cleaned["id"]:
            # Keep the original position when the caller did not ask to move it.
            if not cleaned.get("sort_index") and existing.get("sort_index"):
                cleaned["sort_index"] = existing["sort_index"]
            if provider.get("in_failover_queue") is None:
                cleaned["in_failover_queue"] = existing["in_failover_queue"]
            items[index] = cleaned
            replaced = True
            break
    if not replaced:
        if not cleaned.get("sort_index"):
            cleaned["sort_index"] = (max([p["sort_index"] for p in items]) + 1
                                     if items else 0)
        items.append(cleaned)
    items.sort(key=lambda p: (p["sort_index"], p["id"]))
    _put_providers(client_id, items, lib, path)
    return cleaned


def delete_provider(client_id, pid, library=None, path=None):
    """Remove a provider. Refuses to drop the one currently in use."""
    lib = _resolve(library, path)
    before = _bucket(client_id, lib)
    after = [p for p in before if p["id"] != pid]
    if len(after) == len(before):
        raise ProviderError("没有找到供应商：%s" % pid)
    if lib["current"].get(client_id) == pid:
        lib["current"].pop(client_id, None)
    _put_providers(client_id, after, lib, path)
    return True


def _bucket(client_id, lib):
    """The mutable, normalized provider list for one client inside ``lib``."""
    if client_id not in CLIENTS:
        raise ProviderError("未知的客户端：%s" % client_id)
    bucket = lib["providers"].get(client_id) or []
    return [p for p in (_clean_provider(raw, client_id) for raw in bucket) if p]


def current_id(client_id, library=None, path=None):
    lib = _resolve(library, path)
    return lib["current"].get(client_id) or ""


# ---------------------------------------------------------------------------
# Live read / write, per client format
# ---------------------------------------------------------------------------
def _sorted_json(value):
    """Recursively sort object keys, matching CC Switch's pretty writer."""
    if isinstance(value, dict):
        return {key: _sorted_json(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sorted_json(item) for item in value]
    return value


def _json_text(value):
    return json.dumps(_sorted_json(value), ensure_ascii=False, indent=2)


#: Keys that belong to our own bookkeeping and must never reach a live file.
_INTERNAL_KEYS = ("api_format", "apiFormat", "openrouter_compat_mode",
                  "openrouterCompatMode", "cost_multiplier", "pricing_model_source")


def _sanitize_for_live(config):
    """Drop our own bookkeeping keys (leading underscore) and known internals."""
    return {key: value for key, value in (config or {}).items()
            if key not in _INTERNAL_KEYS and not str(key).startswith("_")}


def read_live(client_id, base=None):
    """The client's current on-disk configuration, in library shape.

    Returns a ``settings_config`` dict ready to be stored as a provider - this
    is what feeds the backfill that keeps user settings from being lost.
    """
    if client_id not in CLIENTS:
        raise ProviderError("未知的客户端：%s" % client_id)
    spec = CLIENTS[client_id]
    fmt = spec["format"]

    if fmt == "claude-json":
        target = _claude_settings_path(base)
        text = _read_text(target)
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except Exception:
            return {}
        return _sanitize_for_live(data) if isinstance(data, dict) else {}

    if fmt == "codex-toml":
        target = _guarded(client_id, base)
        text = _read_text(target)
        if not text.strip():
            return {"auth": {}, "config": ""}
        active = _toml_value(text, "model_provider") or "custom"
        block = _toml_section(text, "model_providers." + active)
        base_url = _toml_value(block or "", "base_url")
        token = _toml_value(block or "", "experimental_bearer_token")
        auth_path = _sidecar(client_id, spec["auth_file"], base)
        auth_text = _read_text(auth_path)
        auth = {}
        try:
            parsed = json.loads(auth_text) if auth_text.strip() else {}
            if isinstance(parsed, dict):
                auth = parsed
        except Exception:
            auth = {}
        return {"auth": auth, "config": text, "base_url": base_url,
                "model_provider": active, "bearer_token": token,
                "_snapshot": True}

    if fmt == "gemini-env":
        env_text = _read_text(_guarded(client_id, base))
        settings_text = _read_text(_sidecar(client_id, spec["settings_file"], base))
        env = {}
        for key in ("GOOGLE_GEMINI_BASE_URL", "GEMINI_API_KEY", "GEMINI_MODEL",
                    "GOOGLE_API_KEY"):
            value = _env_value(env_text, key)
            if value:
                env[key] = value
        config = {}
        try:
            parsed = json.loads(settings_text) if settings_text.strip() else {}
            if isinstance(parsed, dict):
                config = parsed
        except Exception:
            config = {}
        return {"env": env, "config": config}

    raise ProviderError("未知的格式：%s" % fmt)


def _claude_settings_path(base=None):
    """Claude Code's live settings file.

    Honours the legacy ``claude.json`` only when ``settings.json`` is absent,
    which is the same precedence CC Switch uses.
    """
    primary = _guarded("claude", base)
    if os.path.isfile(primary):
        return primary
    legacy = os.path.join(client_home("claude", base), CLIENTS["claude"]["legacy_file"])
    if os.path.isfile(legacy):
        return legacy
    return primary


def write_live(client_id, config, base=None):
    """Project a provider's ``settings_config`` onto the client's real files.

    Returns the list of paths written, so the caller can report exactly what
    changed rather than guessing.
    """
    spec = CLIENTS.get(client_id)
    if not spec:
        raise ProviderError("未知的客户端：%s" % client_id)
    fmt = spec["format"]
    written = []

    if fmt == "claude-json":
        target = _claude_settings_path(base)
        _backup_once(target)
        _write_atomic(target, _json_text(_sanitize_for_live(config)))
        written.append(target)

    elif fmt == "codex-toml":
        target = _guarded(client_id, base)
        _backup_once(target)
        text = _write_codex(client_id, target, config, base)
        _write_atomic(target, text)
        written.append(target)

    elif fmt == "gemini-env":
        env_path = _guarded(client_id, base)
        settings_path = _sidecar(client_id, spec["settings_file"], base)
        _backup_once(env_path)
        env_text = _write_gemini_env(env_path, config.get("env") or {})
        _write_atomic(env_path, env_text)
        written.append(env_path)
        # Read the settings text before sanitizing: it lives under an internal
        # key and would be dropped by the filter.
        next_settings = _gemini_settings(config)
        if next_settings is not None:
            _backup_once(settings_path)
            _write_atomic(settings_path, _json_text(next_settings))
            written.append(settings_path)

    else:
        raise ProviderError("未知的格式：%s" % fmt)

    return written


def _write_codex(client_id, target, config, base):
    """Codex: patch `model_provider` + the provider table, key inside the table.

    Everything else in config.toml - other tables, comments, user keys - is
    carried over untouched. The key goes in as `experimental_bearer_token`
    because that is the field Codex reads for a custom provider; writing it to
    a made-up shape in auth.json (what an earlier version did) is ignored by
    the CLI.

    Two modes, chosen by what the provider actually carries:

    * a snapshot of a real file (`config` holds the whole TOML text and there is
      no explicit base_url) is written back **verbatim**, so reverting to a
      captured provider restores the file exactly, down to the comments;
    * a provider described by fields (base_url / bearer_token / model) is
      **patched** into the existing file.
    """
    text = _read_text(target)
    provided = config.get("config")
    base_url = str(config.get("base_url") or "")

    # A snapshot of a real file is written back verbatim; a provider described
    # by fields is patched into the file that is already there.
    if config.get("_snapshot") and isinstance(provided, str) and provided.strip():
        return provided if provided.endswith("\n") else provided + "\n"

    provider_id = config.get("provider_id") or GATEWAY_ID
    token = str(config.get("bearer_token") or config.get("api_key") or "")
    name = str(config.get("name") or GATEWAY_NAME)
    model = config.get("model")

    body = ['name = "%s"' % _toml_escape(name),
            'base_url = "%s"' % _toml_escape(base_url),
            'wire_api = "responses"']
    if token:
        body.append('experimental_bearer_token = "%s"' % _toml_escape(token))
    if config.get("requires_openai_auth") is not None:
        body.append("requires_openai_auth = %s"
                    % ("true" if config["requires_openai_auth"] else "false"))
    text = _set_toml_section(text, "model_providers." + provider_id, "\n".join(body))
    text = _set_toml_value(text, "model_provider", provider_id)
    if model:
        text = _set_toml_value(text, "model", str(model))
    return text


def _write_gemini_env(path, env):
    """Gemini `.env`: keys sorted, newline-joined, no trailing newline.

    That exact shape is what CC Switch's `serialize_env_file` produces; keys we
    do not own are kept instead of being dropped, which is strictly safer.
    """
    existing = _read_text(path)
    merged = {}
    for key in _env_keys(existing):
        merged[key] = _env_value(existing, key)
    for key, value in (env or {}).items():
        if value:
            merged[str(key)] = str(value)
    lines = ["%s=%s" % (key, merged[key]) for key in sorted(merged)]
    return "\n".join(lines)


def _gemini_settings(config):
    """Gemini `settings.json`: merge the provider config, then stamp auth type.

    The stamp matters. Without `security.auth.selectedType = "gemini-api-key"`
    the CLI keeps using its OAuth login and ignores the base URL we just wrote,
    which looks like "the gateway is broken" from the outside.
    """
    raw = config.get("config")
    existing_text = ""
    if isinstance(config.get("_settings_text"), str):
        existing_text = config["_settings_text"]
    base = {}
    if existing_text.strip():
        try:
            parsed = json.loads(existing_text)
            if isinstance(parsed, dict):
                base = parsed
        except Exception:
            base = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            base[key] = value
    elif raw is None and not base and not (config.get("env") or {}):
        # Nothing to stamp and nothing to protect: leave the file alone.
        return None
    security = base.get("security")
    if not isinstance(security, dict):
        security = {}
    auth = security.get("auth")
    if not isinstance(auth, dict):
        auth = {}
    auth["selectedType"] = "gemini-api-key"
    security["auth"] = auth
    base["security"] = security
    return base


def _toml_escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Gateway provider
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


def gateway_provider(client_id, port=None, key=None, name=None, live=None):
    """Build (but do not store) the provider entry for this gateway.

    ``live`` is the client's current on-disk config. It is merged in rather than
    replaced: Claude Code's file is written whole, so a provider built from
    scratch would silently drop the user's theme, permissions and hooks the
    moment they switch to us. Carrying the live config forward keeps those
    settings attached to the gateway provider.
    """
    if client_id not in CLIENTS:
        raise ProviderError("未知的客户端：%s" % client_id)
    endpoint = gateway_endpoint(port=port)
    if key is None:
        key = gateway_key()
    key = key or ""
    live = live or {}

    if client_id == "claude":
        config = dict(live)
        env = dict(config.get("env") or {})
        env["ANTHROPIC_BASE_URL"] = endpoint
        if key:
            env["ANTHROPIC_AUTH_TOKEN"] = key
        else:
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        config["env"] = env
    elif client_id == "codex":
        config = {"provider_id": GATEWAY_ID, "name": name or GATEWAY_NAME,
                  "base_url": endpoint, "bearer_token": key,
                  "requires_openai_auth": False}
    else:
        config = {"env": {"GOOGLE_GEMINI_BASE_URL": endpoint}}
        if key:
            config["env"]["GEMINI_API_KEY"] = key
        if live.get("config"):
            config["config"] = live["config"]

    return {
        "id": GATEWAY_ID,
        "client": client_id,
        "name": name or (GATEWAY_NAME + " 网关"),
        "settings_config": config,
        "website_url": "",
        "category": "custom",
        "notes": "本机运行的网关，由程序自动维护",
        "in_failover_queue": False,
        "builtin": True,
    }


def ensure_gateway(client_id, port=None, key=None, library=None, path=None,
                   base=None):
    """Make sure the gateway exists in the library, refreshed to the live port."""
    live = read_live(client_id, base=base)
    provider = gateway_provider(client_id, port=port, key=key, live=live)
    return upsert_provider(client_id, provider, library=library, path=path)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def _url_port(url):
    from urllib.parse import urlparse
    try:
        return urlparse(url or "").port
    except Exception:
        return None


def _host_port(url):
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url or "")
        return parsed.hostname, (parsed.port or (443 if parsed.scheme == "https" else 80))
    except Exception:
        return None, None


def current_provider(client_id, base=None, port=None):
    """Which provider the client is currently pointed at.

    ``port`` must match whatever apply() wrote: detection and writing resolve
    the port the same way - the live value first, then the saved preference -
    so a client pointed at our gateway is never reported as pointing elsewhere.
    """
    spec = CLIENTS.get(client_id)
    if not spec:
        raise ProviderError("未知的客户端：%s" % client_id)
    target = _guarded(client_id, base)
    fmt = spec["format"]
    info = {"base_url": "", "has_key": False, "is_gateway": False,
            "file": target, "exists": os.path.isfile(target),
            "provider_id": "", "provider_name": ""}

    live = read_live(client_id, base=base)
    if fmt == "claude-json":
        env = live.get("env") or {}
        info["base_url"] = str(env.get("ANTHROPIC_BASE_URL") or "")
        info["has_key"] = bool(env.get("ANTHROPIC_AUTH_TOKEN")
                               or env.get("ANTHROPIC_API_KEY"))
    elif fmt == "codex-toml":
        info["base_url"] = str(live.get("base_url") or "")
        info["provider_id"] = str(live.get("model_provider") or "")
        if not info["base_url"] and live.get("model_provider"):
            block = _toml_section(_read_text(target),
                                  "model_providers." + live["model_provider"])
            info["base_url"] = _toml_value(block, "base_url")
        info["has_key"] = bool(live.get("bearer_token"))
        info["auth_file_present"] = bool(
            os.path.isfile(_sidecar(client_id, spec["auth_file"], base)))
    elif fmt == "gemini-env":
        env = live.get("env") or {}
        info["base_url"] = str(env.get("GOOGLE_GEMINI_BASE_URL") or "")
        info["has_key"] = bool(env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"))
        settings = live.get("config") or {}
        selected = ((settings.get("security") or {}).get("auth") or {}).get("selectedType")
        info["auth_selected"] = selected or ""
        # A base URL without the auth stamp is a half-done switch: the CLI will
        # keep its OAuth login and ignore us. Surface that instead of hiding it.
        info["needs_auth_stamp"] = bool(info["base_url"]) and selected != "gemini-api-key"

    mine_host, mine_port = _host_port(gateway_endpoint(port=port))
    theirs_host, theirs_port = _host_port(info["base_url"])
    info["gateway_host"] = mine_host
    info["gateway_port"] = mine_port
    info["is_gateway"] = bool(
        theirs_host and mine_host and theirs_host == mine_host
        and theirs_port == mine_port)
    return info


def status(base=None, port=None, path=None):
    """Status for every supported client."""
    out = {}
    lib = load_library(path) if path else load_library()
    for client_id in CLIENTS:
        try:
            info = current_provider(client_id, base=base, port=port)
        except Exception as exc:
            info = {"error": str(exc), "is_gateway": False, "base_url": "",
                    "has_key": False}
        info["name"] = CLIENTS[client_id]["name"]
        info["note"] = CLIENTS[client_id]["note"]
        info["installed"] = bool(os.path.isdir(client_home(client_id, base)))
        try:
            info["provider_count"] = len(_bucket(client_id, lib))
            info["current_id"] = lib["current"].get(client_id) or ""
            info["has_backup"] = has_backup(client_id, base=base)
        except Exception:
            info["provider_count"] = 0
            info["current_id"] = ""
        out[client_id] = info
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
    lines = (text or "").splitlines()
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
    if pattern.search(text or ""):
        return pattern.sub(line, text, count=1)

    lines = (text or "").splitlines()
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
    lines = (text or "").splitlines()
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


def _env_keys(text):
    out = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if key:
            out.append(key)
    return out


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


# ---------------------------------------------------------------------------
# Switch / apply / revert
# ---------------------------------------------------------------------------
def _seed_from_live(client_id, library=None, path=None, base=None):
    """Capture the client's current config as a provider, once.

    Without this the first switch would replace the user's whole settings file
    with the gateway block. CC Switch solves it by backfilling the outgoing
    provider; when there is no outgoing provider yet, this creates one so the
    original state is always reachable.
    """
    lib = library if library is not None else load_library()
    if list_providers(client_id, lib):
        return lib, None
    live = read_live(client_id, base=base)
    if not live:
        return lib, None
    captured = {
        "id": "captured-%d" % int(time.time()),
        "client": client_id,
        "name": "切换前的原配置",
        "settings_config": live,
        "category": "custom",
        "notes": "本程序第一次接管时保存的现场，可随时切回",
        "sort_index": 0,
    }
    upsert_provider(client_id, captured, library=lib, path=path)
    return lib, captured


def switch_to(client_id, pid, base=None, library=None, path=None, port=None):
    """Point a client at one of its stored providers.

    Order matters and follows the same sequence CC Switch uses:

    1. backfill - save the client's live config into the provider being left,
       so nothing the user changed by hand is lost;
    2. write the new provider's config to the live files;
    3. only then move the "current" pointer.
    """
    if client_id not in CLIENTS:
        raise ProviderError("未知的客户端：%s" % client_id)
    lib = _resolve(library, path)
    items = _bucket(client_id, lib)
    target = None
    for item in items:
        if item["id"] == pid:
            target = item
            break
    if not target:
        raise ProviderError("没有找到供应商：%s" % pid)

    leaving = current_id(client_id, lib)
    if leaving and leaving != pid:
        for index, item in enumerate(items):
            if item["id"] == leaving:
                live = read_live(client_id, base=base)
                if live:
                    merged = dict(item["settings_config"])
                    merged.update(live)
                    items[index] = dict(item, settings_config=merged)
                break
        _put_providers(client_id, items, lib, path)

    config = dict(target["settings_config"])
    if client_id == "gemini":
        # The settings merge needs the current file to preserve mcpServers etc.
        config["_settings_text"] = _read_text(
            _sidecar("gemini", CLIENTS["gemini"]["settings_file"], base))
    written = write_live(client_id, config, base=base)
    for item in items:
        if item["id"] == pid:
            item["settings_config"] = _sanitize_for_live(config)
    _put_providers(client_id, items, lib, path)
    lib["current"][client_id] = pid
    save_library(lib, path)
    return {"client": client_id, "provider": target["name"], "provider_id": pid,
            "written": written}


def apply(client_id, port=None, key=None, base=None, path=None):
    """Ensure the gateway provider exists and point the client at it."""
    lib = load_library(path) if path else load_library()
    lib, captured = _seed_from_live(client_id, lib, path, base=base)
    live = read_live(client_id, base=base)
    provider = gateway_provider(client_id, port=port, key=key, live=live)
    upsert_provider(client_id, provider, library=lib, path=path)
    result = switch_to(client_id, GATEWAY_ID, base=base, path=path, port=port)
    result["backup"] = _backup_path(client_id, base)
    result["captured"] = captured["id"] if captured else ""
    return result


def preview(client_id, port=None, key=None, base=None):
    """Show what applying the gateway would change, without touching anything."""
    spec = CLIENTS.get(client_id)
    if not spec:
        raise ProviderError("未知的客户端：%s" % client_id)
    endpoint = gateway_endpoint(port=port)
    key = key if key is not None else gateway_key()
    before = current_provider(client_id, base=base, port=port)
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
        "written_files": _live_files(client_id, base),
    }


def _live_files(client_id, base=None):
    """Every file write_live() may touch for this client."""
    spec = CLIENTS[client_id]
    out = [_guarded(client_id, base)]
    if spec.get("auth_file"):
        out.append(_sidecar(client_id, spec["auth_file"], base))
    if spec.get("settings_file"):
        out.append(_sidecar(client_id, spec["settings_file"], base))
    return out


def _backup_path(client_id, base=None):
    try:
        return _guarded(client_id, base) + BACKUP_SUFFIX
    except Exception:
        return ""


def revert(client_id, base=None):
    """Restore a client's files from the backups this module made."""
    targets = _live_files(client_id, base)
    restored = []
    for target in targets:
        backup = target + BACKUP_SUFFIX
        if os.path.isfile(backup):
            try:
                shutil.copy2(backup, target)
                restored.append(target)
            except Exception as exc:
                raise ProviderError("还原失败：%s" % exc)
    if not restored:
        raise ProviderError("没有找到备份，无法还原")
    lib = load_library()
    if lib["current"].get(client_id) == GATEWAY_ID:
        lib["current"].pop(client_id, None)
        save_library(lib)
    return {"client": client_id, "restored": restored}


def has_backup(client_id, base=None):
    """True when a restorable backup exists for this client."""
    try:
        for target in _live_files(client_id, base):
            if os.path.isfile(target + BACKUP_SUFFIX):
                return True
    except Exception:
        pass
    return False
