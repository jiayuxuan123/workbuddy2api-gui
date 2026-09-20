"""wb_custom_providers.py —— 自定义模型提供商库与模型自动探寻.

用户除了 WorkBuddy 官方账号池，还可以接入第三方 AI API（OpenAI 兼容聚合站、
DeepSeek/Kimi/GLM 等官方供应商）。每个提供商记录:

    {
        "id": "siliconflow",
        "name": "硅基流动",
        "protocol": "openai",          # openai | anthropic | gemini
        "base_url": "https://api.siliconflow.cn",
        "api_key": "sk-...",           # 明文存库文件（与 accounts/ 同级目录）
        "models": ["deepseek-ai/DeepSeek-V3", ...],   # 探寻结果或手填
        "enabled": True,
        "created_at": 1789890000.0,
        "last_probe": {...},           # 最近一次探寻摘要（不含 key）
    }

模型探寻参考 cc-switch 的 model_fetch.rs：按协议选认证头，从 base_url
构造候选模型端点（/v1/models、版本段判定、Anthropic 兼容后缀剥离），
逐个尝试直到成功。404/405 换下一个候选，其余错误直接上报。

只用标准库。
"""

import json
import os
import re
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

#: 支持的 API 协议。决定认证头与端点形态。
PROTOCOLS = ("openai", "anthropic", "gemini")

#: 库文件名（位于数据目录）。
LIBRARY_NAME = "custom-providers.json"

#: 探寻超时（秒）。探测是面板上的交互动作，不宜久等。
PROBE_TIMEOUT = 12.0

#: 允许出站的 URL scheme。网关整体安全策略只放行 http/https。
_ALLOWED_SCHEMES = ("http", "https")

#: 出站代理环境变量，与 wb_update.py / wb_pricing.py 同一约定。
PROXY_ENV = "WB_UPDATE_PROXY"

_lock = threading.Lock()

# Anthropic 协议挂兼容子路径的已知后缀（照搬 cc-switch，按长度降序，
# 最长前缀优先匹配，否则 /anthropic 会提前吃掉 /api/anthropic）。
KNOWN_COMPAT_SUFFIXES = (
    "/api/claudecode",
    "/api/anthropic",
    "/apps/anthropic",
    "/api/coding",
    "/claudecode",
    "/anthropic",
    "/step_plan",
    "/coding",
    "/claude",
)

_URL_RE = re.compile(r"^https?://[^\s/?#]+", re.IGNORECASE)
_VERSION_SEG_RE = re.compile(r"^v\d+$")


class ProviderError(Exception):
    """A failure with a message worth showing to the user."""


# ---------------------------------------------------------------------------
# 出站请求（带代理支持；SSRF 防护：只允许 http/https、拒绝环回/私有地址）
# ---------------------------------------------------------------------------
def _proxy_opener():
    """Build an opener honouring WB_UPDATE_PROXY, like wb_update._opener()."""
    proxy = os.environ.get(PROXY_ENV, "").strip()
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def _check_url(url):
    """出站前校验 URL：scheme 白名单，拒绝内网/环回目标。

    只对 **IP 字面量** 做严格公网校验；域名不做 DNS 解析校验——本机开着
    fake-ip 代理时（Clash/Surge 等），任何域名都会解析进 198.18.0.0/15
    基准段，解析校验会拦掉所有正常供应商（实测 example.com -> 198.18.x.x）。
    域名场景退而求其次：拦 localhost 及常见本地后缀。探测是面板口令后面的
    交互动作，调用者本来就有管理权限，这里防的是配置手误和最低限度的 SSRF。
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ProviderError("仅支持 http/https 地址：%s" % url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise ProviderError("URL 缺少主机名：%s" % url)
    if host == "localhost" or host.endswith(".localhost") \
            or host.endswith(".local") or host.endswith(".lan") \
            or host.endswith(".home.arpa") or host.endswith(".internal"):
        raise ProviderError("拒绝访问本地地址：%s" % host)
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host) or ":" in host:
        # IP 字面量（v4 或 v6）：必须解析成合法 IP 且是公网地址。
        try:
            import ipaddress
            addr = ipaddress.ip_address(host)
        except ValueError:
            raise ProviderError("拒绝畸形 IP 地址：%s" % host)
        if not addr.is_global:
            raise ProviderError("拒绝访问内网/环回地址：%s" % host)
    return url


def _http_get_json(url, headers=None, timeout=PROBE_TIMEOUT):
    """GET 一个 JSON，带 WB_UPDATE_PROXY 代理支持与 SSRF 校验。"""
    _check_url(url)
    req = urllib.request.Request(url, method="GET",
                                 headers=headers or {})
    try:
        with _proxy_opener().open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(512).decode("utf-8", "replace")
        except Exception:
            pass
        raise ProviderError("HTTP %s: %s" % (exc.code, body[:200])) from exc
    except Exception as exc:
        raise ProviderError("请求失败: %s" % exc) from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ProviderError("响应不是合法 JSON：%s" % raw[:120]) from exc


# ---------------------------------------------------------------------------
# 模型端点候选构造（移植自 cc-switch model_fetch.rs）
# ---------------------------------------------------------------------------
def ends_with_version_segment(url):
    """base_url 以 /v{N} 结尾（/v1、/v4 ...）时模型端点应为 {base}/models。"""
    last = url.rstrip("/").rsplit("/", 1)[-1]
    return bool(_VERSION_SEG_RE.match(last))


def strip_compat_suffix(base_url):
    """命中 Anthropic 兼容后缀时返回剥离后的根；否则 None。"""
    trimmed = base_url.rstrip("/")
    for suffix in KNOWN_COMPAT_SUFFIXES:
        if trimmed.endswith(suffix):
            return trimmed[: len(trimmed) - len(suffix)]
    return None


def build_models_url_candidates(base_url, models_url_override=None):
    """从 base_url 构造候选模型端点列表（已去重、按优先序）。

    顺序照搬 cc-switch：
    1. 显式 override 非空 -> 只用它
    2. base 以 /v{N} 结尾 -> {base}/models（版本号已在路径里）；
       非 /v1 时再补 {base}/v1/models 兜底
    3. 常规 -> {base}/v1/models
    4. 命中 Anthropic 兼容后缀 -> 剥离后缀再拼 /v1/models、/models
    """
    if models_url_override and models_url_override.strip():
        return [models_url_override.strip()]

    trimmed = base_url.strip().rstrip("/")
    if not trimmed:
        raise ProviderError("Base URL 不能为空")

    candidates = []
    if ends_with_version_segment(trimmed):
        candidates.append(trimmed + "/models")
        if not trimmed.endswith("/v1"):
            candidates.append(trimmed + "/v1/models")
    else:
        candidates.append(trimmed + "/v1/models")

    stripped = strip_compat_suffix(trimmed)
    if stripped:
        root = stripped.rstrip("/")
        if root:
            candidates.append(root + "/v1/models")
            candidates.append(root + "/models")

    unique = []
    for url in candidates:
        if url not in unique:
            unique.append(url)
    return unique


def auth_headers(protocol, api_key):
    """按协议选认证头，对齐 cc-switch build_model_fetch_headers。"""
    key = (api_key or "").strip()
    headers = {"Accept": "application/json",
               "User-Agent": "WorkBuddy2API/1.7 model-probe"}
    if not key:
        return headers
    if protocol == "anthropic":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
    elif protocol == "gemini":
        headers["x-goog-api-key"] = key
    else:
        headers["Authorization"] = "Bearer " + key
    return headers


def _extract_models(data):
    """兼容两种响应形状：OpenAI 的 data[] 与智谱 Responses 的 models[].slug。"""
    models = []
    if isinstance(data, dict):
        entries = data.get("data")
        if isinstance(entries, list) and entries:
            for entry in entries:
                mid = ""
                if isinstance(entry, dict):
                    mid = str(entry.get("id") or "").strip()
                elif isinstance(entry, str):
                    mid = entry.strip()
                if mid:
                    models.append(mid)
        else:
            # OpenAI 兼容站给 data[]；智谱 Responses 形状给 models[].slug。
            # data 缺失或为空列表时都回退到 models，二者取其一。
            zhipu = data.get("models")
            if isinstance(zhipu, list):
                for entry in zhipu:
                    mid = ""
                    if isinstance(entry, dict):
                        mid = str(entry.get("slug") or entry.get("id") or "").strip()
                    elif isinstance(entry, str):
                        mid = entry.strip()
                    if mid:
                        models.append(mid)
    elif isinstance(data, list):
        for entry in data:
            mid = ""
            if isinstance(entry, dict):
                mid = str(entry.get("id") or "").strip()
            elif isinstance(entry, str):
                mid = entry.strip()
            if mid:
                models.append(mid)
    # 去重保序
    seen = set()
    out = []
    for mid in models:
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


def probe_models(base_url, api_key, protocol="openai", models_url_override=None):
    """探寻一个提供商的模型列表。返回 (models, tried)。

    tried 是 [(url, ok, detail)]，供前端展示尝试路径。
    404/405 换下一个候选；其它错误直接报（网络不通/401 无重试意义）。
    """
    if protocol not in PROTOCOLS:
        raise ProviderError("未知协议：%s" % protocol)
    headers = auth_headers(protocol, api_key)
    candidates = build_models_url_candidates(base_url, models_url_override)
    tried = []
    last_err = None
    for url in candidates:
        try:
            data = _http_get_json(url, headers=headers)
        except ProviderError as exc:
            detail = str(exc)
            tried.append((url, False, detail))
            # 404/405 才换候选；其余错误直接失败。
            if "HTTP 404" in detail or "HTTP 405" in detail:
                last_err = detail
                continue
            raise ProviderError("%s（%s）" % (detail, url)) from exc
        models = _extract_models(data)
        tried.append((url, True, "%d 个模型" % len(models)))
        return models, tried
    raise ProviderError("所有候选端点都不可达（最后错误: %s）" % (last_err or "无候选"))


# ---------------------------------------------------------------------------
# 提供商库（custom-providers.json，位于数据目录）
# ---------------------------------------------------------------------------
def data_dir():
    try:
        import wb_runtime
        return wb_runtime.data_dir()
    except Exception:
        return os.path.expanduser("~")


def library_path():
    return os.path.join(data_dir(), LIBRARY_NAME)


def _write_atomic(target, text):
    folder = os.path.dirname(target)
    os.makedirs(folder, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=".wbcust-", suffix=".tmp", dir=folder)
    try:
        os.write(handle, text.encode("utf-8"))
    finally:
        os.close(handle)
    os.replace(temp, target)


def _clean_provider(raw):
    """Normalize one stored provider; returns None when unusable."""
    if not isinstance(raw, dict):
        return None
    pid = str(raw.get("id") or "").strip()
    if not pid:
        return None
    protocol = str(raw.get("protocol") or "openai").strip().lower()
    if protocol not in PROTOCOLS:
        protocol = "openai"
    try:
        enabled = raw.get("enabled") is not False
    except Exception:
        enabled = True
    models = raw.get("models")
    if not isinstance(models, list):
        models = []
    models = [str(m).strip() for m in models if str(m or "").strip()]
    return {
        "id": pid,
        "name": str(raw.get("name") or pid).strip() or pid,
        "protocol": protocol,
        "base_url": str(raw.get("base_url") or "").strip().rstrip("/"),
        "api_key": str(raw.get("api_key") or ""),
        "models": models,
        "enabled": enabled,
        "notes": str(raw.get("notes") or ""),
        "created_at": float(raw.get("created_at") or time.time()),
        "last_probe": raw.get("last_probe") if isinstance(raw.get("last_probe"), dict) else None,
    }


def load_library(path=None):
    """Read the provider library, tolerating a missing or damaged file."""
    target = path or library_path()
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, ValueError):
        return {"version": 1, "providers": []}
    if not isinstance(data, dict):
        return {"version": 1, "providers": []}
    providers = data.get("providers")
    if not isinstance(providers, list):
        providers = []
    cleaned = [c for c in (_clean_provider(raw) for raw in providers) if c]
    return {"version": 1, "providers": cleaned}


def save_library(library, path=None):
    target = path or library_path()
    _write_atomic(target, json.dumps(library, ensure_ascii=False, indent=2))
    return target


def list_providers(path=None, include_key=False):
    """全部提供商；include_key=False 时脱敏 api_key。"""
    lib = load_library(path)
    out = []
    for p in lib["providers"]:
        if not include_key:
            p = dict(p)
            key = p.get("api_key") or ""
            p["api_key"] = (key[:4] + "***" + key[-4:]) if len(key) > 8 \
                else ("***" if key else "")
        out.append(p)
    return out


def get_provider(pid, path=None):
    for p in load_library(path)["providers"]:
        if p["id"] == pid:
            return p
    raise ProviderError("没有找到提供商：%s" % pid)


def upsert_provider(provider, path=None):
    """新增或更新一个提供商。返回（清洗后的记录, 是否新建）。"""
    cleaned = _clean_provider(provider)
    if not cleaned:
        raise ProviderError("提供商缺少 id")
    with _lock:
        lib = load_library(path)
        items = lib["providers"]
        replaced = False
        for index, existing in enumerate(items):
            if existing["id"] == cleaned["id"]:
                # 更新时若未显式给 key，保留原 key（前端脱敏回传 "***"）。
                # 掩码形态有两种：纯 "***" 与部分掩码 "sk-a***6789"，
                # 都视为「没改 key」。
                key_in = cleaned["api_key"]
                if not key_in or "***" in key_in \
                        or set(key_in) <= {"*", "-"}:
                    cleaned["api_key"] = existing["api_key"]
                if cleaned["base_url"] == existing["base_url"] \
                        and not cleaned["models"]:
                    cleaned["models"] = existing["models"]
                items[index] = cleaned
                replaced = True
                break
        if not replaced:
            items.append(cleaned)
        save_library(lib, path)
    return cleaned, not replaced


def delete_provider(pid, path=None):
    with _lock:
        lib = load_library(path)
        before = len(lib["providers"])
        lib["providers"] = [p for p in lib["providers"] if p["id"] != pid]
        if len(lib["providers"]) == before:
            raise ProviderError("没有找到提供商：%s" % pid)
        save_library(lib, path)
    return True


def set_enabled(pid, enabled, path=None):
    with _lock:
        lib = load_library(path)
        for p in lib["providers"]:
            if p["id"] == pid:
                p["enabled"] = bool(enabled)
                save_library(lib, path)
                return True
    raise ProviderError("没有找到提供商：%s" % pid)


def probe_and_store(pid, path=None):
    """探寻某提供商的模型并写回 models / last_probe。返回更新后的记录。"""
    p = get_provider(pid, path)
    models, tried = probe_models(p["base_url"], p["api_key"], p["protocol"])
    with _lock:
        lib = load_library(path)
        for item in lib["providers"]:
            if item["id"] == pid:
                item["models"] = models
                item["last_probe"] = {
                    "at": time.time(),
                    "count": len(models),
                    # 不存完整 tried，只留端点与成败，避免 key 出现在错误串里
                    "tried": [{"url": u, "ok": ok, "detail": d[:120]}
                              for u, ok, d in tried],
                }
                save_library(lib, path)
                return item
    raise ProviderError("没有找到提供商：%s" % pid)
