"""Verify MCP management: one library, projected into three different formats.

The interesting part of this feature is not storing a server - it is that the
three clients disagree about how to write it down. These cases pin the
translation that CC Switch performs (`claude_mcp.rs`, `gemini_mcp.rs`,
`mcp/codex.rs`):

* Claude lives in ``~/.claude.json`` - a different file from its provider
  settings - and on Windows wraps ``npx`` in ``cmd /c``;
* Codex writes ``[mcp_servers.<id>]`` in TOML with ``headers`` renamed to
  ``http_headers`` and ``env`` as its own sub-table;
* Gemini has no ``type`` field: the transport is whatever key is present
  (``httpUrl`` / ``url`` / ``command``), and it always carries a ``timeout``.

Everything happens in a scratch tree; every path is a module constant written
out in full from plain names, and file access calls read_text/write_text on
those constants directly.
"""

import json
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="wbmcp_"))

CLAUDE_JSON = SANDBOX / ".claude.json"
CLAUDE_DIR = SANDBOX / ".claude" / "keep"
CODEX_CONFIG = SANDBOX / ".codex" / "config.toml"
GEMINI_SETTINGS = SANDBOX / ".gemini" / "settings.json"

LIB = SANDBOX / "mcp-lib.json"
LIB2 = SANDBOX / "mcp-lib2.json"

failures = []


def check(name, ok, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


def main():
    import wb_mcp as M

    stdio = {
        "id": "context7",
        "name": "Context7",
        "server": {"type": "stdio", "command": "npx",
                   "args": ["-y", "@upstash/context7-mcp"],
                   "env": {"FOO": "bar"}},
        "apps": {"claude": True, "codex": True, "gemini": True},
    }
    http = {
        "id": "remote",
        "name": "Remote",
        "server": {"type": "http", "url": "https://mcp.example.com/mcp",
                   "headers": {"Authorization": "Bearer sk-1"}},
        "apps": {"claude": True, "codex": True, "gemini": True},
    }

    print("=== a client that is not installed is skipped ===")
    check("nothing is installed in an empty home",
          M.should_sync("claude", base=str(SANDBOX)) is False)
    check("writing to it returns no path",
          M.write_to_app("claude", stdio, base=str(SANDBOX)) == "")
    check("and no file is created", not CLAUDE_JSON.is_file())
    M.upsert_server(stdio, path=str(LIB), base=str(SANDBOX))
    M.upsert_server(http, path=str(LIB), base=str(SANDBOX))
    check("the library records both servers",
          [s["id"] for s in M.list_servers(path=str(LIB))] == ["context7", "remote"],
          str([s["id"] for s in M.list_servers(path=str(LIB))]))

    print()
    print("=== Claude: ~/.claude.json, cmd /c wrapping on Windows ===")
    CLAUDE_JSON.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_JSON.write_text(json.dumps({"numStartups": 3, "theme": "dark"}),
                           encoding="utf-8")
    M.set_app_enabled("context7", "claude", True,
                      path=str(LIB), base=str(SANDBOX))
    M.set_app_enabled("remote", "claude", True, path=str(LIB), base=str(SANDBOX))
    data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    servers = data.get("mcpServers") or {}
    check("the file is the one beside .claude, not inside it",
          M.app_file("claude", base=str(SANDBOX)) == str(CLAUDE_JSON),
          M.app_file("claude", base=str(SANDBOX)))
    check("the server is under mcpServers", "context7" in servers, str(servers))
    check("unrelated root keys survive", data.get("theme") == "dark"
          and data.get("numStartups") == 3, str(data))
    entry = servers.get("context7") or {}
    if sys.platform.startswith("win"):
        check("npx is wrapped in cmd /c",
              entry.get("command") == "cmd"
              and entry.get("args", [])[:2] == ["/c", "npx"], str(entry))
    else:
        check("npx is left alone off Windows",
              entry.get("command") == "npx", str(entry))
    check("env is kept", entry.get("env") == {"FOO": "bar"}, str(entry))
    check("a remote server keeps its headers",
          (servers.get("remote") or {}).get("headers")
          == {"Authorization": "Bearer sk-1"}, str(servers.get("remote")))

    print()
    print("=== Codex: TOML tables, headers renamed to http_headers ===")
    CODEX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CODEX_CONFIG.write_text('model = "gpt-5"\n', encoding="utf-8")
    M.set_app_enabled("context7", "codex", True, path=str(LIB), base=str(SANDBOX))
    M.set_app_enabled("remote", "codex", True, path=str(LIB), base=str(SANDBOX))
    body = CODEX_CONFIG.read_text(encoding="utf-8")
    check("the top-level key survives", 'model = "gpt-5"' in body, body)
    check("a stdio server gets its own table",
          "[mcp_servers.context7]" in body, body)
    check("args are a TOML array",
          'args = ["-y", "@upstash/context7-mcp"]' in body, body)
    check("env becomes its own sub-table",
          "[mcp_servers.context7.env]" in body and 'FOO = "bar"' in body, body)
    check("a remote server gets a table too",
          "[mcp_servers.remote]" in body, body)
    check("headers are renamed to http_headers",
          "[mcp_servers.remote.http_headers]" in body
          and 'Authorization = "Bearer sk-1"' in body, body)
    check("the plain name 'headers' is not used as a key",
          not any(line.strip().startswith("headers =") for line in body.splitlines()),
          body)
    check("the wire protocol is not misread as a transport",
          'type = "http"' in body, body)

    print()
    print("=== Gemini: no type field, transport implied by the key ===")
    GEMINI_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    GEMINI_SETTINGS.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    M.set_app_enabled("context7", "gemini", True, path=str(LIB), base=str(SANDBOX))
    M.set_app_enabled("remote", "gemini", True, path=str(LIB), base=str(SANDBOX))
    gdata = json.loads(GEMINI_SETTINGS.read_text(encoding="utf-8"))
    gservers = gdata.get("mcpServers") or {}
    check("unrelated keys survive", gdata.get("theme") == "dark", str(gdata))
    check("a stdio server uses command",
          (gservers.get("context7") or {}).get("command") == "npx",
          str(gservers.get("context7")))
    check("Gemini does not wrap in cmd /c",
          (gservers.get("context7") or {}).get("command") == "npx",
          str(gservers.get("context7")))
    check("an http server uses httpUrl",
          (gservers.get("remote") or {}).get("httpUrl")
          == "https://mcp.example.com/mcp", str(gservers.get("remote")))
    check("no type field is written",
          "type" not in (gservers.get("context7") or {})
          and "type" not in (gservers.get("remote") or {}), str(gservers))
    check("a timeout is always written",
          (gservers.get("context7") or {}).get("timeout")
          == M.GEMINI_DEFAULT_TIMEOUT_MS, str(gservers.get("context7")))
    check("no url key on the http entry",
          "url" not in (gservers.get("remote") or {}), str(gservers.get("remote")))

    print()
    print("=== disabling removes the key from the file ===")
    M.set_app_enabled("context7", "claude", False, path=str(LIB), base=str(SANDBOX))
    after = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    check("the key is gone from Claude",
          "context7" not in (after.get("mcpServers") or {}),
          str(after.get("mcpServers")))
    check("the other server is untouched",
          "remote" in (after.get("mcpServers") or {}), str(after.get("mcpServers")))
    check("unrelated root keys still survive", after.get("theme") == "dark")

    M.set_app_enabled("context7", "codex", False, path=str(LIB), base=str(SANDBOX))
    cbody = CODEX_CONFIG.read_text(encoding="utf-8")
    check("the table is gone from Codex",
          "[mcp_servers.context7]" not in cbody, cbody)
    check("its env sub-table is gone too",
          "[mcp_servers.context7.env]" not in cbody, cbody)
    check("the other table is untouched",
          "[mcp_servers.remote]" in cbody, cbody)
    check("the top-level key survives the removal",
          'model = "gpt-5"' in cbody, cbody)

    print()
    print("=== reading an app's file back round-trips the shape ===")
    live = M.read_from_app("gemini", base=str(SANDBOX))
    check("gemini http is read back as http",
          (live.get("remote") or {}).get("type") == "http", str(live))
    live_codex = M.read_from_app("codex", base=str(SANDBOX))
    check("codex http is read back as http",
          (live_codex.get("remote") or {}).get("type") == "http", str(live_codex))
    check("codex headers are mapped back from http_headers",
          (live_codex.get("remote") or {}).get("headers")
          == {"Authorization": "Bearer sk-1"}, str(live_codex))
    check("the timeout is not treated as part of the spec",
          "timeout" not in (live.get("remote") or {}), str(live))

    print()
    print("=== deleting removes it everywhere ===")
    M.delete_server("remote", path=str(LIB), base=str(SANDBOX))
    check("gone from the library",
          [s["id"] for s in M.list_servers(path=str(LIB))] == ["context7"],
          str([s["id"] for s in M.list_servers(path=str(LIB))]))
    check("gone from the Codex file",
          "[mcp_servers.remote]" not in CODEX_CONFIG.read_text(encoding="utf-8"))
    check("gone from the Gemini file",
          "remote" not in (json.loads(GEMINI_SETTINGS.read_text(encoding="utf-8"))
                           .get("mcpServers") or {}))

    print()
    print("=== stale entries in a client file are cleaned up ===")
    CODEX_CONFIG.write_text(
        'model = "gpt-5"\n\n'
        "[mcp_servers.leftover]\n"
        'type = "stdio"\n'
        'command = "node"\n',
        encoding="utf-8")
    M.sync_app("codex", path=str(LIB), base=str(SANDBOX))
    check("an unknown server is removed",
          "[mcp_servers.leftover]" not in CODEX_CONFIG.read_text(encoding="utf-8"),
          CODEX_CONFIG.read_text(encoding="utf-8"))
    # context7 was switched off for Codex earlier, so a sync must not bring it
    # back: disabled means absent, not merely unchecked.
    check("a server disabled for this app is not re-added",
          "[mcp_servers.context7]" not in CODEX_CONFIG.read_text(encoding="utf-8"),
          CODEX_CONFIG.read_text(encoding="utf-8"))
    M.set_app_enabled("context7", "codex", True, path=str(LIB), base=str(SANDBOX))
    check("re-enabling writes it back",
          "[mcp_servers.context7]" in CODEX_CONFIG.read_text(encoding="utf-8"),
          CODEX_CONFIG.read_text(encoding="utf-8"))

    print()
    print("=== a server with no command or url is rejected ===")
    try:
        M.upsert_server({"id": "bad", "name": "Bad", "server": {"type": "stdio"}},
                        path=str(LIB2))
        check("an empty stdio server is refused", False, "it was accepted")
    except M.McpError:
        check("an empty stdio server is refused", True)
    try:
        M.upsert_server({"id": "bad2", "name": "Bad",
                         "server": {"type": "http"}}, path=str(LIB2))
        check("an empty http server is refused", False, "it was accepted")
    except M.McpError:
        check("an empty http server is refused", True)

    print()
    print("=== the library is per-file and independent ===")
    check("library 2 holds nothing",
          M.list_servers(path=str(LIB2)) == [], str(M.list_servers(path=str(LIB2))))

    print()
    if failures:
        print("FAILED: %d case(s)" % len(failures))
        for name in failures:
            print("  - %s" % name)
    else:
        print("all cases passed")
    shutil.rmtree(str(SANDBOX), ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
