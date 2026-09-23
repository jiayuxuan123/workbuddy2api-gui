# -*- coding: utf-8 -*-
"""_test_cat_travel.py —— 猫猫旅行协议的回归测试（纯离线）。

为什么需要这个测试
------------------
线上曾经稳定报 HTTP 400。根因不是网络也不是鉴权，而是 ``depart`` 的请求体：
旧实现固定发送空 ``{}``，而上游要求显式给出 ``location_id`` 与
``duration_hours``，于是每次都回 ``{"code":400,"msg":"invalid request"}``。

更麻烦的是错误被吞了 —— 旧代码只保留异常的字符串形式，看不到上游的响应体，
所以"协议写错"和"网络不通"长得一模一样，这个问题因此反复被误判。
这个测试用假账号和假响应把三件事钉住：

1. ``depart`` 必须带正确的字段（回归到空 body 就会失败）；
2. ``claim`` 用空请求体（它与 depart 的要求相反）；
3. 上游的错误响应体会被保留到返回值里，不再丢失。

完全离线：``urllib.request.urlopen`` 被替换成假的，不发任何真实请求。
"""

import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import wb_tasks

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print("  %-56s %s%s" % (label, "PASS" if cond else "FAIL",
                            ("  <- " + detail) if (detail and not cond) else ""))


class FakeResponse(object):
    """Minimal stand-in for the object urlopen yields."""

    def __init__(self, status, payload):
        self.status = status
        self._body = payload if isinstance(payload, bytes) else \
            json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeAccount(object):
    """Just enough Account surface for wb_tasks."""

    def __init__(self, realm="cn", uid="uid-1", token="tok"):
        self.realm = realm
        self.uid = uid
        self.access_token = token
        self.credits = {}
        self.refreshed = 0

    def headers(self, purpose="chat"):
        return {"Authorization": "Bearer " + self.access_token,
                "X-User-Id": self.uid,
                "Content-Type": "application/json"}

    def fetch_credits(self):
        self.refreshed += 1
        self.credits = {"remain": 100}


class Recorder(object):
    """Records every request and replies from a scripted table."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, req, timeout=None):
        body = None
        if req.data is not None:
            body = req.data
        self.calls.append({"url": req.full_url, "method": req.get_method(),
                           "body": body,
                           "headers": dict(req.headers)})
        route = None
        for pattern, response in self.routes:
            if pattern in req.full_url:
                route = response
                break
        if route is None:
            return FakeResponse(404, {"code": 404, "msg": "not found"})
        if isinstance(route, Exception):
            raise route
        return FakeResponse(200, route)


def install(recorder):
    """Swap urlopen for the recorder; return the original for restoration."""
    original = wb_tasks.urllib.request.urlopen
    wb_tasks.urllib.request.urlopen = recorder
    return original


STATUS_IDLE = {"code": 0, "msg": "OK", "data": {
    "state": "idle", "buddy_id": 0, "record_id": 0,
    "daily_limit_reached": False}}
STATUS_TRAVELING = {"code": 0, "msg": "OK", "data": {
    "state": "traveling", "buddy_id": 7560794, "record_id": 8575781,
    "daily_limit_reached": True}}
STATUS_ARRIVED = {"code": 0, "msg": "OK", "data": {
    "state": "arrived", "buddy_id": 7560794, "record_id": 8575781,
    "reward_credit": 10}}
CONFIG = {"code": 0, "msg": "OK", "data": {"locations": [
    {"id": 1, "code": "coffee", "name": "咖啡馆",
     "duration_hours_min": 1, "duration_hours_max": 4},
    {"id": 2, "code": "mall", "name": "商场店铺",
     "duration_hours_min": 1, "duration_hours_max": 4},
]}}
DEPART_OK = {"code": 0, "msg": "OK", "data": {
    "state": "traveling", "record_id": 8575781, "arrive_at": 1790146972}}
CLAIM_OK = {"code": 0, "msg": "OK", "data": {"reward_credit": 10}}

ROUTES = [
    ("/travel/status", STATUS_IDLE),
    ("/travel/config", CONFIG),
    ("/travel/depart", DEPART_OK),
    ("/travel/claim", CLAIM_OK),
]


def find_call(recorder, needle):
    for call in recorder.calls:
        if needle in call["url"]:
            return call
    return None


def body_of(call):
    if call is None or call["body"] is None:
        return None
    return json.loads(call["body"].decode("utf-8"))


def main():
    account = FakeAccount()

    # ---------- depart 必须带目的地与时长 ----------
    print("=== depart 载荷（回归点） ===")
    rec = Recorder(ROUTES)
    original = install(rec)
    try:
        result = wb_tasks.do_cat_travel(account)
    finally:
        wb_tasks.urllib.request.urlopen = original

    check("派出返回成功", result.get("ok") is True, repr(result))
    check("动作是 depart", result.get("action") == "depart", repr(result))

    call = find_call(rec, "/travel/depart")
    check("确实调用了 depart", call is not None,
          repr([c["url"] for c in rec.calls]))
    check("depart 用 POST", call and call["method"] == "POST",
          repr(call and call["method"]))

    payload = body_of(call)
    check("depart 请求体非空（旧实现发 {} 会被拒）",
          payload not in (None, {}), repr(payload))
    check("depart 带 location_id",
          isinstance(payload, dict) and "location_id" in payload,
          repr(payload))
    check("depart 带 duration_hours",
          isinstance(payload, dict) and "duration_hours" in payload,
          repr(payload))
    check("location_id 是正整数",
          isinstance(payload, dict)
          and isinstance(payload.get("location_id"), int)
          and payload["location_id"] > 0, repr(payload))
    check("duration_hours 在配置区间内",
          isinstance(payload, dict)
          and 1 <= payload.get("duration_hours", 0) <= 4, repr(payload))

    check("先读了 config 拿目的地",
          find_call(rec, "/travel/config") is not None,
          repr([c["url"] for c in rec.calls]))

    # ---------- claim 用空请求体 ----------
    print("\n=== claim 载荷（与 depart 相反） ===")
    rec2 = Recorder([("/travel/status", STATUS_ARRIVED),
                     ("/travel/claim", CLAIM_OK)])
    original = install(rec2)
    try:
        result2 = wb_tasks.do_cat_travel(account)
    finally:
        wb_tasks.urllib.request.urlopen = original

    check("领奖返回成功", result2.get("ok") is True, repr(result2))
    check("动作是 claim", result2.get("action") == "claim", repr(result2))
    check("领奖带回积分", result2.get("credit") == 10, repr(result2))
    claim_call = find_call(rec2, "/travel/claim")
    check("claim 用 POST（GET 会 404）",
          claim_call and claim_call["method"] == "POST",
          repr(claim_call and claim_call["method"]))
    check("claim 请求体是空对象",
          body_of(claim_call) == {}, repr(body_of(claim_call)))
    check("领奖后刷新了余额", account.refreshed >= 1)

    # ---------- traveling / 已达上限 ----------
    print("\n=== 状态分支 ===")
    rec3 = Recorder([("/travel/status", STATUS_TRAVELING)])
    original = install(rec3)
    try:
        result3 = wb_tasks.do_cat_travel(account)
    finally:
        wb_tasks.urllib.request.urlopen = original
    check("旅行中不发 depart",
          find_call(rec3, "/travel/depart") is None,
          repr([c["url"] for c in rec3.calls]))
    check("旅行中返回 traveling",
          result3.get("action") == "traveling", repr(result3))

    limit_status = json.loads(json.dumps(STATUS_IDLE))
    limit_status["data"]["daily_limit_reached"] = True
    rec4 = Recorder([("/travel/status", limit_status)])
    original = install(rec4)
    try:
        result4 = wb_tasks.do_cat_travel(account)
    finally:
        wb_tasks.urllib.request.urlopen = original
    check("已达上限不发 depart",
          find_call(rec4, "/travel/depart") is None,
          repr([c["url"] for c in rec4.calls]))
    check("已达上限提示明日刷新",
          "明日" in (result4.get("msg") or ""), repr(result4))

    # ---------- 上游错误体必须保留 ----------
    print("\n=== 错误信息不再被吞 ===")

    class HttpError400(wb_tasks.urllib.error.HTTPError):
        pass

    def fake_error(code, body_text):
        import io
        return wb_tasks.urllib.error.HTTPError(
            "https://copilot.tencent.com/x", code, "Bad Request", {},
            io.BytesIO(body_text.encode("utf-8")))

    def raising_recorder(req, timeout=None):
        if "/travel/status" in req.full_url:
            return FakeResponse(200, STATUS_IDLE)
        if "/travel/config" in req.full_url:
            return FakeResponse(200, CONFIG)
        raise fake_error(400, '{"code":400,"msg":"invalid request"}')

    original = wb_tasks.urllib.request.urlopen
    wb_tasks.urllib.request.urlopen = raising_recorder
    try:
        result5 = wb_tasks.do_cat_travel(account)
    finally:
        wb_tasks.urllib.request.urlopen = original

    msg = result5.get("msg") or ""
    check("失败被报告", result5.get("ok") is False, repr(result5))
    check("错误信息含 HTTP 状态码", "400" in msg, repr(msg))
    check("错误信息含上游 msg 原文", "invalid request" in msg, repr(msg))

    # ---------- 目的地解析的健壮性 ----------
    print("\n=== 目的地解析 ===")
    check("pick_travel_location 空列表返回 None",
          wb_tasks.pick_travel_location([]) is None)
    picked = wb_tasks.pick_travel_location(
        [{"id": 7, "code": "x", "name": "X", "min": 2, "max": 3}])
    check("选中项带 id 与时长",
          picked and picked["location_id"] == 7
          and picked["duration_hours"] == 3, repr(picked))
    check("时长取区间上限（每日一次，价值最大）",
          picked and picked["duration_hours"] == 3, repr(picked))

    # 缺字段的条目应被跳过而不是抛错
    rec5 = Recorder([("/travel/status", STATUS_IDLE),
                     ("/travel/config", {"code": 0, "msg": "OK", "data": {
                         "locations": [{"code": "broken"},
                                       {"id": 5, "code": "ok",
                                        "name": "OK", "duration_hours_min": 2,
                                        "duration_hours_max": 2}]}}),
                     ("/travel/depart", DEPART_OK)])
    original = install(rec5)
    try:
        wb_tasks.do_cat_travel(account)
    finally:
        wb_tasks.urllib.request.urlopen = original
    payload5 = body_of(find_call(rec5, "/travel/depart"))
    check("跳过非法条目，选中合法的那个",
          payload5 and payload5.get("location_id") == 5, repr(payload5))

    print("\n" + "=" * 70)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for name in FAIL:
            print("  FAIL: %s" % name)
        return 1
    print("RESULT: ALL CAT TRAVEL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
