# -*- coding: utf-8 -*-
"""wb_pricing.py —— 模型价目表：远程拉取 + 本地缓存 + 内嵌快照兜底。

成本统计需要 USD/百万 token 价目。本项目自拟价目不可靠，因此改为自动
从网上拉取：

* 主源：LiteLLM 官方价目 JSON（model_prices_and_context_window.json，
  4000+ 模型、社区维护、更新勤）。国内直连 raw.githubusercontent 常常
  不通，因此按实测延迟依次尝试一组镜像（jsDelivr 各边缘节点 → 官方
  raw → ghproxy 中转），并遵循与 wb_update.py 相同的 ``WB_UPDATE_PROXY``
  代理约定；
* 副源：OpenRouter /api/v1/models（pricing.prompt / pricing.completion，
  单位是 USD/token 字符串）；
* 兜底：随包内嵌一份生成时抓取的快照（EMBEDDED_PRICES），断网也能算；
* 最后防线：未知模型按兜底价计（FALLBACK_PRICE），并在响应里标记
  ``estimated=true``。

拉取结果写到数据目录 ``usage/prices-cache.json``，带 TTL（默认 24h）。

联网绝不占用请求线程：get_table() 只读内存 / 缓存文件 / 内嵌快照，
远程刷新由守护线程在后台完成（成功后至多每 CACHE_TTL 一次，失败按
RETRY_INTERVAL 退避重试）；只有面板「刷新价目表」按钮走同步拉取，
且同样在模块锁之外进行，不阻塞其它请求。

计价语义（与 LiteLLM 一致，单位 USD/token）：

* ``input``   × prompt_tokens（含缓存未命中部分）；
* ``output``  × completion_tokens；
* ``cache_read`` × cached_tokens（缓存命中通常打折）；
* reasoning_tokens 已包含在 completion_tokens 里（OpenAI 语义），不重复计。

价目匹配顺序：精确名 → 归一化名（小写、去版本尾缀）→ 目录映射表
（WorkBuddy 别名 → LiteLLM 键）→ 前缀/子串匹配 → 兜底价。
"""

import json
import os
import re
import threading
import time
import urllib.request

#: 随包快照：生成自 LiteLLM 2025-07 抓取，仅作断网兜底，可被缓存覆盖。
EMBEDDED_PRICES = {
    "glm-5.3": {"input": 1.1268e-06, "output": 3.9438e-06, "cache_read": 2.817e-07},
    "glm-5.2": {"input": 1.4e-06, "output": 4.4e-06, "cache_read": 2.6e-07},
    "glm-5.1": {"input": 1.4e-06, "output": 4.4e-06, "cache_read": 2.6e-07},
    "glm-4.6": {"input": 5.5e-07, "output": 2.2e-06, "cache_read": 1.1e-07},
    "glm-4.6v": {"input": 3e-07, "output": 9e-07, "cache_read": 5.5e-08},
    "glm-5.3-flash": {"input": 1.1268e-07, "output": 3.9438e-07, "cache_read": 2.817e-08},
    "glm-5v-turbo": {"input": 7.042e-07, "output": 3.09848e-06, "cache_read": 1.69008e-07},
    "kimi-k3": {"input": 3e-06, "output": 1.5e-05, "cache_read": 3e-07},
    "kimi-k2.5": {"input": 6e-07, "output": 3e-06, "cache_read": 1e-07},
    "kimi-k2.6": {"input": 9.5e-07, "output": 4e-06, "cache_read": 1.6e-07},
    "kimi-k2-thinking": {"input": 6e-07, "output": 2.5e-06, "cache_read": None},
    "minimax-m2.5": {"input": 3e-07, "output": 1.2e-06, "cache_read": 3e-08},
    "minimax-m2.7": {"input": 2.958e-07, "output": 1.1832e-06, "cache_read": 5.916e-08},
    "minimax-m3": {"input": 2.88e-07, "output": 1.152e-06, "cache_read": None},
    "deepseek-v4-flash": {"input": 3e-07, "output": 1.2e-06, "cache_read": 6e-09},
    "deepseek-v4-pro": {"input": 1.32e-06, "output": 3.96e-06, "cache_read": 4.4e-08},
    "deepseek-v4.1-flash": {"input": 1.5e-07, "output": 6e-07, "cache_read": 3e-09},
    "deepseek-v3-0324": {"input": 9e-07, "output": 9e-07, "cache_read": None},
    "deepseek-r1-0528": {"input": 3e-06, "output": 8e-06, "cache_read": None},
    "gpt-5.3-codex": {"input": 1.75e-06, "output": 1.4e-05, "cache_read": 1.75e-07},
    "gpt-5.4": {"input": 2.5e-06, "output": 1.5e-05, "cache_read": 2.5e-07},
    "gpt-5.5": {"input": 5e-06, "output": 3e-05, "cache_read": 5e-07},
    "gpt-5.6-sol": {"input": 4.4e-06, "output": 2.2e-05, "cache_read": 4.4e-07},
    "gpt-5.6-terra": {"input": 2.2e-06, "output": 1.32e-05, "cache_read": 2.2e-07},
    "gpt-5.6-luna": {"input": 2.2e-07, "output": 1.32e-06, "cache_read": 2.2e-08},
    "gpt-6-astra": {"input": 1e-05, "output": 5e-05, "cache_read": 1e-06},
    "gemini-3.5-flash": {"input": 1.5e-06, "output": 9e-06, "cache_read": 1.5e-07},
    "hy3": {"input": 1.562e-07, "output": 6.248e-07, "cache_read": 3.905e-08},
    "hy4-preview": {"input": 8.45e-07, "output": 2.535e-06, "cache_read": 4.225e-08},
}

#: WorkBuddy 目录别名 / 渠道变体 → 基础名的映射。键一律小写。
ALIAS_MAP = {
    # WorkBuddy 自有别名（catalog 里的 default/fast/balanced 等）
    "default-model": "gpt-5.5",
    "fast-model": "glm-5.3-flash",
    "balanced-model": "gpt-5.4",
    "primary-model": "gpt-5.5",
    "deep-model": "deepseek-r1-0528",
    # 渠道后缀变体（volc/lkeap/taiji 是不同渠道，价格按基础模型近似）
    "deepseek-v3-1": "deepseek-v3-0324",
    "deepseek-v3-0324-lkeap": "deepseek-v3-0324",
    "deepseek-v3-1-lkeap": "deepseek-v3-0324",
    "deepseek-v3-1-volc": "deepseek-v3-0324",
    "deepseek-v3-2-volc": "deepseek-v3-0324",
    "deepseek-r1-0528-lkeap": "deepseek-r1-0528",
    "kimi-k2-instruct-taiji": "kimi-k2.5",
    "kimi-k3-1": "kimi-k3",
    "kimi-k2.7": "kimi-k2.6",
    "kimi-k2.8-preview": "kimi-k2.6",
    "hy3-x": "hy3",
    "hy4-preview-f": "hy4-preview",
    "hy4-preview-dev": "hy4-preview",
    "hy4-preview-x": "hy4-preview",
    "glm-5.0-turbo": "glm-5.1",
    "hunyuan-2.0-instruct": None,   # 无对应价目 → 走兜底价
    "hunyuan-chat": None,
    "default-1.1": None,
    "default-1.2": None,
}

#: 未知模型的兜底价（USD/token）：按中档模型估，宁低勿高。
FALLBACK_PRICE = {"input": 1e-06, "output": 3e-06, "cache_read": 1e-07}

#: 缓存有效期（秒）。价目变化不频繁，一天刷一次足够。
CACHE_TTL = 86400.0

#: 远程拉取失败后的重试间隔（秒）。避免断网主机每个请求都撞一次超时。
RETRY_INTERVAL = 1800.0

#: 远程拉取的超时（秒）。镜像链会逐个尝试，单源不宜拖太久。
FETCH_TIMEOUT = 10

#: 可选代理，与 wb_update.py 同一约定（``WB_UPDATE_PROXY``），例如
#: "http://127.0.0.1:7890"。GitHub 直连在国内网络经常不通。
PROXY_ENV = "WB_UPDATE_PROXY"

#: LiteLLM 价目 JSON 的拉取源，按实测延迟排序。前四个是 jsDelivr 的
#: 多家边缘节点（国内可达、免 KEY），然后是官方 raw（有代理时最快），
#: 最后是 ghproxy 中转。内容完全一致，只是传输路径不同。
LITELLM_MIRRORS = (
    "https://gcore.jsdelivr.net/gh/BerriAI/litellm@main/"
    "model_prices_and_context_window.json",
    "https://testingcf.jsdelivr.net/gh/BerriAI/litellm@main/"
    "model_prices_and_context_window.json",
    "https://fastly.jsdelivr.net/gh/BerriAI/litellm@main/"
    "model_prices_and_context_window.json",
    "https://cdn.jsdelivr.net/gh/BerriAI/litellm@main/"
    "model_prices_and_context_window.json",
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json",
    "https://ghproxy.net/https://raw.githubusercontent.com/BerriAI/litellm/"
    "main/model_prices_and_context_window.json",
)

#: 兼容旧名：测试与调用方引用的主源。
LITELLM_URL = LITELLM_MIRRORS[0]
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"

_lock = threading.Lock()
_state = {
    "prices": None,       # model_key(小写) -> {"input","output","cache_read"}
    "source": None,       # "embedded" | "cache" | "litellm" | "openrouter"
    "fetched_at": 0.0,    # epoch of last successful remote pull
    "cache_path": None,
    "last_attempt": 0.0,  # epoch of last remote refresh attempt (any outcome)
    "refreshing": False,  # a background refresh is in flight
}


# ---------------------------------------------------------------- helpers
def _num(value):
    """Coerce a pricing field to float, tolerating strings and None."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


def _normalize(name):
    """Lowercase and strip vendor prefixes / path segments / revisions.

    ``novita/zai-org/glm-4.6`` → ``glm-4.6``; ``bedrock/eu/.../model:0``
    → ``model``; ``openrouter/deepseek/x:free`` → ``x``.
    """
    key = (name or "").strip().lower()
    key = re.sub(r"^(openai|anthropic|google|azure|azure_ai|bedrock|"
                 r"vertex_ai|gemini|mistral|deepseek|moonshot|z-ai|zai|"
                 r"minimax|qwen|groq|fireworks_ai|novita|aihubmix|"
                 r"openrouter|dashscope|cloudflare|together_ai|together|"
                 r"xai|perplexity|anyscale|cerebras|nebius|lambda|"
                 r"hyperbolic|nanogpt|github|github_copilot|oci|jina|"
                 r"voyage|cohere|watsonx|palm|maritaca|nvidia_nim|"
                 r"featherless_ai|meta_llama|lmstudio|sambanova|replicate)"
                 r"/", "", key)
    if "/" in key:                    # remaining org paths: keep model part
        key = key.rsplit("/", 1)[-1]
    key = re.sub(r":.*$", "", key)    # bedrock revision, openrouter :free
    return key.strip()


class PriceTable(object):
    """Resolved price lookup with alias + substring fallback."""

    def __init__(self, prices, source, fetched_at):
        self.prices = prices          # normalized-key -> dict
        self.source = source
        self.fetched_at = fetched_at
        self._keys = sorted(prices.keys(), key=len, reverse=True)

    def get(self, model):
        """Price dict for a model name; None when unknown."""
        key = (model or "").strip().lower()
        if not key:
            return None
        if key in ALIAS_MAP:
            mapped = ALIAS_MAP[key]
            if mapped is None:
                return None
            key = mapped.lower()
        if key in self.prices:
            return self.prices[key]
        norm = _normalize(key)
        if norm in self.prices:
            return self.prices[norm]
        # substring: prefer the longest table key contained in the name
        for table_key in self._keys:
            if table_key and table_key in norm:
                return self.prices[table_key]
        return None


def _entry(inp, outp, cache_read):
    return {
        "input": _num(inp),
        "output": _num(outp),
        "cache_read": _num(cache_read),
    }


def _parse_litellm(data):
    """LiteLLM rows -> {normalized_key: price}; requires both I/O prices.

    Two passes: bare (vendor-prefixed) names win over routed variants, so
    ``gpt-5.5`` keeps OpenAI's official price even though dozens of
    ``<provider>/gpt-5.5`` keys normalize to the same name.
    """
    out = {}
    if not isinstance(data, dict):
        return out
    for routed in (False, True):
        for name, spec in data.items():
            if not isinstance(spec, dict):
                continue
            is_routed = "/" in name
            if is_routed != routed:
                continue
            inp = _num(spec.get("input_cost_per_token"))
            outp = _num(spec.get("output_cost_per_token"))
            if inp is None or outp is None:
                continue
            key = _normalize(name)
            if not key:
                continue
            out[key] = _entry(inp, outp, spec.get("cache_read_input_token_cost"))
    return out


def _parse_openrouter(data):
    """OpenRouter rows (USD/token strings) as a secondary source."""
    out = {}
    rows = (data or {}).get("data")
    if not isinstance(rows, list):
        return out
    for spec in rows:
        if not isinstance(spec, dict):
            continue
        mid = spec.get("id") or ""
        pricing = spec.get("pricing") or {}
        inp = _num(pricing.get("prompt"))
        outp = _num(pricing.get("completion"))
        if inp is None or outp is None or inp <= 0 or outp <= 0:
            continue
        key = _normalize(mid)
        if not key:
            continue
        out.setdefault(key, _entry(inp, outp, None))
    return out


def _parse_embedded(snapshot):
    out = {}
    for name, spec in (snapshot or {}).items():
        key = _normalize(name)
        if key:
            out[key] = _entry(spec.get("input"), spec.get("output"),
                              spec.get("cache_read"))
    return out


# ---------------------------------------------------------------- fetch
def _opener():
    """urlopen wrapper honouring the optional ``WB_UPDATE_PROXY`` setting
    (same convention as wb_update.py; GitHub direct is often unreachable
    from mainland networks)."""
    proxy = (os.environ.get(PROXY_ENV) or "").strip()
    if not proxy:
        return urllib.request.urlopen
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    return urllib.request.build_opener(handler).open


def _fetch_json(url, timeout=FETCH_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "WorkBuddy2API/1.7"})
    with _opener()(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_litellm():
    """Try the LiteLLM price JSON across the mirror chain, in order.

    Returns (parsed, mirror_url); raises when every mirror fails. Mirrors
    serve the identical file, so the first success wins — later entries
    only matter when an edge node is blocked or stale.
    """
    last_exc = None
    for url in LITELLM_MIRRORS:
        try:
            return _fetch_json(url), url
        except Exception as exc:            # noqa: BLE001 - mirror hop
            last_exc = exc
    raise last_exc if last_exc else IOError("no mirrors configured")


def _load_cache(path):
    """Cache file -> PriceTable or None (missing/corrupt/expired)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        table = _parse_embedded(data.get("prices") or {})
        if not table:
            return None
        return PriceTable(table, str(data.get("source") or "cache"),
                          float(data.get("fetched_at") or 0))
    except Exception:
        return None


def _save_cache(path, table):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        payload = {
            "fetched_at": table.fetched_at,
            "source": table.source,
            "saved_at": time.time(),
            # store as-is keyed by normalized name
            "prices": {k: v for k, v in table.prices.items()},
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _resolve_local_locked():
    """In-memory table -> cache file -> embedded snapshot. No network."""
    if _state["prices"] is not None:
        return _state["prices"]
    path = _state["cache_path"]
    if path:
        cached = _load_cache(path)
        if cached:
            _state["prices"] = cached
            _state["source"] = cached.source
            _state["fetched_at"] = cached.fetched_at
            return cached
    # fetched_at=0 marks "never fetched remotely": the background scheduler
    # sees it as immediately due, without blocking this call.
    embedded = PriceTable(_parse_embedded(EMBEDDED_PRICES), "embedded", 0.0)
    _state["prices"] = embedded
    _state["source"] = "embedded"
    return embedded


def _refresh_remote(force=False):
    """Synchronous remote pull (LiteLLM mirrors -> OpenRouter).

    Runs OUTSIDE the module lock so the network can never stall other
    threads. Returns the new PriceTable, or None when every source was
    unreachable (the local table stays in place). Raises only when
    ``force`` is set and nothing was reachable — the panel's refresh
    button wants that surfaced as an error, not silently stale data.
    """
    now = time.time()
    table = None
    # 1) LiteLLM across the mirror chain
    try:
        data, _mirror = _fetch_litellm()
        parsed = _parse_litellm(data)
        if len(parsed) >= 100:
            table = PriceTable(parsed, "litellm", now)
    except Exception:
        table = None

    # 2) OpenRouter as a lighter secondary
    if table is None:
        try:
            parsed = _parse_openrouter(_fetch_json(OPENROUTER_URL))
            if len(parsed) >= 50:
                table = PriceTable(parsed, "openrouter", now)
        except Exception:
            table = None

    with _lock:
        _state["last_attempt"] = time.time()
        path = None
        if table is not None:
            _state["prices"] = table
            _state["source"] = table.source
            _state["fetched_at"] = now
            path = _state["cache_path"]
    if table is not None and path:
        _save_cache(path, table)
    if table is None and force:
        raise IOError("价目表远程源全部不可达（国内网络可设置 "
                      "WB_UPDATE_PROXY=http://代理地址 后重试）")
    return table


def _refresh_worker():
    try:
        _refresh_remote()
    except Exception:
        pass
    finally:
        with _lock:
            _state["refreshing"] = False


def _refresh_async():
    """Kick a background refresh when the table is stale and none runs.

    Backs off for RETRY_INTERVAL after any attempt so an offline host does
    not spawn a thread on every request.
    """
    with _lock:
        now = time.time()
        if _state["refreshing"]:
            return
        if _state["last_attempt"] and now - _state["last_attempt"] < RETRY_INTERVAL:
            return
        if _state["prices"] is not None and _state["fetched_at"] > 0 and \
                now - _state["fetched_at"] <= CACHE_TTL:
            return
        _state["refreshing"] = True
        _state["last_attempt"] = now
    threading.Thread(target=_refresh_worker,
                     name="wb-pricing-refresh", daemon=True).start()


def get_table(cache_path=None, force_refresh=False):
    """Public accessor. Resolves instantly from memory / cache file /
    embedded snapshot and NEVER blocks the calling thread on the network.

    ``cache_path`` persists remote pulls. A stale or missing table triggers
    a single background refresh (daemon thread; at most one in flight,
    retried at most once per RETRY_INTERVAL while unreachable).
    ``force_refresh`` — the panel's refresh button — pulls synchronously
    but still outside the module lock, and raises when every remote source
    is unreachable.
    """
    with _lock:
        if cache_path and _state["cache_path"] != cache_path:
            _state["cache_path"] = cache_path
            _state["prices"] = None        # rebind: re-read the file
            _state["fetched_at"] = 0.0
            _state["last_attempt"] = 0.0
        table = _resolve_local_locked()
    if force_refresh:
        return _refresh_remote(force=True)
    _refresh_async()
    return table


def _reset():
    """Drop all cached state (tests only)."""
    with _lock:
        _state["prices"] = None
        _state["source"] = None
        _state["fetched_at"] = 0.0
        _state["cache_path"] = None
        _state["last_attempt"] = 0.0
        _state["refreshing"] = False


# ---------------------------------------------------------------- costing
def cost_of(model, prompt_tokens=0, completion_tokens=0, cached_tokens=0,
            table=None, cache_path=None):
    """USD cost of one request. Returns (cost, entry).

    ``entry`` is the resolved price dict, or ``None`` when the model was
    unknown and FALLBACK_PRICE was used — callers count those as estimated.
    reasoning_tokens are part of completion_tokens (OpenAI semantics) and
    are deliberately not billed again. cached_tokens bill at cache_read.
    """
    tbl = table or get_table(cache_path=cache_path)
    entry = tbl.get(model)
    resolved = entry is not None
    if not resolved:
        entry = FALLBACK_PRICE
    inp = max(0, int(prompt_tokens or 0))
    out = max(0, int(completion_tokens or 0))
    cached = max(0, int(cached_tokens or 0))
    # cached portion bills at cache_read, the remainder at full input price
    uncached_prompt = max(0, inp - cached) if entry.get("cache_read") is not None else inp
    cost = (uncached_prompt * (entry["input"] or 0.0)
            + out * (entry["output"] or 0.0)
            + cached * (entry["cache_read"] or 0.0))
    return cost, (entry if resolved else None)


def price_meta(table=None, cache_path=None):
    """Where the current prices came from (for the /usage/cost payload)."""
    tbl = table or get_table(cache_path=cache_path)
    return {
        "source": tbl.source,
        "models_covered": len(tbl.prices),
        "fetched_at": tbl.fetched_at,
        "fallback_input_per_mtok": FALLBACK_PRICE["input"] * 1e6,
        "fallback_output_per_mtok": FALLBACK_PRICE["output"] * 1e6,
    }
