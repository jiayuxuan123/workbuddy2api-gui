# -*- coding: utf-8 -*-
"""wb_pricing.py —— 模型价目表：远程拉取 + 本地缓存 + 内嵌快照兜底。

成本统计需要 USD/百万 token 价目。本项目自拟价目不可靠，因此改为自动
从网上拉取：

* 主源：LiteLLM 官方价目 JSON（model_prices_and_context_window.json，
  4000+ 模型、社区维护、更新勤）；
* 副源：OpenRouter /api/v1/models（pricing.prompt / pricing.completion，
  单位是 USD/token 字符串）；
* 兜底：随包内嵌一份生成时抓取的快照（EMBEDDED_PRICES），断网也能算；
* 最后防线：未知模型按兜底价计（FALLBACK_PRICE），并在响应里标记
  ``estimated=true``。

拉取结果写到数据目录 ``usage/prices-cache.json``，带 TTL（默认 24h）；
每次刷新只做一次网络请求、失败静默回退缓存/快照，绝不影响请求路径。

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

LITELLM_URL = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
               "model_prices_and_context_window.json")
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"

_lock = threading.Lock()
_state = {
    "prices": None,       # model_key(小写) -> {"input","output","cache_read"}
    "source": None,       # "embedded" | "cache" | "litellm" | "openrouter"
    "fetched_at": 0.0,    # epoch of last successful remote pull
    "cache_path": None,
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
def _fetch_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "WorkBuddy2API/1.7"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


def _refresh_locked(force=False):
    """Build the live PriceTable: remote -> cache -> embedded."""
    path = _state["cache_path"]
    now = time.time()

    def too_stale():
        return force or (_state["prices"] is None) or \
            (now - _state["fetched_at"] > CACHE_TTL)

    if not too_stale():
        return _state["prices"]

    # 1) remote LiteLLM
    try:
        table = PriceTable(_parse_litellm(_fetch_json(LITELLM_URL)),
                           "litellm", now)
        if len(table.prices) >= 100:
            _state["prices"] = table
            _state["source"] = table.source
            _state["fetched_at"] = now
            if path:
                _save_cache(path, table)
            return table
    except Exception:
        pass

    # 2) OpenRouter as a lighter secondary
    try:
        table = PriceTable(_parse_openrouter(_fetch_json(OPENROUTER_URL)),
                           "openrouter", now)
        if len(table.prices) >= 50:
            _state["prices"] = table
            _state["source"] = table.source
            _state["fetched_at"] = now
            if path:
                _save_cache(path, table)
            return table
    except Exception:
        pass

    # 3) cache (even if expired — stale beats embedded)
    if path:
        cached = _load_cache(path)
        if cached:
            _state["prices"] = cached
            _state["source"] = cached.source
            _state["fetched_at"] = cached.fetched_at
            return cached

    # 4) embedded snapshot (fetched_at=now so offline hosts don't re-hit the
    #    network on every call; remote retry happens after CACHE_TTL)
    embedded = PriceTable(_parse_embedded(EMBEDDED_PRICES), "embedded", 0.0)
    _state["prices"] = embedded
    _state["source"] = "embedded"
    _state["fetched_at"] = now
    return embedded


def get_table(cache_path=None, force_refresh=False):
    """Public accessor. ``cache_path`` persists remote pulls.

    Thread-safe; remote fetch happens at most once per CACHE_TTL unless
    ``force_refresh`` (used by the panel's refresh button).
    """
    with _lock:
        if cache_path and _state["cache_path"] != cache_path:
            _state["cache_path"] = cache_path
            _state["fetched_at"] = 0.0     # new file: try (re)binding
        return _refresh_locked(force=force_refresh)


def _reset():
    """Drop all cached state (tests only)."""
    with _lock:
        _state["prices"] = None
        _state["source"] = None
        _state["fetched_at"] = 0.0
        _state["cache_path"] = None


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
