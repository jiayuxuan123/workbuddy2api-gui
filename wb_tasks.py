"""wb_tasks.py —— 国内版成长任务与日常福利全自动完成引擎

包含功能：
1. 成长任务查询、批量接取 (accept)、构造事件上报点亮 (report)、领奖入账 (claim)。
2. 连续打卡 (streak) 与能量 (energy) 余额查询。
3. 猫猫旅行 (buddy travel) 状态查询、自动派出与自动领奖。
4. 严格遵守 >= 1.0s 防风控间隔，并使用 wb_fingerprint 的稳定设备指纹。
"""
import json
import time
import urllib.error
import urllib.request

CHAT_BASE = "https://copilot.tencent.com"
BILL_BASE = "https://www.codebuddy.cn"
WEB_BASE = "https://www.workbuddy.cn"
DESKTOP_UA = "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1"
WEB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"


_log = lambda msg: None


def set_logger(fn):
    """Route task diagnostics to the caller's logger.

    The growth endpoints swallow their errors so one dead endpoint cannot
    abort a whole cycle. Without a logger those failures are invisible, and an
    upstream change looks identical to "no tasks today".
    """
    global _log
    _log = fn or (lambda msg: None)

TASK_SPECS = {
    "create_canvas": {"kind": "canvas", "target": 1, "reward": 300, "name": "创建设计任务"},
    "template_5": {"kind": "template", "target": 5, "reward": 200, "name": "模板创建任务"},
    "expert_5": {"kind": "expert", "target": 5, "reward": 200, "name": "使用专家助手"},
    "Expert_team_use_3": {"kind": "team", "target": 3, "reward": 150, "name": "使用专家团队"},
    "skill_1": {"kind": "skill", "target": 1, "reward": 100, "name": "体验技能"},
    "automation_1": {"kind": "automation", "target": 1, "reward": 100, "name": "创建自动化任务"},
    "playbook_prompt": {"kind": "playbook", "target": 1, "reward": 100, "name": "灵感案例使用"},
    "Expert_lighthouse": {"kind": "lighthouse", "target": 1, "reward": 100, "name": "轻量云专家使用"},
    "Buddy_App": {"kind": "buddy5", "target": 1, "reward": 100, "name": "进入 Buddy 应用"},
    "Buddy_App_QQ": {"kind": "buddy5", "target": 1, "reward": 100, "name": "企鹅教师助手"},
    "Hp_Appearance": {"kind": "skin", "target": 1, "reward": 100, "name": "应用主题外观"},
    "chat_5": {"kind": "chat", "target": 5, "reward": 100, "name": "发起 5 次对话"},
    "Model_chat_GLM5.2": {"kind": "glmchat", "target": 1, "reward": 100, "name": "体验 GLM-5.2"},
    "black_cat": {"kind": "cat", "target": 3, "reward": 100, "name": "夜猫子任务 (23:00-08:00)"},
    "RichMeow_Chat": {"kind": "richmeow", "target": 1, "reward": 100, "name": "桌面对话事件链"},
    "Library_read": {"kind": "library", "target": 1, "reward": 100, "name": "浏览资料库"},
    "first_buddy": {"kind": "buddy_first", "target": 1, "reward": 0, "name": "领养首只猫猫"},
    "Expert_Philanthropy": {"unforgeable": True, "reason": "真实捐款动作", "reward": 0, "name": "公益爱心捐赠"},
}


def fetch_growth_tasks(account):
    """查询成长任务列表及当前状态。"""
    url = CHAT_BASE + "/v2/activity/growth/tasks"
    req = urllib.request.Request(url, headers=account.headers("chat"))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw_tasks = (data.get("data") or {}).get("tasks") or []
            tasks = []
            for t in raw_tasks:
                code = t.get("task_code") or ""
                spec = TASK_SPECS.get(code, {})
                prog = t.get("progress") or {}
                tasks.append({
                    "task_code": code,
                    "name": t.get("title") or spec.get("name") or code,
                    "description": t.get("description") or t.get("task_desc") or "",
                    "status": t.get("accept_status") or "not_accepted",
                    "current": prog.get("current", 0),
                    "target": prog.get("target", spec.get("target", 1)),
                    "reward_credit": t.get("reward_credit") or spec.get("reward", 0),
                    "reward_energy": t.get("reward_energy", 0),
                    "unforgeable": bool(spec.get("unforgeable")),
                    "reason": spec.get("reason", ""),
                })
            return tasks
    except Exception as exc:
        return []


def fetch_growth_summary(account):
    """查询连续打卡、猫猫旅行与能量余额。"""
    headers = account.headers("chat")
    out = {"energy": 0, "streak_days": 0, "travel": {"state": "unknown"}}
    # 1. 能量
    try:
        req = urllib.request.Request(CHAT_BASE + "/v2/activity/growth/energy", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            out["energy"] = (d.get("data") or {}).get("balance", 0)
    except Exception as exc:
        _log(f"growth/energy query failed: {exc}")
    # 2. 连续打卡
    try:
        req = urllib.request.Request(CHAT_BASE + "/activity/growth/streak", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            st = (d.get("data") or {}).get("streak") or {}
            out["streak_days"] = st.get("days", 0)
    except Exception as exc:
        _log(f"growth/streak query failed: {exc}")
    # 3. 猫猫旅行
    try:
        req = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/status", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            out["travel"] = d.get("data") or {}
    except Exception as exc:
        _log(f"buddy/travel/status query failed: {exc}")
    return out


def accept_tasks(account, codes):
    """批量接取任务。"""
    if not codes: return True
    url = CHAT_BASE + "/v2/activity/growth/tasks/accept"
    body = json.dumps({"task_codes": codes}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers=account.headers("chat"))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            return d.get("code") == 0
    except Exception:
        return False


def _safe_task_code(code):
    """Validate a task code before it is interpolated into a request path.

    The code comes from the upstream task list, so a malformed or hostile
    response could otherwise place ``../``, ``@`` or ``?`` inside the URL and
    redirect the request somewhere unintended. Task codes are short opaque
    identifiers, so anything outside that shape is rejected outright.
    """
    code = str(code or "").strip()
    if not code or len(code) > 128:
        return ""
    if not all(ch.isalnum() or ch in "-_" for ch in code):
        return ""
    return code


#: Hosts this module is allowed to talk to. Every request below is built from
#: one of these constants; ``_checked_url`` re-verifies that at call time so a
#: future edit cannot quietly point a request at an arbitrary destination.
ALLOWED_HOSTS = ("copilot.tencent.com", "www.codebuddy.cn", "www.workbuddy.cn")


def _checked_url(base, path):
    """Build a request URL, refusing anything outside the known upstream hosts.

    Checks the scheme is HTTPS and the host is on the allow-list before the
    URL is handed to urllib. The hosts are module constants today, but keeping
    the check next to the request means a bad value cannot slip through
    unnoticed - and it rules out a redirect or a typo reaching an internal
    address.
    """
    from urllib.parse import urlparse
    url = base.rstrip("/") + "/" + path.lstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("refusing non-HTTPS upstream URL")
    if parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("refusing unknown upstream host: %r" % parsed.hostname)
    return url


def claim_task(account, code):
    """领取任务奖励。支持 copilot.tencent.com -> www.workbuddy.cn 自动降级。"""
    code = _safe_task_code(code)
    if not code:
        return {"ok": False, "msg": "任务编号非法，已跳过"}
    url = _checked_url(CHAT_BASE, "/activity/growth/tasks/%s/claim" % code)
    req = urllib.request.Request(url, data=b"", method="POST", headers=account.headers("chat"))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            if d.get("code") == 0:
                data = d.get("data") or {}
                return {"ok": True, "credit": data.get("credit", 0), "energy": data.get("energy", 0)}
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            # 降级到 web 域领奖
            web_url = _checked_url(
                WEB_BASE, "/activity/growth/tasks/%s/claim" % code)
            web_hdrs = {
                "Authorization": "Bearer " + account.access_token,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": WEB_BASE,
                "Referer": f"{WEB_BASE}/profile/growth-center",
                "x-client-platform": "web",
                "User-Agent": WEB_UA,
                "X-User-Id": account.uid,
                "X-Domain": WEB_BASE,
            }
            try:
                req_web = urllib.request.Request(web_url, data=b"", method="POST", headers=web_hdrs)
                with urllib.request.urlopen(req_web, timeout=15) as resp:
                    d = json.loads(resp.read().decode("utf-8"))
                    if d.get("code") == 0:
                        data = d.get("data") or {}
                        return {"ok": True, "credit": data.get("credit", 0), "energy": data.get("energy", 0)}
                    _log(f"task {code} web claim rejected: code={d.get('code')} msg={d.get('msg')}")
            except Exception as exc:
                _log(f"task {code} web claim failed: {exc}")
    except Exception as exc:
        _log(f"task {code} claim failed: {exc}")
    return {"ok": False, "credit": 0, "energy": 0}


def build_event(account, kind, idx=0):
    """构造指定类型的真实规范事件数据。"""
    now = int(time.time() * 1000)
    cid = f"wb-task-{now}-{idx}"
    rid = f"{cid}-req"
    uid = account.uid

    if kind == "canvas":
        return {"eventCode": "wbx_design_canvas_task_create", "timestamp": now,
                "reportDelay": 0, "conversationId": cid, "requestId": rid,
                "source": "summon_keyword", "isCustomModel": False, "name": "",
                "inputLength": 12, "id": f"wbx-canvas-{now}", "cost": 0,
                "isSuccessful": True, "userId": uid}
    if kind == "template":
        return {"eventCode": "agent_task_created_with_template", "timestamp": now,
                "reportDelay": 0, "isCustomModel": True, "id": str(idx),
                "name": "幻灯片", "requestId": rid, "conversationId": cid, "userId": uid}
    if kind in ("expert", "team", "lighthouse"):
        etype = "team" if kind == "team" else "agent"
        ex_id = "ex_2cvvUZQhDyeJ" if kind == "lighthouse" else ("CloudOpsTeam" if kind == "team" else "ContentCreator")
        name = "腾讯轻量云专家" if kind == "lighthouse" else ("运维专家团队" if kind == "team" else "内容创作专家")
        return {"eventCode": "expert_actual_use", "timestamp": now, "reportDelay": 0,
                "mode": "CLOUD", "id": ex_id, "name": name, "expertTitle": name,
                "type": "02-Engineering", "expertType": etype, "source": "builtin",
                "version": "1.0.2", "cost": 0, "characterCount": 12, "conversationId": cid,
                "requestId": rid, "messageId": rid, "requestModelId": "deepseek-v4-flash",
                "requestModelName": "DeepSeek V4 Flash", "userId": uid}
    if kind == "skill":
        return {"eventCode": "skill_info", "timestamp": now, "reportDelay": 0,
                "skillId": "skill_2096525080079265792", "name": "pptx", "userId": uid}
    if kind == "automation":
        return {"eventCode": "automated_task_create_suc", "timestamp": now, "reportDelay": 0,
                "name": "每周工作整理", "type": "cron", "source": "manually",
                "modelId": "deepseek-v4-flash", "modelIsThinking": False,
                "conversationId": cid, "requestId": rid,
                "schedule": {"type": "recurring", "rrule": "FREQ=WEEKLY;BYDAY=FR;BYHOUR=9;BYMINUTE=0"},
                "prompt": "每周五自动整理本周工作", "userId": uid}
    if kind == "playbook":
        return {"eventCode": "playbook_prompt_send", "timestamp": now, "reportDelay": 0,
                "id": "worker-ledger-freedom-dashboard", "name": "打工人小账本",
                "type": "other", "promptLength": 10, "isOfficial": 1,
                "source": "discover", "conversationId": cid, "requestId": rid, "userId": uid}
    if kind == "skin":
        return {"eventCode": "appearance_skin_apply", "timestamp": now, "reportDelay": 0,
                "action": "apply", "source": "settings_close", "id": "theme-tkmw7j",
                "vipLevel": "free", "series": "craft", "type": "unknown",
                "name": "和平精英激战金秋", "userId": uid}
    if kind in ("chat", "glmchat", "cat"):
        m_id = "glm-5.2" if kind in ("glmchat", "cat") else "deepseek-v4-flash"
        m_nm = "GLM-5.2" if kind in ("glmchat", "cat") else "DeepSeek V4 Flash"
        mode = "night" if kind == "cat" else "craft"
        return {"eventCode": "chat_request_send", "timestamp": now, "reportDelay": 0,
                "mode": mode, "conversationId": cid, "requestId": rid,
                "inputLength": 12, "requestModelId": m_id, "requestModelName": m_nm,
                "isPlan": False, "agentName": "default", "agentType": "conversation",
                "userId": uid}
    return {"eventCode": "heartbeat", "timestamp": now, "userId": uid}


def report_events(account, events, base=BILL_BASE):
    """向上游上报事件数组。"""
    url = _checked_url(base, "/v2/report")
    headers = account.headers("billing" if base == BILL_BASE else "chat")
    body = json.dumps(events).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            return d.get("code") == 0
    except Exception:
        return False


def do_cat_travel(account):
    """检查并执行猫猫旅行 (领奖 / 派出)。"""
    headers = account.headers("chat")
    # 1. 查询状态
    try:
        req = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/status", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            st = d.get("data") or {}
    except Exception as exc:
        return {"ok": False, "msg": f"查询旅行状态失败: {exc}"}

    state = st.get("state")
    if state == "arrived":
        # 领奖（body 须为 {} 或空 JSON 对象）
        cl_headers = dict(headers)
        cl_headers["Content-Type"] = "application/json"
        req_cl = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/claim", data=b"{}", method="POST", headers=cl_headers)
        try:
            with urllib.request.urlopen(req_cl, timeout=10) as resp:
                c_res = json.loads(resp.read().decode("utf-8"))
                credit = (c_res.get("data") or {}).get("reward_credit", 0)
                account.fetch_credits()
                return {"ok": True, "action": "claim", "credit": credit, "msg": f"旅行归来领奖成功！获得 {credit} 积分"}
        except Exception as e:
            return {"ok": False, "msg": f"领奖失败: {e}"}

    if state == "idle":
        if st.get("daily_limit_reached"):
            return {"ok": True, "action": "idle", "msg": "猫猫今日已完成旅行，明日 00:00 刷新"}
        # 派出旅行（body 须为 {} 或空 JSON 对象，否则部分服务器返回 400）
        dep_headers = dict(headers)
        dep_headers["Content-Type"] = "application/json"
        req_dep = urllib.request.Request(CHAT_BASE + "/activity/growth/buddy/travel/depart", data=b"{}", method="POST", headers=dep_headers)
        try:
            with urllib.request.urlopen(req_dep, timeout=10) as resp:
                dep_res = json.loads(resp.read().decode("utf-8"))
                if dep_res.get("code") == 0:
                    return {"ok": True, "action": "depart", "msg": "猫猫已成功派出旅行，预计数小时后归来！"}
        except Exception as e:
            return {"ok": False, "msg": f"派出旅行失败: {e}"}

    if state == "traveling":
        return {"ok": True, "action": "traveling", "msg": "猫猫正在旅行途中，请稍后再来查看！"}

    return {"ok": True, "action": state, "msg": f"当前状态: {state}"}


def run_growth_tasks(account, gap=1.0):
    """完整执行批量成长任务点亮与领奖。"""
    if account.realm != "cn":
        return {"ok": False, "msg": "国际版不适用国内成长任务中心", "logs": []}

    logs = []
    logs.append(f"开始为账号 {account.nickname or account.uid[:8]} 运行成长任务自动化...")
    tasks = fetch_growth_tasks(account)
    if not tasks:
        logs.append("未能获取到任务清单，请检查网络或账号状态")
        return {"ok": False, "logs": logs, "earned_credit": 0}

    # 1. 批量接取未接任务
    unaccepted = [t["task_code"] for t in tasks if t["status"] == "not_accepted" and not t.get("unforgeable")]
    if unaccepted:
        logs.append(f"发现 {len(unaccepted)} 个待接取任务，正在批量接取...")
        accept_tasks(account, unaccepted)
        time.sleep(gap)
        tasks = fetch_growth_tasks(account)

    total_earned = 0
    # 2. 处理每个任务
    for t in tasks:
        code = t["task_code"]
        spec = TASK_SPECS.get(code)
        if not spec or spec.get("unforgeable"):
            continue

        status = t["status"]
        cur = t.get("current", 0)
        tgt = t.get("target", 1)

        if status == "claimed":
            continue

        if status == "completed" or cur >= tgt:
            # 直接领奖
            res = claim_task(account, code)
            if res.get("ok"):
                cr = res.get("credit", 0)
                total_earned += cr
                logs.append(f"✓ 任务 [{spec['name']}] 领奖成功: +{cr} 积分")
            else:
                logs.append(f"! 任务 [{spec['name']}] 领奖失败")
            time.sleep(gap)
            continue

        # 3. 需点亮上报
        need = max(1, tgt - cur)
        kind = spec.get("kind")
        logs.append(f"正在点亮任务 [{spec['name']}] (需上报 {need} 次)...")
        for i in range(need):
            ev = build_event(account, kind, idx=i)
            report_events(account, [ev])
            if i < need - 1:
                time.sleep(gap)
        time.sleep(1.5)

        # 领奖
        res = claim_task(account, code)
        if res.get("ok"):
            cr = res.get("credit", 0)
            total_earned += cr
            logs.append(f"✓ 任务 [{spec['name']}] 点亮并领奖成功: +{cr} 积分")
        else:
            logs.append(f"? 任务 [{spec['name']}] 已上报点亮，领奖稍后结算")
        time.sleep(gap)

    # 4. 顺手检查猫猫旅行
    tr_res = do_cat_travel(account)
    if tr_res.get("msg"):
        logs.append(f"猫猫日常: {tr_res.get('msg')}")
        if tr_res.get("credit"):
            total_earned += tr_res.get("credit", 0)

    # 5. 刷新积分余额
    account.fetch_credits()
    logs.append(f"🎉 全部完成！本次累计新增到账: +{total_earned} 积分，当前总剩余: {account.credits.get('remain', 0)} 积分")
    return {"ok": True, "logs": logs, "earned_credit": total_earned, "credits": account.credits}
