# -*- coding: utf-8 -*-
"""_test_pricing.py —— 用量成本统计测试。

覆盖：
* wb_pricing：解析器、归一化、别名映射、兜底价、缓存落盘/回退；
* wb_usagelog：按日聚合（含错误行、跨天、realm 过滤、成本与 estimated 标记）；
* wb_proxy：GET /usage/cost 端点（面板门禁、字段形状）、
  POST /usage/cost/refresh 门禁。

纯标准库直跑：python _test_pricing.py
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time

import wb_pricing
import wb_usagelog

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s %s" % (name, extra))


# ---------------------------------------------------------------- pricing
def test_normalize():
    print("[normalize]")
    n = wb_pricing._normalize
    check("bare name", n("gpt-5.5") == "gpt-5.5")
    check("case", n("GPT-5.5") == "gpt-5.5")
    check("vendor prefix", n("novita/zai-org/glm-4.6") == "glm-4.6")
    check("bedrock path", n("bedrock/ap-northeast-1/moonshotai.kimi-k2-thinking") ==
          "moonshotai.kimi-k2-thinking")
    check("openrouter free", n("openrouter/deepseek/deepseek-v4.1-flash:free") ==
          "deepseek-v4.1-flash")
    check("azure prefix", n("azure/eu/gpt-5.6-sol") == "gpt-5.6-sol")
    check("colon revision", n("model:0") == "model")


def test_parse_litellm():
    print("[parse_litellm]")
    data = {
        "gpt-5.5": {"input_cost_per_token": 5e-06, "output_cost_per_token": 3e-05,
                    "cache_read_input_token_cost": 5e-07},
        "azure/eu/gpt-5.6-sol": {"input_cost_per_token": 4.4e-06,
                                 "output_cost_per_token": 2.2e-05},
        "no-price": {"max_tokens": 8192},
        "sample_spec": {"input_cost_per_token": -1, "output_cost_per_token": 1},
    }
    out = wb_pricing._parse_litellm(data)
    # bare name must win over the routed variant for gpt-5.5
    check("bare wins", out.get("gpt-5.5", {}).get("input") == 5e-06,
          str(out.get("gpt-5.5")))
    check("routed normalized", out.get("gpt-5.6-sol", {}).get("input") == 4.4e-06)
    check("unpriced skipped", "no-price" not in out)
    check("negative skipped", "sample_spec" not in out)
    check("cache_read kept", out["gpt-5.5"]["cache_read"] == 5e-07)


def test_parse_openrouter():
    print("[parse_openrouter]")
    data = {"data": [
        {"id": "vendor/model-a", "pricing": {"prompt": "0.0000001",
                                             "completion": "0.0000004"}},
        {"id": "vendor/model-b", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "vendor/model-c"},  # no pricing
    ]}
    out = wb_pricing._parse_openrouter(data)
    check("model-a", out.get("model-a", {}).get("input") == 1e-07)
    check("zero skipped", "model-b" not in out)
    check("missing skipped", "model-c" not in out)


def test_price_table_lookup():
    print("[price_table]")
    table = wb_pricing.PriceTable({
        "gpt-5.5": {"input": 5e-06, "output": 3e-05, "cache_read": 5e-07},
        "glm-5.3": {"input": 1e-06, "output": 4e-06, "cache_read": 2e-07},
    }, "test", 0.0)
    check("exact", table.get("gpt-5.5") is not None)
    check("case-insensitive", table.get("GPT-5.5") is not None)
    check("alias default-model", table.get("default-model") is not None)
    check("alias fast-model", table.get("fast-model") is not None)
    check("unknown none", table.get("no-such-model") is None)
    check("alias explicit none", table.get("hunyuan-chat") is None)


def test_cost_of():
    print("[cost_of]")
    table = wb_pricing.PriceTable({
        "m": {"input": 2e-06, "output": 6e-06, "cache_read": 5e-07},
    }, "test", 0.0)
    # 1000 prompt (300 cached), 500 completion
    cost, entry = wb_pricing.cost_of("m", 1000, 500, 300, table=table)
    expect = 700 * 2e-06 + 500 * 6e-06 + 300 * 5e-07
    check("split billing", abs(cost - expect) < 1e-12, "%r vs %r" % (cost, expect))
    # reasoning not double-billed: callers pass completion incl. reasoning
    # unknown model -> fallback + entry None
    cost2, entry2 = wb_pricing.cost_of("???", 1000, 0, 0, table=table)
    check("fallback entry None", entry2 is None)
    check("fallback positive", cost2 > 0)
    # negative guards
    cost3, _ = wb_pricing.cost_of("m", -5, -5, -5, table=table)
    check("negatives clamp", cost3 == 0.0)


def test_embedded_snapshot():
    print("[embedded snapshot]")
    table = wb_pricing.PriceTable(
        wb_pricing._parse_embedded(wb_pricing.EMBEDDED_PRICES), "embedded", 0.0)
    check("embedded count", len(table.prices) >= 25, str(len(table.prices)))
    check("embedded gpt", table.get("gpt-5.5") is not None)
    check("embedded hy4", table.get("hy4-preview") is not None)


def test_cache_roundtrip(tmp):
    print("[cache roundtrip]")
    path = os.path.join(tmp, "prices-cache.json")
    table = wb_pricing.PriceTable(
        wb_pricing._parse_embedded(wb_pricing.EMBEDDED_PRICES), "litellm",
        12345.0)
    wb_pricing._save_cache(path, table)
    loaded = wb_pricing._load_cache(path)
    check("loaded", loaded is not None)
    check("source kept", loaded.source == "litellm")
    check("fetched_at kept", loaded.fetched_at == 12345.0)
    check("prices kept", loaded.get("gpt-5.5") is not None)
    # corrupt file -> None
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{broken")
    check("corrupt -> None", wb_pricing._load_cache(path) is None)


# ---------------------------------------------------------------- usagelog
def test_by_day(tmp):
    print("[usagelog by_day]")
    log_path = os.path.join(tmp, "usage.jsonl")
    rows = [
        # day 1: two ok requests on one model
        {"at": 1789720000, "iso": "2026-07-19T10:00:00", "model": "glm-5.3",
         "prompt_tokens": 1000, "completion_tokens": 500, "reasoning_tokens": 0,
         "cached_tokens": 200, "total_tokens": 1500, "realm": "intl",
         "account": "a1"},
        {"at": 1789720500, "iso": "2026-07-19T10:08:20", "model": "glm-5.3",
         "prompt_tokens": 100, "completion_tokens": 50, "reasoning_tokens": 0,
         "cached_tokens": 0, "total_tokens": 150, "realm": "intl",
         "account": "a1"},
        # day 2: one ok on unknown model (estimated) + one error
        {"at": 1789800000, "iso": "2026-07-20T10:00:00", "model": "no-such",
         "prompt_tokens": 100, "completion_tokens": 50, "reasoning_tokens": 0,
         "cached_tokens": 0, "total_tokens": 150, "realm": "cn",
         "account": "a2"},
        {"at": 1789800100, "iso": "2026-07-20T10:01:40", "model": "glm-5.3",
         "error": "upstream boom", "realm": "cn", "account": "a2"},
    ]
    with open(log_path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    ul = wb_usagelog.UsageLog(log_path)
    ul.refresh()
    days = ul.by_day()
    check("two days", [d["date"] for d in days] == ["2026-07-19", "2026-07-20"],
          str(days))
    d1, d2 = days
    check("d1 requests", d1["requests"] == 2)
    check("d1 tokens", d1["total_tokens"] == 1650)
    check("d1 cost>0", d1["cost"] > 0)
    check("d1 not estimated", d1["cost_estimated"] == 0)
    check("d2 requests excl error", d2["requests"] == 1)
    check("d2 errors", d2["errors"] == 1)
    check("d2 estimated", d2["cost_estimated"] == 1)
    check("d2 cost from fallback", d2["cost"] > 0)
    # realm filter uses the tail window
    days_cn = ul.by_day(realm="cn")
    check("cn only day2", [d["date"] for d in days_cn] == ["2026-07-20"],
          str(days_cn))
    # incremental append only reads new bytes
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "at": 1789880000, "iso": "2026-07-21T09:00:00", "model": "glm-5.3",
            "prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 0,
            "cached_tokens": 0, "total_tokens": 15, "realm": "intl",
            "account": "a1"}) + "\n")
    ul.refresh()
    days = ul.by_day()
    check("append -> day3", days[-1]["date"] == "2026-07-21")
    check("three days", len(days) == 3)
    # no date info -> row skipped (no crash, no empty bucket)
    check("no empty buckets", all(d["requests"] + d["errors"] > 0 for d in days))


def test_day_of_row():
    print("[day_of_row]")
    check("iso", wb_usagelog.day_of_row({"iso": "2026-07-20T10:00:00"}) == "2026-07-20")
    check("at fallback", wb_usagelog.day_of_row({"at": 0}) ==
          time.strftime("%Y-%m-%d", time.localtime(0)))
    check("neither", wb_usagelog.day_of_row({}) == "")


# ---------------------------------------------------------------- http
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Client(object):
    """Minimal HTTP client speaking to the test gateway."""

    def __init__(self, port):
        self.port = port

    def request(self, method, path, body=None, headers=None):
        import wb_proxy
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        payload = json.dumps(body).encode() if body is not None else None
        req = "%s %s HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n" % (method, path)
        if headers:
            for k, v in headers.items():
                req += "%s: %s\r\n" % (k, v)
        if payload is not None:
            req += "Content-Type: application/json\r\nContent-Length: %d\r\n" % len(payload)
        req += "\r\n"
        conn.sendall(req.encode() + (payload or b""))
        chunks = []
        while True:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
        conn.close()
        raw = b"".join(chunks)
        head, _, body_raw = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ")[1])
        return status, body_raw.decode("utf-8", "replace")


def test_http(tmp):
    print("[http endpoints]")
    import wb_gateway
    import wb_proxy

    # fully offline: no remote fetch may happen inside the test gateway.
    # _fetch_json raising forces get_table() down the cache/embedded path.
    orig_fetch = wb_pricing._fetch_json

    def _no_net(url, timeout=15):
        raise IOError("network disabled in tests")

    wb_pricing._fetch_json = _no_net

    # sandbox the usage dir so the real log is never touched
    usage_dir = os.path.join(tmp, "usage")
    os.makedirs(usage_dir, exist_ok=True)
    old_dir, old_log = wb_proxy.USAGE_DIR, wb_proxy.USAGE_LOG
    wb_proxy.USAGE_DIR, wb_proxy.USAGE_LOG = usage_dir, os.path.join(usage_dir, "usage.jsonl")

    # pre-seed a price cache so cost_of resolves known models deterministically
    wb_pricing._reset()
    seed_table = wb_pricing.PriceTable(
        wb_pricing._parse_embedded(wb_pricing.EMBEDDED_PRICES), "litellm", time.time())
    wb_pricing._save_cache(os.path.join(usage_dir, "prices-cache.json"), seed_table)

    # seed two usage rows through the real writer path
    wb_proxy.record_usage("glm-5.3", {"prompt_tokens": 1000, "completion_tokens": 500,
                                      "cached_tokens": 100, "total_tokens": 1500},
                          stream=False, elapsed_ms=10, account="acct-1")
    wb_proxy.record_usage("mystery-model", {"prompt_tokens": 100, "completion_tokens": 50,
                                            "cached_tokens": 0, "total_tokens": 150},
                          stream=False, elapsed_ms=10, account="acct-1")

    token = wb_proxy.PANEL.create()
    hdr = {"X-Panel-Token": token}
    gw = wb_gateway.Gateway()
    gw.prefs.update({"lan": False, "require_local_key": False,
                     "port": _free_port()})
    ok, msg = gw.start()
    check("gateway started", ok, msg)
    port = gw.prefs["port"]
    time.sleep(0.4)
    client = _Client(port)
    try:
        # no panel token -> 401 (usage routes are panel routes)
        status, body = client.request("GET", "/usage/cost")
        check("GET no token 401", status == 401, str(status))
        status, _ = client.request("POST", "/usage/cost/refresh")
        check("POST refresh no token 401", status == 401, str(status))

        status, body = client.request("GET", "/usage/cost", headers=hdr)
        check("GET 200", status == 200, str(status))
        data = json.loads(body)
        check("days present", isinstance(data.get("days"), list))
        check("totals present", isinstance(data.get("totals"), dict))
        check("pricing meta", data.get("pricing", {}).get("currency") == "USD")
        check("cost positive", data["totals"]["cost"] > 0,
              str(data["totals"]))
        check("estimated counted", data["totals"]["cost_estimated_requests"] >= 1)
        total_tokens = data["totals"]["total_tokens"]
        check("token totals", total_tokens == 1650, str(total_tokens))

        # realm filter accepted
        status, body = client.request("GET", "/usage/cost?realm=cn", headers=hdr)
        check("GET realm 200", status == 200)
        data_cn = json.loads(body)
        check("realm echoed", data_cn.get("realm") == "cn")

        # refresh endpoint: network may be blocked, accept 200 or 502
        status, body = client.request("POST", "/usage/cost/refresh", headers=hdr)
        check("refresh 200/502", status in (200, 502), str(status))
        if status == 200:
            rdata = json.loads(body)
            check("refresh shape", rdata.get("ok") is True and
                  "models_covered" in rdata)
    finally:
        gw.stop()
        wb_pricing._fetch_json = orig_fetch
        wb_pricing._reset()
        wb_proxy.USAGE_DIR, wb_proxy.USAGE_LOG = old_dir, old_log
        wb_usagelog.ensure_path(old_log)
        wb_proxy.PANEL.revoke(token)


def main():
    tmp = tempfile.mkdtemp(prefix="wb_pricing_test_")
    # Sandbox the data dir BEFORE wb_gateway/wb_proxy get imported (they do so
    # lazily inside test_http).  Gateway._prepare() rewrites wb_proxy.USAGE_DIR
    # from wb_runtime.data_dir(), which honours WB_DATA_DIR at call time.
    os.environ["WB_DATA_DIR"] = tmp
    try:
        test_normalize()
        test_parse_litellm()
        test_parse_openrouter()
        test_price_table_lookup()
        test_cost_of()
        test_embedded_snapshot()
        test_cache_roundtrip(tmp)
        test_by_day(tmp)
        test_day_of_row()
        test_http(tmp)
    finally:
        print("\n%d passed, %d failed" % (PASS, FAIL))
        if FAIL:
            sys.exit(1)


if __name__ == "__main__":
    main()
