# -*- coding: utf-8 -*-
"""_test_custom_providers.py —— 自定义模型提供商 + 自动探寻模型测试。

覆盖：
* wb_custom_providers：URL 候选构造（版本段/兼容后缀/override）、
  协议认证头、响应解析（data[] 与智谱 models[].slug）、库 CRUD、
  探寻打桩（404 换候选、非 404 直接报错）；
* wb_proxy HTTP 端点：/providers/custom 系列的门禁与形状。

纯标准库直跑：python _test_custom_providers.py
"""
import json
import os
import sys
import tempfile
import time

import wb_custom_providers as wcp

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


# ------------------------------------------------------------ URL 候选
def test_candidates():
    print("[url candidates]")
    b = wcp.build_models_url_candidates
    check("plain root", b("https://api.siliconflow.cn") ==
          ["https://api.siliconflow.cn/v1/models"])
    check("trailing slash", b("https://api.example.com/") ==
          ["https://api.example.com/v1/models"])
    check("with v1", b("https://api.example.com/v1") ==
          ["https://api.example.com/v1/models"])
    check("zhipu paas v4", b("https://open.bigmodel.cn/api/coding/paas/v4") == [
        "https://open.bigmodel.cn/api/coding/paas/v4/models",
        "https://open.bigmodel.cn/api/coding/paas/v4/v1/models"])
    check("deepseek anthropic", b("https://api.deepseek.com/anthropic") == [
        "https://api.deepseek.com/anthropic/v1/models",
        "https://api.deepseek.com/v1/models",
        "https://api.deepseek.com/models"])
    check("bailian apps/anthropic",
          b("https://dashscope.aliyuncs.com/apps/anthropic") == [
              "https://dashscope.aliyuncs.com/apps/anthropic/v1/models",
              "https://dashscope.aliyuncs.com/v1/models",
              "https://dashscope.aliyuncs.com/models"])
    check("override wins", b("https://a.com/anthropic",
                             "https://a.com/models") == ["https://a.com/models"])
    check("override blank falls through",
          b("https://a.com", "  ") == ["https://a.com/v1/models"])
    try:
        b("")
        check("empty raises", False)
    except wcp.ProviderError:
        check("empty raises", True)


def test_version_segment():
    print("[version segment]")
    v = wcp.ends_with_version_segment
    check("v1", v("https://x.com/v1"))
    check("v4 paas", v("https://open.bigmodel.cn/api/coding/paas/v4"))
    check("v10", v("https://x.com/v10"))
    check("api no", not v("https://x.com/api"))
    check("vX no", not v("https://x.com/vX"))
    check("models no", not v("https://x.com/models"))


# ------------------------------------------------------------ 认证头
def test_auth_headers():
    print("[auth headers]")
    h = wcp.auth_headers
    check("openai bearer", h("openai", "sk-1")["Authorization"] == "Bearer sk-1")
    check("anthropic x-api-key", h("anthropic", "k2")["x-api-key"] == "k2")
    check("anthropic no bearer",
          "Authorization" not in h("anthropic", "k2"))
    check("gemini goog", h("gemini", "k3")["x-goog-api-key"] == "k3")
    check("empty key ok", "Authorization" not in h("openai", "  "))
    check("unknown protocol -> bearer",
          h("wat", "k")["Authorization"] == "Bearer k")


# ------------------------------------------------------------ 响应解析
def test_extract():
    print("[extract models]")
    e = wcp._extract_models
    check("openai data", e({"data": [{"id": "m1"}, {"id": "m2"}]}) == ["m1", "m2"])
    check("zhipu slug", e({"models": [{"slug": "glm-5"}]}) == ["glm-5"])
    check("data empty falls back to models",
          e({"data": [], "models": [{"slug": "z1"}]}) == ["z1"])
    check("bare list", e(["m1", {"id": "m2"}]) == ["m1", "m2"])
    check("dedupe keep order", e({"data": [{"id": "a"}, {"id": "a"}, {"id": "b"}]}) ==
          ["a", "b"])
    check("skip empty", e({"data": [{"id": ""}, {}]}) == [])
    check("junk none", e("nope") == [])


# ------------------------------------------------------------ SSRF 校验
def test_url_guard():
    print("[url guard]")
    c = wcp._check_url
    check("https ok", c("https://api.example.com/v1/models") is not None)
    check("http ok", c("http://api.example.com/v1/models") is not None)
    for bad in ("ftp://x.com", "file:///c:/win", "https://127.0.0.1/v1",
                "http://localhost/v1", "http://192.168.1.5/v1",
                "http://10.0.0.1/v1", "http://172.16.0.1/v1",
                "http://169.254.169.254/latest/meta-data"):
        try:
            c(bad)
            check("reject %s" % bad, False)
        except wcp.ProviderError:
            check("reject %s" % bad.split("//")[1].split("/")[0], True)
    try:
        c("https://256.999.1.2/v1")
        check("reject malformed ip", False)
    except wcp.ProviderError:
        check("reject malformed ip", True)


# ------------------------------------------------------------ 库 CRUD
def test_library():
    print("[library]")
    path = os.path.join(tempfile.mkdtemp(prefix="wbcptest_"), "custom-providers.json")
    empty = wcp.load_library(path)
    check("missing file -> blank", empty == {"version": 1, "providers": []})

    p, created = wcp.upsert_provider({
        "id": "sf", "name": "硅基流动", "protocol": "openai",
        "base_url": "https://api.siliconflow.cn/", "api_key": "sk-abc123456789",
        "models": [], "enabled": True,
    }, path=path)
    check("created new", created)
    check("base_url rstrip", p["base_url"] == "https://api.siliconflow.cn")
    check("protocol default clean", p["protocol"] == "openai")

    # 脱敏
    listed = wcp.list_providers(path=path)
    check("key masked", listed[0]["api_key"] == "sk-a***6789")
    full = wcp.list_providers(path=path, include_key=True)
    check("key full on demand", full[0]["api_key"] == "sk-abc123456789")

    # 更新：不带 key 时保留原 key；带星号掩码也保留
    p2, created2 = wcp.upsert_provider({
        "id": "sf", "name": "硅基流动2", "api_key": "***6789",
        "base_url": "https://api.siliconflow.cn", "models": ["m-x"],
    }, path=path)
    check("update not create", not created2)
    check("key kept on masked update",
          p2["api_key"] == "sk-abc123456789", repr(p2["api_key"]))
    check("name updated", p2["name"] == "硅基流动2")
    check("models updated", p2["models"] == ["m-x"])
    check("protocol kept openai", p2["protocol"] == "openai")

    # 模型列表为空时保留旧 models
    p3, _ = wcp.upsert_provider({"id": "sf", "name": "s3",
                                 "base_url": "https://api.siliconflow.cn",
                                 "models": []}, path=path)
    check("models kept when empty", p3["models"] == ["m-x"])

    # 探寻结果写回
    orig = wcp.probe_models
    wcp.probe_models = lambda base_url, api_key, protocol="openai", override=None: (
        (["glm-4.6", "deepseek-v3"], [(base_url + "/v1/models", True, "2 个模型")]))
    try:
        stored = wcp.probe_and_store("sf", path=path)
        check("probe stored models", stored["models"] == ["glm-4.6", "deepseek-v3"])
        check("probe stored meta", stored["last_probe"]["count"] == 2)
    finally:
        wcp.probe_models = orig

    # 坏协议归一为 openai
    wcp.upsert_provider({"id": "bad", "protocol": "wat",
                         "base_url": "https://x.com"}, path=path)
    check("bad protocol normalized",
          wcp.get_provider("bad", path=path)["protocol"] == "openai")

    check("delete", wcp.delete_provider("bad", path=path))
    try:
        wcp.delete_provider("bad", path=path)
        check("delete missing raises", False)
    except wcp.ProviderError:
        check("delete missing raises", True)

    check("set_enabled off", wcp.set_enabled("sf", False, path=path))
    check("enabled flag", wcp.get_provider("sf", path=path)["enabled"] is False)

    # 损坏文件容错
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{broken")
    check("corrupt -> blank", wcp.load_library(path) == {"version": 1, "providers": []})


# ------------------------------------------------------------ 探寻流程
def test_probe_flow():
    print("[probe flow]")
    # 404 -> 换下一个候选（deepseek anthropic 形态最典型）
    calls = []

    def fake_get(url, headers=None, timeout=12.0):
        calls.append(url)
        if url.endswith("/anthropic/v1/models"):
            raise wcp.ProviderError("HTTP 404: nope")
        return {"data": [{"id": "ds-model"}]}

    orig = wcp._http_get_json
    wcp._http_get_json = fake_get
    try:
        models, tried = wcp.probe_models("https://api.deepseek.com/anthropic", "sk-k")
    finally:
        wcp._http_get_json = orig
    check("404 falls through", models == ["ds-model"])
    check("tried records both", len(tried) == 2 and tried[0][1] is False
          and tried[1][1] is True)
    check("candidate order", calls == [
        "https://api.deepseek.com/anthropic/v1/models",
        "https://api.deepseek.com/v1/models"])

    # 401 直接报错不换候选
    def fake_401(url, headers=None, timeout=12.0):
        raise wcp.ProviderError("HTTP 401: bad key")

    wcp._http_get_json = fake_401
    try:
        wcp.probe_models("https://api.example.com", "sk-k")
        check("401 raises", False)
    except wcp.ProviderError as exc:
        check("401 raises", "401" in str(exc))
    finally:
        wcp._http_get_json = orig

    # 全部候选 404 -> 汇总报错
    def fake_404(url, headers=None, timeout=12.0):
        raise wcp.ProviderError("HTTP 404: no")

    wcp._http_get_json = fake_404
    try:
        wcp.probe_models("https://api.example.com", "sk-k")
        check("all 404 raises", False)
    except wcp.ProviderError as exc:
        check("all 404 raises", "候选" in str(exc) or "404" in str(exc))
    finally:
        wcp._http_get_json = orig

    # anthropic 协议的认证头确实带 x-api-key
    seen = {}

    def fake_capture(url, headers=None, timeout=12.0):
        seen.update(headers or {})
        return {"data": [{"id": "m"}]}

    wcp._http_get_json = fake_capture
    try:
        wcp.probe_models("https://api.anthropic.com", "sk-ant", protocol="anthropic")
    finally:
        wcp._http_get_json = orig
    check("anthropic header sent", seen.get("x-api-key") == "sk-ant")
    check("anthropic version sent", seen.get("anthropic-version") == "2023-06-01")


# ------------------------------------------------------------ HTTP 端点
def test_http():
    print("[http endpoints]")
    import wb_gateway
    import wb_proxy
    import wb_runtime

    tmp = tempfile.mkdtemp(prefix="wbcptest_http_")
    old_dir = wb_proxy.ACCOUNTS_DIR
    # 库文件落在沙箱数据目录
    os.environ["WB_DATA_DIR"] = tmp
    lib_path = os.path.join(tmp, wcp.LIBRARY_NAME)

    # 端点探寻打桩：不联网
    orig_get = wcp._http_get_json
    wcp._http_get_json = lambda url, headers=None, timeout=12.0: {
        "data": [{"id": "glm-4.6"}, {"id": "deepseek-v3"}]}

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
        # 门禁：无面板 token -> 401
        status, _ = client.request("GET", "/providers/custom")
        check("GET no token 401", status == 401, str(status))
        status, _ = client.request("POST", "/providers/custom/upsert",
                                   body={"id": "x"})
        check("POST no token 401", status == 401, str(status))

        # 新增
        status, body = client.request("POST", "/providers/custom/upsert", headers=hdr,
                                      body={"id": "sf", "name": "硅基流动",
                                            "protocol": "openai",
                                            "base_url": "https://api.siliconflow.cn",
                                            "api_key": "sk-test12345678"})
        check("upsert 200", status == 200, body[:120])
        data = json.loads(body)
        check("upsert ok flag", data.get("ok") is True)
        check("upsert masked key",
              data.get("provider", {}).get("api_key") == "sk-t***5678",
              str(data.get("provider", {}).get("api_key")))

        # 列表
        status, body = client.request("GET", "/providers/custom", headers=hdr)
        check("list 200", status == 200)
        data = json.loads(body)
        check("list has provider", any(p["id"] == "sf"
                                       for p in data.get("providers", [])))
        check("list masked", data["providers"][0]["api_key"].endswith("***")
              or "*" in data["providers"][0]["api_key"])

        # 探寻
        status, body = client.request("POST", "/providers/custom/probe", headers=hdr,
                                      body={"id": "sf"})
        check("probe 200", status == 200, body[:200])
        data = json.loads(body)
        check("probe models", data.get("models") == ["glm-4.6", "deepseek-v3"],
              str(data))
        check("probe tried", isinstance(data.get("tried"), list))

        # 启停
        status, body = client.request("POST", "/providers/custom/set", headers=hdr,
                                      body={"id": "sf", "enabled": False})
        check("set 200", status == 200)
        check("set persisted",
              wcp.get_provider("sf", path=lib_path)["enabled"] is False)

        # 删除
        status, body = client.request("POST", "/providers/custom/delete", headers=hdr,
                                      body={"id": "sf"})
        check("delete 200", status == 200)
        try:
            wcp.get_provider("sf", path=lib_path)
            check("deleted gone", False)
        except wcp.ProviderError:
            check("deleted gone", True)

        # 探寻不存在的 id -> 400
        status, body = client.request("POST", "/providers/custom/probe", headers=hdr,
                                      body={"id": "nope"})
        check("probe missing 400", status == 400, str(status))

        # 删除不存在的 -> 400
        status, _ = client.request("POST", "/providers/custom/delete", headers=hdr,
                                   body={"id": "nope"})
        check("delete missing 400", status == 400)
    finally:
        gw.stop()
        wcp._http_get_json = orig_get
        wb_proxy.PANEL.revoke(token)
        wb_proxy.ACCOUNTS_DIR = old_dir


# ------------------------------------------------------------ 测试基建（照抄 _test_pricing.py）
def _free_port():
    s = __import__("socket").socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Client:
    """Raw-socket HTTP client against the test gateway."""

    def __init__(self, port):
        self.port = port

    def request(self, method, path, headers=None, body=None):
        import socket as _s
        payload = json.dumps(body).encode() if body is not None else None
        req = "%s %s HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n" % (method, path)
        if headers:
            for k, v in headers.items():
                req += "%s: %s\r\n" % (k, v)
        if payload is not None:
            req += "Content-Type: application/json\r\nContent-Length: %d\r\n" % len(payload)
        req += "\r\n"
        conn = _s.create_connection(("127.0.0.1", self.port), timeout=10)
        conn.sendall(req.encode() + (payload or b""))
        chunks = []
        while True:
            try:
                chunk = conn.recv(65536)
            except _s.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
        conn.close()
        raw = b"".join(chunks)
        head, _, body_raw = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ")[1])
        return status, body_raw.decode("utf-8", "replace")


def main():
    test_candidates()
    test_version_segment()
    test_auth_headers()
    test_extract()
    test_url_guard()
    test_library()
    test_probe_flow()
    test_http()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
