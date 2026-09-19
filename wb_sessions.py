"""会话管理：扫描本机 Claude Code / Codex / Gemini CLI 的本地会话。

数据来源（只读；删除仅限对应根目录内且先解析校验会话 ID）：
- Claude:  ~/.claude/projects/**/*.jsonl（跳过 agent-* 与 journal.jsonl）
- Codex:   ~/.codex/sessions/** 与 ~/.codex/archived_sessions/**；标题参考
           ~/.codex/session_index.jsonl 与 state_5.sqlite 的 threads 表
           （sqlite_home 配置 / CODEX_SQLITE_HOME 环境变量可迁移 DB 位置）
- Gemini:  ~/.gemini/tmp/<project>/chats/session-*.json（.project_root 给出项目目录）

安全设计（对齐 cc-switch 同名功能）：
- 读取与删除都做根目录包含校验（realpath + 大小写归一），拒绝越界路径；
- 删除前重新解析会话文件并核对 session_id，防止凭路径盲删；
- SQLite 只读打开（mode=ro、busy_timeout=2s），绝不写 Codex 的数据库。

对外接口（wb_proxy 调用）：
- scan_sessions() -> [meta]   按 last_active_at（缺省 created_at）降序
- load_messages(provider, path) -> [msg]   只保留最近 _MAX_MESSAGES 条
- delete_session(provider, session_id, path)   失败抛 SessionError

meta 键：provider_id / session_id / title / summary / project_dir /
created_at / last_active_at（毫秒）/ source_path / resume_command。
msg 键：role（user|assistant|tool）/ content / ts（毫秒或 None）。
"""
import json
import os
import re
import shutil
import sqlite3
from collections import deque
from datetime import datetime, timezone
from urllib.request import pathname2url

TITLE_MAX_CHARS = 80
SUMMARY_MAX_CHARS = 160

# 面板展示用上限：单条消息内容截断、整段会话只留最近 N 条，防止超大响应。
_MAX_MESSAGES = 400
_MAX_CONTENT_CHARS = 4000

_HEAD_N = 10
_TAIL_N = 30
_SMALL_FILE = 16384      # 小于 16KB 的文件整体读入
_TAIL_WINDOW = 16384     # 大文件尾部读取窗口

PROVIDERS = ("claude", "codex", "gemini")

CODEX_STATE_DB_FILENAME = "state_5.sqlite"

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_SQLITE_HOME_RE = re.compile(r"^\s*sqlite_home\s*=\s*['\"](.+?)['\"]\s*(?:#.*)?$")

CODEX_IDE_CONTEXT_PREFIX = "# Context from my IDE setup:"
CODEX_REQUEST_MARKER = "my request for codex"


class SessionError(Exception):
    """可安全透传给面板的业务错误（路径越界、ID 不匹配、解析失败等）。"""


def _home():
    # 测试通过替换本函数把所有 provider 根目录指进沙箱。
    return os.path.expanduser("~")


def provider_roots(provider_id, home=None):
    """会话文件允许存在的根目录（删除/读取的包含校验边界）。"""
    home = home or _home()
    if provider_id == "claude":
        return [os.path.join(home, ".claude", "projects")]
    if provider_id == "codex":
        return [os.path.join(home, ".codex", "sessions"),
                os.path.join(home, ".codex", "archived_sessions")]
    if provider_id == "gemini":
        return [os.path.join(home, ".gemini", "tmp")]
    raise SessionError("unsupported provider: %s" % (provider_id or "<empty>"))


# ---------------------------------------------------------------------------
# 扫描入口
# ---------------------------------------------------------------------------

def scan_sessions(home=None):
    """扫描三家 CLI 的全部会话，按最近活跃时间降序返回。"""
    home = home or _home()
    out = []
    out.extend(_scan_claude(home))
    out.extend(_scan_codex(home))
    out.extend(_scan_gemini(home))
    out.sort(key=lambda m: m.get("last_active_at") or m.get("created_at") or 0,
             reverse=True)
    return out


def _walk_jsonl(root):
    if not os.path.isdir(root):
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".jsonl"):
                yield os.path.join(dirpath, name)


def _meta(provider, session_id, title, summary, project_dir,
          created_at, last_active_at, path, resume_fmt):
    return {
        "provider_id": provider,
        "session_id": session_id,
        "title": title,
        "summary": summary,
        "project_dir": project_dir,
        "created_at": created_at,
        "last_active_at": last_active_at,
        "source_path": os.path.abspath(path),
        "resume_command": resume_fmt % session_id,
    }


# ---------------------------------------------------------------------------
# 通用工具（head/tail 读取、时间戳、文本提取、截断）
# ---------------------------------------------------------------------------

def _read_head_tail(path, head_n=_HEAD_N, tail_n=_TAIL_N):
    """读前 head_n 行与后 tail_n 行；大文件尾部只读最后 16KB 窗口。

    返回 (head_lines, tail_lines)。seek 落在行中间时首行是残行，按
    cc-switch 的取舍直接丢弃该行。
    """
    try:
        size = os.path.getsize(path)
        if size < _SMALL_FILE:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            return lines[:head_n], lines[len(lines) - tail_n:] if tail_n else []
        head = []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                head.append(line)
                if i + 1 >= head_n:
                    break
        seek_pos = max(0, size - _TAIL_WINDOW)
        with open(path, "rb") as fh:
            fh.seek(seek_pos)
            blob = fh.read()
        tail_lines = blob.decode("utf-8", "replace").splitlines()
        if seek_pos > 0 and tail_lines:
            tail_lines = tail_lines[1:]  # 丢弃可能的残行
        return head, tail_lines[len(tail_lines) - tail_n:] if tail_n else []
    except OSError:
        return [], []


def _ts_to_ms(value):
    """时间戳统一为毫秒：>1e12 视为已是毫秒，否则按秒 ×1000；字符串走 RFC3339。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 1_000_000_000_000 else value * 1000
    if isinstance(value, float):
        n = int(value)
        return n if n > 1_000_000_000_000 else n * 1000
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if re.fullmatch(r"-?\d+(\.\d+)?", raw):
        return _ts_to_ms(float(raw) if "." in raw else int(raw))
    # 少数写库方会带纳秒级小数，截到 6 位保证 fromisoformat 兼容。
    raw = re.sub(r"(\.\d{6})\d+", r"\1", raw)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _try_json(line):
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _extract_text(content):
    """从消息 content（字符串 / 分段数组 / 对象）提取纯文本。

    tool_use|toolCall 分段渲染为 "[Tool: name]"；tool_result 取嵌套文本。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            text = _extract_text_item(item)
            if text and text.strip():
                parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return ""


def _extract_text_item(item):
    if not isinstance(item, dict):
        return None
    itype = item.get("type")
    itype = itype if isinstance(itype, str) else ""
    if itype in ("tool_use", "toolCall"):
        name = item.get("name")
        return "[Tool: %s]" % (name if isinstance(name, str) and name else "unknown")
    if itype == "tool_result":
        if "content" in item:
            text = _extract_text(item.get("content"))
            if text:
                return text
        return None
    for key in ("text", "input_text", "output_text"):
        value = item.get(key)
        if isinstance(value, str):
            return value
    if "content" in item:
        text = _extract_text(item.get("content"))
        if text:
            return text
    return None


def truncate_summary(text, max_chars):
    trimmed = (text or "").strip()
    if not trimmed:
        return ""
    if len(trimmed) <= max_chars:
        return trimmed
    return trimmed[:max_chars] + "..."


def _path_basename(value):
    trimmed = (value or "").strip()
    if not trimmed:
        return None
    normalized = trimmed.rstrip("/\\")
    if not normalized:
        return None
    segments = [s for s in re.split(r"[\\/]+", normalized) if s]
    return segments[-1] if segments else None


def _cap(text):
    if len(text) <= _MAX_CONTENT_CHARS:
        return text
    return text[:_MAX_CONTENT_CHARS] + "...[已截断]"


def _ensure_under_roots(path, roots, label):
    """路径必须真实存在且落在某个已存在的根目录内，否则抛 SessionError。"""
    if not path or not os.path.exists(path):
        raise SessionError("%s not found: %s" % (label, path))
    real = os.path.normcase(os.path.realpath(path))
    saw_root = False
    for root in roots:
        if not os.path.exists(root):
            continue
        saw_root = True
        root_real = os.path.normcase(os.path.realpath(root))
        if real == root_real or real.startswith(root_real + os.sep):
            return
    if not saw_root:
        raise SessionError("session root not found (provider roots missing)")
    raise SessionError("session path is outside provider roots: %s" % path)


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

def _scan_claude(home):
    root = os.path.join(home, ".claude", "projects")
    out = []
    for path in _walk_jsonl(root):
        meta = _claude_parse(path)
        if meta:
            out.append(meta)
    return out


def _claude_is_agent_file(path):
    name = os.path.basename(path)
    return name.startswith("agent-") or name == "journal.jsonl"


def _claude_parse(path):
    if _claude_is_agent_file(path):
        return None
    head, tail = _read_head_tail(path)
    session_id = project_dir = created_at = first_user = None
    for line in head:
        value = _try_json(line)
        if value is None:
            continue
        if session_id is None and isinstance(value.get("sessionId"), str):
            session_id = value["sessionId"].strip() or None
        if project_dir is None and isinstance(value.get("cwd"), str):
            project_dir = value["cwd"]
        if created_at is None:
            created_at = _ts_to_ms(value.get("timestamp"))
        if first_user is None:
            msg = value.get("message")
            is_user = (value.get("type") == "user"
                       or (isinstance(msg, dict) and msg.get("role") == "user"))
            if is_user and isinstance(msg, dict):
                trimmed = _extract_text(msg.get("content")).strip()
                # 跳过系统注入的命令说明与斜杠命令（/clear 等）
                if (trimmed
                        and "<local-command-caveat>" not in trimmed
                        and not trimmed.startswith("<command-name>")):
                    first_user = trimmed
        if session_id and project_dir and created_at and first_user:
            break

    last_active = summary = custom_title = None
    for line in reversed(tail):
        value = _try_json(line)
        if value is None:
            continue
        if last_active is None:
            last_active = _ts_to_ms(value.get("timestamp"))
        if custom_title is None and value.get("type") == "custom-title":
            ct = value.get("customTitle")
            if isinstance(ct, str) and ct.strip():
                custom_title = ct.strip()
        if summary is None:
            if value.get("isMeta") is True:
                continue
            msg = value.get("message")
            if isinstance(msg, dict):
                text = _extract_text(msg.get("content"))
                if text.strip():
                    summary = text
        if last_active is not None and summary is not None and custom_title is not None:
            break

    if session_id is None:
        session_id = os.path.splitext(os.path.basename(path))[0]
    if not session_id:
        return None
    if custom_title:
        title = truncate_summary(custom_title, TITLE_MAX_CHARS)
    elif first_user:
        title = truncate_summary(first_user, TITLE_MAX_CHARS)
    else:
        title = _path_basename(project_dir) if project_dir else None
    return _meta("claude", session_id, title,
                 truncate_summary(summary, SUMMARY_MAX_CHARS) if summary else None,
                 project_dir, created_at, last_active, path, "claude --resume %s")


def _load_claude(path):
    out = deque(maxlen=_MAX_MESSAGES)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            value = _try_json(line)
            if value is None:
                continue
            if value.get("isMeta") is True:
                continue
            msg = value.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            role = role if isinstance(role, str) and role else "unknown"
            # Claude 把 tool_result 包在 user 消息里，全部为 tool_result 时归为 tool
            content_items = msg.get("content")
            if (role == "user" and isinstance(content_items, list) and content_items
                    and all(isinstance(i, dict) and i.get("type") == "tool_result"
                            for i in content_items)):
                role = "tool"
            text = _cap(_extract_text(msg.get("content")))
            if not text.strip():
                continue
            out.append({"role": role, "content": text,
                        "ts": _ts_to_ms(value.get("timestamp"))})
    return list(out)


def _delete_claude(path, session_id):
    meta = _claude_parse(path)
    if meta is None:
        raise SessionError("failed to parse Claude session metadata: %s" % path)
    if meta["session_id"] != session_id:
        raise SessionError("Claude session ID mismatch: expected %s, found %s"
                           % (session_id, meta["session_id"]))
    # 同名 sidecar 目录（子代理 / 工具结果）一并删除
    stem = os.path.splitext(os.path.basename(path))[0]
    sidecar = os.path.join(os.path.dirname(path), stem)
    if os.path.isdir(sidecar) and not os.path.islink(sidecar):
        shutil.rmtree(sidecar)
    elif os.path.exists(sidecar):
        os.remove(sidecar)
    os.remove(path)


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

def _scan_codex(home):
    titles = _codex_thread_titles(home)
    out = []
    for root in provider_roots("codex", home):
        for path in _walk_jsonl(root):
            meta = _codex_parse(path, titles)
            if meta:
                out.append(meta)
    return out


def _codex_thread_titles(home):
    titles = {}
    base = os.path.join(home, ".codex")
    titles.update(_codex_index_titles(os.path.join(base, "session_index.jsonl")))
    for db_path in _codex_state_db_paths(home):
        titles.update(_codex_db_titles(db_path))
    return titles


def _codex_index_titles(path):
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                value = _try_json(line)
                if value is None:
                    continue
                sid = value.get("id")
                name = value.get("thread_name")
                if (isinstance(sid, str) and sid.strip()
                        and isinstance(name, str) and name.strip()):
                    out[sid.strip()] = name.strip()
    except OSError:
        return {}
    return out


def _codex_state_db_paths(home):
    """state_5.sqlite 的候选位置：配置目录 + sqlite_home/CODEX_SQLITE_HOME 覆盖。"""
    base = os.path.join(home, ".codex")
    paths = [os.path.join(base, CODEX_STATE_DB_FILENAME)]
    override = _codex_sqlite_home(base)
    if not override:
        override = (os.environ.get("CODEX_SQLITE_HOME") or "").strip() or None
    if override:
        candidate = os.path.join(_resolve_user_path(override, home),
                                 CODEX_STATE_DB_FILENAME)
        if candidate not in paths:
            paths.append(candidate)
    return paths


def _codex_sqlite_home(base):
    """config.toml 里的 sqlite_home 覆盖。轻量解析：只认 key = 'value' / "value"。"""
    try:
        with open(os.path.join(base, "config.toml"), "r",
                  encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _SQLITE_HOME_RE.match(line)
                if m:
                    value = m.group(1).strip()
                    return value or None
    except OSError:
        pass
    return None


def _resolve_user_path(raw, home):
    raw = raw.strip()
    if raw == "~":
        return home
    if raw.startswith("~/") or raw.startswith("~\\"):
        return os.path.join(home, raw[2:])
    return raw


def _codex_db_titles(db_path):
    """只读打开 Codex 的 threads 表取标题。

    Codex 运行时会写锁该库，busy timeout=2s 避免读操作立即失败；
    title 等于首条用户消息的行没有信息量，跳过（镜像 Codex 自身逻辑）。
    """
    if not os.path.exists(db_path):
        return {}
    try:
        uri = "file:" + pathname2url(os.path.abspath(db_path)) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            rows = conn.execute(
                "SELECT id, title FROM threads "
                "WHERE title <> '' "
                "AND (first_user_message IS NULL "
                "     OR TRIM(title) <> TRIM(first_user_message))").fetchall()
        finally:
            conn.close()
    except Exception:  # sqlite3.Error / 缺表 / 被锁 —— 标题缺失不算错误
        return {}
    out = {}
    for sid, title in rows:
        if (isinstance(sid, str) and sid.strip()
                and isinstance(title, str) and title.strip()):
            out[sid.strip()] = title.strip()
    return out


def _codex_title_candidate(text):
    trimmed = (text or "").strip()
    if (not trimmed
            or trimmed.startswith("# AGENTS.md")
            or trimmed.startswith("<environment_context>")):
        return None
    if trimmed.startswith(CODEX_IDE_CONTEXT_PREFIX):
        return _codex_ide_prompt(trimmed)
    return trimmed


def _codex_ide_prompt(text):
    """VSCode 注入的 IDE 上下文里挖真实提问：取最后一个 "my request for codex" 段。"""
    lines = text.replace("\r\n", "\n").split("\n")
    prompt = None
    for i, raw in enumerate(lines):
        trimmed = raw.strip()
        if not trimmed.startswith("#"):
            continue
        heading = trimmed.lstrip("#").strip()
        if not heading.lower().startswith(CODEX_REQUEST_MARKER):
            continue
        suffix = heading[len(CODEX_REQUEST_MARKER):].lstrip()
        if not suffix:
            inline = ""
        else:
            sep = suffix[0]
            if sep not in (":", "：", "-", "—"):
                continue  # 不是请求标题（可能是正文里的同级标题）
            inline = suffix.lstrip(" \t:：-—").strip()
        if inline:
            prompt = inline
        else:
            rest = "\n".join(lines[i + 1:]).strip()
            if rest:
                prompt = rest
    return prompt


def _codex_parse(path, titles):
    head, tail = _read_head_tail(path)
    session_id = project_dir = created_at = first_user = None
    for line in head:
        value = _try_json(line)
        if value is None:
            continue
        if created_at is None:
            created_at = _ts_to_ms(value.get("timestamp"))
        if value.get("type") == "session_meta":
            payload = value.get("payload")
            if isinstance(payload, dict):
                source = payload.get("source")
                if isinstance(source, dict) and "subagent" in source:
                    return None  # 子代理会话不展示
                if session_id is None and isinstance(payload.get("id"), str):
                    session_id = payload["id"].strip() or None
                if project_dir is None and isinstance(payload.get("cwd"), str):
                    project_dir = payload["cwd"]
                if created_at is None:
                    created_at = _ts_to_ms(payload.get("timestamp"))
        if first_user is None and value.get("type") == "response_item":
            payload = value.get("payload")
            if (isinstance(payload, dict)
                    and payload.get("type") == "message"
                    and payload.get("role") == "user"):
                cand = _codex_title_candidate(_extract_text(payload.get("content")))
                if cand:
                    first_user = cand
        if session_id and project_dir and created_at and first_user:
            break

    last_active = summary = None
    for line in reversed(tail):
        value = _try_json(line)
        if value is None:
            continue
        if last_active is None:
            last_active = _ts_to_ms(value.get("timestamp"))
        if summary is None and value.get("type") == "response_item":
            payload = value.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "message":
                text = _extract_text(payload.get("content"))
                if text.strip():
                    summary = text
        if last_active is not None and summary is not None:
            break

    if session_id is None:
        m = _UUID_RE.search(os.path.basename(path))
        if m:
            session_id = m.group(0)
    if not session_id:
        return None
    db_title = titles.get(session_id)
    if db_title:
        title = truncate_summary(db_title, TITLE_MAX_CHARS)
    elif first_user:
        title = truncate_summary(first_user, TITLE_MAX_CHARS)
    else:
        title = _path_basename(project_dir) if project_dir else None
    return _meta("codex", session_id, title,
                 truncate_summary(summary, SUMMARY_MAX_CHARS) if summary else None,
                 project_dir, created_at, last_active, path, "codex resume %s")


def _load_codex(path):
    out = deque(maxlen=_MAX_MESSAGES)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            value = _try_json(line)
            if value is None or value.get("type") != "response_item":
                continue
            payload = value.get("payload")
            if not isinstance(payload, dict):
                continue
            ptype = payload.get("type")
            ptype = ptype if isinstance(ptype, str) else ""
            if ptype == "message":
                role = payload.get("role")
                role = role if isinstance(role, str) and role else "unknown"
                text = _extract_text(payload.get("content"))
            elif ptype == "function_call":
                role = "assistant"
                name = payload.get("name")
                text = "[Tool: %s]" % (name if isinstance(name, str) and name
                                       else "unknown")
            elif ptype == "function_call_output":
                role = "tool"
                out_v = payload.get("output")
                text = out_v if isinstance(out_v, str) else ""
            else:
                continue
            text = _cap(text)
            if not text.strip():
                continue
            out.append({"role": role, "content": text,
                        "ts": _ts_to_ms(value.get("timestamp"))})
    return list(out)


def _delete_codex(path, session_id):
    meta = _codex_parse(path, {})
    if meta is None:
        raise SessionError("failed to parse Codex session metadata: %s" % path)
    if meta["session_id"] != session_id:
        raise SessionError("Codex session ID mismatch: expected %s, found %s"
                           % (session_id, meta["session_id"]))
    os.remove(path)


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

def _scan_gemini(home):
    tmp_root = os.path.join(home, ".gemini", "tmp")
    out = []
    if not os.path.isdir(tmp_root):
        return out
    for name in os.listdir(tmp_root):
        proj_path = os.path.join(tmp_root, name)
        chats = os.path.join(proj_path, "chats")
        if not os.path.isdir(chats):
            continue
        project_dir = None
        try:
            with open(os.path.join(proj_path, ".project_root"), "r",
                      encoding="utf-8", errors="replace") as fh:
                project_dir = fh.read().strip() or None
        except OSError:
            project_dir = None
        for fn in os.listdir(chats):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(chats, fn)
            meta = _gemini_parse(path)
            if meta:
                meta["project_dir"] = project_dir
                out.append(meta)
    return out


def _gemini_parse(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    session_id = value.get("sessionId")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    session_id = session_id.strip()
    created_at = _ts_to_ms(value.get("startTime"))
    last_active = _ts_to_ms(value.get("lastUpdated")) or created_at
    title = None
    msgs = value.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            # content 可能是纯字符串，也可能是 [{text:...}] 数组
            if (isinstance(m, dict) and m.get("type") == "user"):
                content = m.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = [i.get("text") for i in content
                             if isinstance(i, dict) and isinstance(i.get("text"), str)]
                    text = "\n".join(parts)
                else:
                    text = ""
                if text.strip():
                    title = truncate_summary(text, SUMMARY_MAX_CHARS)
                    break
    return _meta("gemini", session_id, title, title, None,
                 created_at, last_active, path, "gemini --resume %s")


def _load_gemini(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            value = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SessionError("failed to read session: %s" % exc)
    msgs = value.get("messages") if isinstance(value, dict) else None
    if not isinstance(msgs, list):
        raise SessionError("no messages array found")
    out = []
    for m in msgs[len(msgs) - _MAX_MESSAGES:]:
        if not isinstance(m, dict):
            continue
        mtype = m.get("type")
        if mtype == "gemini":
            role = "assistant"
        elif mtype == "user":
            role = "user"
        else:
            continue  # info / error / 未知类型跳过
        content = m.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = [i.get("text") for i in content
                     if isinstance(i, dict) and isinstance(i.get("text"), str)]
            text = "\n".join(parts)
        else:
            text = ""
        calls = m.get("toolCalls")
        if isinstance(calls, list):
            for call in calls:
                if isinstance(call, dict) and isinstance(call.get("name"), str):
                    if text:
                        text += "\n"
                    text += "[Tool: %s]" % call["name"]
        text = _cap(text)
        if not text.strip():
            continue
        out.append({"role": role, "content": text,
                    "ts": _ts_to_ms(m.get("timestamp"))})
    return out


def _delete_gemini(path, session_id):
    meta = _gemini_parse(path)
    if meta is None:
        raise SessionError("failed to parse Gemini session metadata: %s" % path)
    if meta["session_id"] != session_id:
        raise SessionError("Gemini session ID mismatch: expected %s, found %s"
                           % (session_id, meta["session_id"]))
    os.remove(path)


# ---------------------------------------------------------------------------
# 读取 / 删除的统一入口（含包含校验）
# ---------------------------------------------------------------------------

def load_messages(provider_id, source_path, home=None):
    provider_id = (provider_id or "").strip().lower()
    roots = provider_roots(provider_id, home)
    _ensure_under_roots(source_path, roots, "session source")
    path = os.path.abspath(source_path)
    if provider_id == "claude":
        return _load_claude(path)
    if provider_id == "codex":
        return _load_codex(path)
    return _load_gemini(path)


def delete_session(provider_id, session_id, source_path, home=None):
    """删除一个会话文件。路径越界、解析失败、ID 不匹配都抛 SessionError。"""
    provider_id = (provider_id or "").strip().lower()
    session_id = (session_id or "").strip()
    roots = provider_roots(provider_id, home)
    _ensure_under_roots(source_path, roots, "session source")
    path = os.path.abspath(source_path)
    if provider_id == "claude":
        _delete_claude(path, session_id)
    elif provider_id == "codex":
        _delete_codex(path, session_id)
    else:
        _delete_gemini(path, session_id)
