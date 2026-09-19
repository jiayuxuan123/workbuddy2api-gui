"""wb_sessions 会话管理测试：纯函数 + 三家 provider 扫描/读取/删除 + HTTP 端点。

全部文件操作在 tempfile 沙箱里做：
- 扫描/读取/删除走 wb_sessions 的 home 参数或 patch wb_sessions._home；
- HTTP 测试另需 patch expanduser（请求线程里 scan_sessions() 用 _home()），
  并保证 wb_gateway/wb_proxy 导入前设置 WB_DATA_DIR。
绝不触碰真实的 ~/.claude、~/.codex、~/.gemini。
"""
import json
import os
import socket
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wb_sessions  # noqa: E402

FAILURES = []
_COUNT = [0]


def check(name, cond, detail=""):
    _COUNT[0] += 1
    if cond:
        print("  ok  %s" % name)
    else:
        print("FAIL  %s  %s" % (name, detail))
        FAILURES.append(name)


def _mk(home, rel, content, binary=False):
    path = os.path.join(home, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if binary:
        with open(path, "wb") as fh:
            fh.write(content)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    return path


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def test_utils():
    print("[utils]")
    check("ts ms passthrough", wb_sessions._ts_to_ms(1_771_061_953_033) == 1_771_061_953_033)
    check("ts seconds", wb_sessions._ts_to_ms(1_771_061_953) == 1_771_061_953_000)
    check("ts rfc3339", wb_sessions._ts_to_ms("1970-01-01T00:00:01Z") == 1000)
    check("ts rfc3339 offset",
          wb_sessions._ts_to_ms("2026-03-06T21:50:12+08:00")
          == wb_sessions._ts_to_ms("2026-03-06T13:50:12Z"))
    check("ts garbage", wb_sessions._ts_to_ms("not-a-date") is None)
    check("ts none", wb_sessions._ts_to_ms(None) is None)

    check("truncate short", wb_sessions.truncate_summary("hello", 10) == "hello")
    long = "x" * 200
    t = wb_sessions.truncate_summary(long, 80)
    check("truncate long", len(t) == 83 and t.endswith("..."))
    check("truncate strips", wb_sessions.truncate_summary("  hi  ", 10) == "hi")

    check("basename", wb_sessions._path_basename("C:\\a\\b\\proj") == "proj")
    check("basename posix", wb_sessions._path_basename("/home/u/proj/") == "proj")

    text = wb_sessions._extract_text(
        [{"type": "tool_use", "id": "t1", "name": "Write",
          "input": {"file_path": "a.txt"}}])
    check("tool_use rendered", text == "[Tool: Write]", text)
    text = wb_sessions._extract_text(
        [{"type": "text", "text": "part1"}, {"type": "text", "text": "part2"}])
    check("multi-part joined", text == "part1\npart2", text)


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

def test_claude(home):
    print("[claude]")
    proj = os.path.join(home, ".claude", "projects", "C--tmp-project")
    body = (
        json.dumps({"type": "file-history-snapshot", "messageId": "m1",
                    "snapshot": {}}) + "\n"
        + json.dumps({"type": "user", "cwd": "/tmp/project",
                      "sessionId": "sess-1", "isMeta": True,
                      "timestamp": "2026-03-06T10:00:00Z",
                      "message": {"role": "user",
                                  "content": "<local-command-caveat>caveat"
                                             "</local-command-caveat>"}}) + "\n"
        + json.dumps({"type": "user", "cwd": "/tmp/project",
                      "sessionId": "sess-1",
                      "timestamp": "2026-03-06T10:01:00Z",
                      "message": {"role": "user", "content": "帮我修复登录bug"}}) + "\n"
        + json.dumps({"type": "assistant", "timestamp": "2026-03-06T10:02:00Z",
                      "message": {"role": "assistant", "content": [
                          {"type": "text", "text": "好的，我来看。"},
                          {"type": "tool_use", "id": "t1", "name": "Read",
                           "input": {}}]}}) + "\n"
        + json.dumps({"type": "user", "timestamp": "2026-03-06T10:03:00Z",
                      "message": {"role": "user", "content": [
                          {"type": "tool_result", "tool_use_id": "t1",
                           "content": "file content"}]}}) + "\n"
        + json.dumps({"type": "custom-title", "customTitle": "fix-login",
                      "sessionId": "sess-1"}) + "\n"
    )
    _mk(home, os.path.join(".claude", "projects", "C--tmp-project", "sess-1.jsonl"),
        body)
    # sidecar 目录应随会话一起删除
    _mk(home, os.path.join(".claude", "projects", "C--tmp-project",
                           "sess-1", "tool-results", "t1.txt"), "x")
    # agent 会话与 journal 必须被排除
    _mk(home, os.path.join(".claude", "projects", "C--tmp-project",
                           "agent-abc.jsonl"), "{}\n")
    _mk(home, os.path.join(".claude", "projects", "C--tmp-project",
                           "journal.jsonl"), "{}\n")

    sessions = wb_sessions.scan_sessions(home=home)
    cl = [s for s in sessions if s["provider_id"] == "claude"]
    check("claude one session", len(cl) == 1, str(len(cl)))
    s = cl[0]
    check("claude id", s["session_id"] == "sess-1")
    check("claude title custom", s["title"] == "fix-login", str(s["title"]))
    check("claude project_dir", s["project_dir"] == "/tmp/project")
    # created_at 取首条带时间戳的行（这里是 10:00 的 caveat 消息），
    # 与 cc-switch 行为一致：标题跳过 caveat，但时间戳照取。
    exp_created = wb_sessions._ts_to_ms("2026-03-06T10:00:00Z")
    check("claude created", s["created_at"] == exp_created, str(s["created_at"]))
    check("claude resume", s["resume_command"] == "claude --resume sess-1")
    check("claude source abs", os.path.isabs(s["source_path"]))

    msgs = wb_sessions.load_messages("claude", s["source_path"], home=home)
    roles = [m["role"] for m in msgs]
    # caveat 消息带 isMeta 被跳过：剩 user / assistant(含 tool_use) / tool 三条
    check("claude msg count", len(msgs) == 3, str(roles))
    check("claude roles", roles == ["user", "assistant", "tool"], str(roles))
    check("claude caveat skipped",
          all("local-command-caveat" not in m["content"] for m in msgs))
    check("claude tool_use marker",
          any("[Tool: Read]" in m["content"] for m in msgs))
    check("claude tool result content",
          any(m["role"] == "tool" and m["content"] == "file content" for m in msgs))

    # 删除：ID 不匹配拒绝；匹配则文件 + sidecar 一起没了
    try:
        wb_sessions.delete_session("claude", "wrong-id", s["source_path"], home=home)
        check("claude delete mismatch rejected", False)
    except wb_sessions.SessionError:
        check("claude delete mismatch rejected", True)
    wb_sessions.delete_session("claude", "sess-1", s["source_path"], home=home)
    check("claude file gone", not os.path.exists(s["source_path"]))
    sidecar = os.path.join(proj, "sess-1")
    check("claude sidecar gone", not os.path.exists(sidecar))


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

def _codex_jsonl(session_id, cwd, lines):
    out = [json.dumps({"timestamp": "2026-03-06T21:50:12Z", "type": "session_meta",
                       "payload": {"id": session_id, "cwd": cwd}})]
    out.extend(lines)
    return "\n".join(out) + "\n"


def test_codex(home):
    print("[codex]")
    sid = "019cc369-bd7c-7891-b371-7b20b4fe0b18"
    body = _codex_jsonl(sid, "/tmp/project", [
        json.dumps({"timestamp": "2026-03-06T21:50:13Z", "type": "response_item",
                    "payload": {"type": "message", "role": "user",
                                "content": [{"type": "input_text",
                                             "text": "fix the parser"}]}}),
        json.dumps({"timestamp": "2026-03-06T21:50:14Z", "type": "response_item",
                    "payload": {"type": "function_call", "name": "shell",
                                "arguments": "{}"}}),
        json.dumps({"timestamp": "2026-03-06T21:50:15Z", "type": "response_item",
                    "payload": {"type": "function_call_output", "output": "ok"}}),
        json.dumps({"timestamp": "2026-03-06T21:50:16Z", "type": "response_item",
                    "payload": {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text",
                                             "text": "done"}]}}),
    ])
    _mk(home, os.path.join(".codex", "sessions", "2026", "03", "06",
                           "rollout-2026-03-06T21-50-12-%s.jsonl" % sid), body)
    archived = _codex_jsonl("aaaabbbb-cccc-dddd-eeee-ffff00001111", "/tmp/archive", [
        json.dumps({"timestamp": "2026-03-06T21:50:13Z", "type": "response_item",
                    "payload": {"type": "message", "role": "user",
                                "content": "archived work"}})])
    _mk(home, os.path.join(".codex", "archived_sessions", "archived.jsonl"), archived)
    # 子代理会话必须被排除
    sub = json.dumps({"timestamp": "2026-03-06T21:50:12Z", "type": "session_meta",
                      "payload": {"id": "sub-1", "cwd": "/tmp/x",
                                  "source": {"subagent": "spawn"}}})
    _mk(home, os.path.join(".codex", "sessions", "2026", "03", "06",
                           "sub.jsonl"), sub + "\n")

    sessions = wb_sessions.scan_sessions(home=home)
    cx = [s for s in sessions if s["provider_id"] == "codex"]
    ids = sorted(s["session_id"] for s in cx)
    check("codex two sessions", len(cx) == 2, str(ids))
    main = next(s for s in cx if s["session_id"] == sid)
    check("codex title from msg", main["title"] == "fix the parser", str(main["title"]))
    check("codex cwd", main["project_dir"] == "/tmp/project")
    check("codex resume", main["resume_command"] == "codex resume %s" % sid)

    msgs = wb_sessions.load_messages("codex", main["source_path"], home=home)
    roles = [m["role"] for m in msgs]
    check("codex msg count", len(msgs) == 4, str(roles))
    check("codex roles", roles == ["user", "assistant", "tool", "assistant"],
          str(roles))
    check("codex tool marker", any(m["content"] == "[Tool: shell]" for m in msgs))
    check("codex tool output", any(m["role"] == "tool" and m["content"] == "ok"
                                   for m in msgs))

    # DB 标题优先于首条用户消息；等价标题被过滤
    db = os.path.join(home, ".codex", "state_5.sqlite")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE threads (id TEXT, title TEXT, first_user_message TEXT)")
    conn.execute("INSERT INTO threads VALUES (?, ?, ?)",
                 (sid, "Parser Fix Thread", "ignored"))
    conn.execute("INSERT INTO threads VALUES (?, ?, ?)",
                 ("aaaabbbb-cccc-dddd-eeee-ffff00001111", "  dup  ", "dup"))
    conn.commit()
    conn.close()
    sessions = wb_sessions.scan_sessions(home=home)
    main = next(s for s in sessions
                if s["provider_id"] == "codex" and s["session_id"] == sid)
    check("codex db title wins", main["title"] == "Parser Fix Thread",
          str(main["title"]))
    archived_meta = next(s for s in sessions
                         if s["provider_id"] == "codex"
                         and s["session_id"] == "aaaabbbb-cccc-dddd-eeee-ffff00001111")
    check("codex db dup-title filtered", archived_meta["title"] == "archived work",
          str(archived_meta["title"]))

    # IDE 上下文标题提取（取最后一段请求）
    cand = wb_sessions._codex_title_candidate(
        "# Context from my IDE setup:\n\n## My request for Codex:\nrefactor it")
    check("codex ide prompt", cand == "refactor it", str(cand))

    try:
        wb_sessions.delete_session("codex", "mismatch", main["source_path"], home=home)
        check("codex delete mismatch rejected", False)
    except wb_sessions.SessionError:
        check("codex delete mismatch rejected", True)
    wb_sessions.delete_session("codex", sid, main["source_path"], home=home)
    check("codex file gone", not os.path.exists(main["source_path"]))


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

def test_gemini(home):
    print("[gemini]")
    chat = {
        "sessionId": "gemini-session-9",
        "startTime": "2026-03-06T10:17:58.000Z",
        "lastUpdated": "2026-03-06T10:20:00.000Z",
        "messages": [
            {"id": "1", "timestamp": "2026-03-06T10:17:58Z", "type": "user",
             "content": [{"text": "查询天气"}]},
            {"id": "2", "timestamp": "2026-03-06T10:18:00Z", "type": "gemini",
             "content": "", "toolCalls": [{"id": "c1", "name": "web_search",
                                           "args": {}}]},
            {"id": "3", "timestamp": "2026-03-06T10:19:00Z", "type": "gemini",
             "content": "今天晴。"},
            {"id": "4", "timestamp": "2026-03-06T10:19:30Z", "type": "info",
             "content": "system info"},
            {"id": "5", "timestamp": "2026-03-06T10:19:40Z", "type": "error",
             "content": "MCP ERROR"},
        ],
    }
    _mk(home, os.path.join(".gemini", "tmp", "proj-hash", "chats",
                           "session-abc.json"),
        json.dumps(chat, ensure_ascii=False))
    _mk(home, os.path.join(".gemini", "tmp", "proj-hash", ".project_root"),
        "/home/u/weather-app\n")

    sessions = wb_sessions.scan_sessions(home=home)
    gm = [s for s in sessions if s["provider_id"] == "gemini"]
    check("gemini one session", len(gm) == 1, str(len(gm)))
    s = gm[0]
    check("gemini id", s["session_id"] == "gemini-session-9")
    check("gemini title", s["title"] == "查询天气", str(s["title"]))
    check("gemini project_root", s["project_dir"] == "/home/u/weather-app",
          str(s["project_dir"]))
    check("gemini last_active",
          s["last_active_at"] == wb_sessions._ts_to_ms("2026-03-06T10:20:00.000Z"),
          str(s["last_active_at"]))
    check("gemini resume", s["resume_command"] == "gemini --resume gemini-session-9")

    msgs = wb_sessions.load_messages("gemini", s["source_path"], home=home)
    check("gemini msg count", len(msgs) == 3, str(len(msgs)))
    check("gemini roles", [m["role"] for m in msgs] == ["user", "assistant",
                                                        "assistant"])
    check("gemini array content", msgs[0]["content"] == "查询天气", msgs[0]["content"])
    check("gemini tool call", "[Tool: web_search]" in msgs[1]["content"],
          msgs[1]["content"])

    try:
        wb_sessions.delete_session("gemini", "other", s["source_path"], home=home)
        check("gemini delete mismatch rejected", False)
    except wb_sessions.SessionError:
        check("gemini delete mismatch rejected", True)
    wb_sessions.delete_session("gemini", "gemini-session-9", s["source_path"],
                               home=home)
    check("gemini file gone", not os.path.exists(s["source_path"]))


# ---------------------------------------------------------------------------
# 安全校验：路径越界 / 大文件 head-tail / 上限
# ---------------------------------------------------------------------------

def test_security_and_limits(home):
    print("[security & limits]")
    # 路径越界必须被拒
    outside = _mk(home, "outside.jsonl", "{}\n")
    try:
        wb_sessions.load_messages("claude", outside, home=home)
        check("load outside rejected", False)
    except wb_sessions.SessionError:
        check("load outside rejected", True)
    try:
        wb_sessions.delete_session("claude", "x", outside, home=home)
        check("delete outside rejected", False)
    except wb_sessions.SessionError:
        check("delete outside rejected", True)
    try:
        wb_sessions.load_messages("claude", "", home=home)
        check("empty path rejected", False)
    except wb_sessions.SessionError:
        check("empty path rejected", True)
    # 根目录不存在时 provider 校验也要拒绝
    try:
        wb_sessions.load_messages("codex", outside, home=home)
        check("missing root rejected", False)
    except wb_sessions.SessionError:
        check("missing root rejected", True)

    # 大文件：尾部窗口读取（残行丢弃）+ title/summary 仍能解析
    big_dir = os.path.join(home, ".claude", "projects", "C--big")
    pad = json.dumps({"type": "filler", "message": {"role": "assistant",
                                                    "content": "x" * 400}}) + "\n"
    body = (json.dumps({"sessionId": "big-1", "cwd": "/tmp/big",
                        "timestamp": "2026-03-06T10:00:00Z",
                        "message": {"role": "user", "content": "big session start"}})
            + "\n" + pad * 200)
    path = _mk(home, os.path.join(".claude", "projects", "C--big", "big-1.jsonl"),
               body)
    check("big file size", os.path.getsize(path) > 16384)
    sessions = wb_sessions.scan_sessions(home=home)
    big = next(s for s in sessions if s["provider_id"] == "claude"
               and s["session_id"] == "big-1")
    check("big file title", big["title"] == "big session start", str(big["title"]))
    check("big file created", big["created_at"] == wb_sessions._ts_to_ms("2026-03-06T10:00:00Z"),
          str(big["created_at"]))

    # head/tail 直接验证：残行被丢、不越界
    head, tail = wb_sessions._read_head_tail(path, 2, 3)
    check("head lines", len(head) == 2 and '"big-1"' in head[0])
    check("tail parses", all(wb_sessions._try_json(l) is not None for l in tail))

    # 消息上限：只保留最近 _MAX_MESSAGES 条
    many = "\n".join(
        json.dumps({"timestamp": 1772814000000 + i,
                    "message": {"role": "user", "content": "m%d" % i}})
        for i in range(wb_sessions._MAX_MESSAGES + 50))
    p2 = _mk(home, os.path.join(".claude", "projects", "C--big", "many.jsonl"),
             many + "\n")
    msgs = wb_sessions.load_messages("claude", p2, home=home)
    check("msg cap", len(msgs) == wb_sessions._MAX_MESSAGES, str(len(msgs)))
    check("msg cap keeps tail", msgs[-1]["content"] == "m%d" % (wb_sessions._MAX_MESSAGES + 49),
          msgs[-1]["content"])

    # 超长内容截断
    huge = json.dumps({"message": {"role": "user", "content": "y" * 9000}}) + "\n"
    p3 = _mk(home, os.path.join(".claude", "projects", "C--big", "huge.jsonl"),
             huge)
    msgs = wb_sessions.load_messages("claude", p3, home=home)
    check("content cap", len(msgs[0]["content"]) <= wb_sessions._MAX_CONTENT_CHARS + 20)


# ---------------------------------------------------------------------------
# HTTP 端点
# ---------------------------------------------------------------------------

class _Client:
    def __init__(self, port):
        self.port = port

    def request(self, method, path, body=None, headers=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")


def test_http(home):
    print("[http endpoints]")
    import wb_gateway
    import wb_proxy

    # 沙箱 usage 目录，避免触碰真实用量日志
    usage_dir = os.path.join(home, "usage")
    os.makedirs(usage_dir, exist_ok=True)
    old_dir, old_log = wb_proxy.USAGE_DIR, wb_proxy.USAGE_LOG
    wb_proxy.USAGE_DIR = usage_dir
    wb_proxy.USAGE_LOG = os.path.join(usage_dir, "usage.jsonl")

    old_home = wb_sessions._home
    wb_sessions._home = lambda: home

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
        status, _ = client.request("GET", "/sessions")
        check("GET /sessions no token 401", status == 401, str(status))
        status, _ = client.request("POST", "/sessions/delete", body={})
        check("POST /sessions/delete no token 401", status == 401, str(status))

        # 供列表/消息/删除用例读取的会话：先建再断言
        chat = {"sessionId": "gemini-http-1",
                "startTime": "2026-03-06T10:00:00Z", "lastUpdated": "2026-03-06T10:05:00Z",
                "messages": [{"id": "1", "timestamp": "2026-03-06T10:00:00Z",
                              "type": "user", "content": "http case"}]}
        path = _mk(home, os.path.join(".gemini", "tmp", "p2", "chats",
                                      "session-http.json"),
                   json.dumps(chat))

        status, body = client.request("GET", "/sessions", headers=hdr)
        check("GET /sessions 200", status == 200, str(status))
        data = json.loads(body)
        ids = {(s["provider_id"], s["session_id"]) for s in data.get("sessions", [])}
        check("GET sessions list", ("gemini", "gemini-http-1") in ids, str(ids))

        status, body = client.request("GET", "/sessions", headers=hdr)
        data = json.loads(body)
        target = next(s for s in data["sessions"]
                      if s["session_id"] == "gemini-http-1")
        check("scan sees new session", target is not None)

        from urllib.parse import quote
        q = "/sessions/messages?provider=gemini&path=%s" % quote(target["source_path"], safe="")
        status, body = client.request("GET", q, headers=hdr)
        check("GET messages 200", status == 200, str(status))
        mdata = json.loads(body)
        check("messages content", mdata["messages"][0]["content"] == "http case",
              body[:200])

        status, body = client.request(
            "GET", "/sessions/messages?provider=claude&path=%s"
                   % quote(os.path.join(home, "evil.jsonl"), safe=""), headers=hdr)
        check("messages outside 400", status == 400, str(status))

        status, body = client.request(
            "POST", "/sessions/delete",
            body={"provider": "gemini", "session_id": "gemini-http-1",
                  "source_path": target["source_path"]}, headers=hdr)
        check("POST delete 200", status == 200, body[:200])
        check("delete removed file", not os.path.exists(path))

        status, body = client.request(
            "POST", "/sessions/delete",
            body={"provider": "gemini", "session_id": "x", "source_path": path},
            headers=hdr)
        check("delete missing 400", status == 400, str(status))

        status, body = client.request("POST", "/sessions/delete",
                                      body={"provider": "gemini"}, headers=hdr)
        check("delete missing fields 400", status == 400, str(status))
    finally:
        gw.stop()
        wb_sessions._home = old_home
        wb_proxy.USAGE_DIR, wb_proxy.USAGE_LOG = old_dir, old_log
        wb_proxy.PANEL.revoke(token)


def main():
    # 沙箱必须在 wb_gateway / wb_proxy 导入之前设好 WB_DATA_DIR。
    tmp = tempfile.mkdtemp(prefix="wb_sessions_test_")
    os.environ["WB_DATA_DIR"] = tmp
    try:
        test_utils()
        test_claude(tmp)
        test_codex(tmp)
        test_gemini(tmp)
        test_security_and_limits(tmp)
        test_http(tmp)
    finally:
        print("")
        print("checks: %d, failures: %d" % (_COUNT[0], len(FAILURES)))
        if FAILURES:
            for f in FAILURES:
                print("  FAILED: %s" % f)
            sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
