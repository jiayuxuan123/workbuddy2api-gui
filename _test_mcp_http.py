"""End-to-end check of the MCP management endpoints over real HTTP.

Starts an actual gateway and issues real requests, because the question is
what the panel experiences, not what the handler returns in isolation.

Covers the endpoint surface added alongside the circuit-breaker wiring:

  GET  /mcp                library + per-app view          (panel gated)
  POST /mcp/upsert         add/replace one server          (panel gated)
  POST /mcp/set            enable a server for one app     (panel gated)
  POST /mcp/delete         remove a server everywhere      (panel gated)
  POST /mcp/sync           re-project the library          (panel gated)
  GET  /health/breakers    circuit-breaker snapshot        (panel gated)
  POST /providers/apply    regression: payload must be read (was UnboundLocalError)

The user's home directory is redirected into a sandbox by patching
os.path.expanduser, which wb_mcp and wb_providers resolve at call time -
nothing here touches a real ~/.claude or friends.
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ROOT = os.path.realpath(tempfile.mkdtemp(prefix="wbmcphttp_"))
os.environ["WB_DATA_DIR"] = ROOT

failures = []


def check(name, ok, detail=""):
    print("  %-56s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _request(port, method, path, token=None, body=None, timeout=6.0):
    """Loopback request by hand: literal host, no redirects."""
    headers = ["%s %s HTTP/1.1" % (method, path), "Host: 127.0.0.1:%d" % port,
               "Accept: application/json", "Connection: close"]
    payload = b""
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers.append("Content-Type: application/json")
        headers.append("Content-Length: %d" % len(payload))
    if token:
        headers.append("X-Panel-Token: %s" % token)
    request = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + payload

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(("127.0.0.1", port))
        sock.sendall(request)
        chunks = []
        while True:
            block = sock.recv(8192)
            if not block:
                break
            chunks.append(block)
    except OSError:
        return None, None
    finally:
        sock.close()
    raw = b"".join(chunks)
    split = raw.find(b"\r\n\r\n")
    if split == -1:
        return None, None
    try:
        status = int(raw[:split].split(b" ")[1])
    except Exception:
        return None, None
    body_raw = raw[split + 4:]
    try:
        parsed = json.loads(body_raw.decode("utf-8")) if body_raw else None
    except Exception:
        parsed = None
    return status, parsed


def get(port, path, token=None):
    return _request(port, "GET", path, token=token)


def post(port, path, body=None, token=None):
    return _request(port, "POST", path, token=token, body=body if body is not None else {})


import wb_gateway   # noqa: E402  (after WB_DATA_DIR is set)
import wb_proxy     # noqa: E402
import wb_health    # noqa: E402

# Redirect the home directory BEFORE any request: wb_mcp / wb_providers call
# os.path.expanduser at request time, never at import time.
HOME = os.path.join(ROOT, "home")
os.makedirs(os.path.join(HOME, ".claude"), exist_ok=True)
os.makedirs(os.path.join(HOME, ".codex"), exist_ok=True)
os.makedirs(os.path.join(HOME, ".gemini"), exist_ok=True)
_real_expanduser = os.path.expanduser
os.path.expanduser = lambda p: HOME

CLAUDE_JSON = os.path.join(HOME, ".claude.json")
GEMINI_SETTINGS = os.path.join(HOME, ".gemini", "settings.json")
CODEX_TOML = os.path.join(HOME, ".codex", "config.toml")

TOKEN = wb_proxy.PANEL.create()

gw = wb_gateway.Gateway()
gw.prefs.update({"lan": False, "require_local_key": False, "port": free_port()})
ok, msg = gw.start()
check("gateway started", ok, msg)
PORT = gw.prefs["port"]
time.sleep(0.4)

try:
    print()
    print("=== A. panel gating ===")
    st, _ = get(PORT, "/mcp")
    check("GET /mcp without token is 401", st == 401, "status=%s" % st)
    st, _ = get(PORT, "/health/breakers")
    check("GET /health/breakers without token is 401", st == 401, "status=%s" % st)
    st, _ = post(PORT, "/mcp/upsert", {})
    check("POST /mcp/upsert without token is 401", st == 401, "status=%s" % st)
    st, _ = post(PORT, "/providers/apply", {"client": "claude"})
    check("POST /providers/apply without token is 401", st == 401, "status=%s" % st)
    st, body = get(PORT, "/health")
    check("plain /health stays public", st == 200, "status=%s" % st)

    print()
    print("=== B. GET /mcp and /health/breakers ===")
    st, body = get(PORT, "/mcp", token=TOKEN)
    check("GET /mcp with token is 200", st == 200, "status=%s" % st)
    check("response has servers list", isinstance((body or {}).get("servers"), list))
    apps = (body or {}).get("apps") or {}
    check("response covers all three apps",
          sorted(apps.keys()) == ["claude", "codex", "gemini"], str(sorted(apps.keys())))

    # Seed one failure so the snapshot has something to show.
    wb_health.ACCOUNTS.get("acc-x").record_failure(status=429, error="test trip")
    st, body = get(PORT, "/health/breakers", token=TOKEN)
    check("GET /health/breakers with token is 200", st == 200, "status=%s" % st)
    keys = [b.get("key") for b in (body or {}).get("breakers", [])]
    check("snapshot names the tripped account", "acc-x" in keys, str(keys))
    entry = next((b for b in (body or {}).get("breakers", []) if b.get("key") == "acc-x"), {})
    check("snapshot carries last_status", entry.get("last_status") == 429,
          str(entry.get("last_status")))

    print()
    print("=== C. /mcp/upsert + /mcp/set projection ===")
    st, body = post(PORT, "/mcp/upsert", {"server": {"id": "weather"}}, token=TOKEN)
    check("upsert without command/url is 400", st == 400, "status=%s" % st)
    # The upsert payload is the library record itself: the same shape GET /mcp
    # returns, so the panel can round-trip an edited entry verbatim.
    st, body = post(PORT, "/mcp/upsert",
                    {"server": {"id": "weather", "name": "Weather",
                                "server": {"command": "npx", "args": ["-y", "mcp-weather"]}}},
                    token=TOKEN)
    check("valid upsert is 200", st == 200, "status=%s %s" % (st, body))
    st, body = get(PORT, "/mcp", token=TOKEN)
    names = [s.get("id") for s in (body or {}).get("servers", [])]
    check("library lists the new server", "weather" in names, str(names))

    st, body = post(PORT, "/mcp/set", {"id": "weather", "app": "claude", "enabled": True},
                    token=TOKEN)
    check("set enabled for claude is 200", st == 200, "status=%s %s" % (st, body))
    with open(CLAUDE_JSON, encoding="utf-8") as fh:
        live = json.load(fh)
    check("claude file gained the server", "weather" in (live.get("mcpServers") or {}),
          str(live.get("mcpServers")))
    check("claude entry was cmd-wrapped on Windows",
          str((live.get("mcpServers") or {}).get("weather", {}).get("command", "")).lower()
          in ("cmd", "npx"))

    st, body = post(PORT, "/mcp/set", {"id": "weather", "app": "claude", "enabled": False},
                    token=TOKEN)
    check("set disabled for claude is 200", st == 200, "status=%s" % st)
    with open(CLAUDE_JSON, encoding="utf-8") as fh:
        live = json.load(fh)
    check("claude file lost the server", "weather" not in (live.get("mcpServers") or {}),
          str(live.get("mcpServers")))

    st, body = post(PORT, "/mcp/set", {"id": "weather", "app": "nosuch", "enabled": True},
                    token=TOKEN)
    check("unknown app is 400", st == 400, "status=%s" % st)
    st, body = post(PORT, "/mcp/delete", {"id": "nope"}, token=TOKEN)
    check("deleting an unknown server is 400", st == 400, "status=%s" % st)

    print()
    print("=== D. /mcp/sync projects to installed clients ===")
    post(PORT, "/mcp/set", {"id": "weather", "app": "gemini", "enabled": True}, token=TOKEN)
    post(PORT, "/mcp/set", {"id": "weather", "app": "codex", "enabled": True}, token=TOKEN)
    st, body = post(PORT, "/mcp/sync", {}, token=TOKEN)
    check("sync is 200", st == 200, "status=%s" % st)
    report = (body or {}).get("report") or {}
    check("gemini was synced, not skipped",
          report.get("gemini", {}).get("skipped") is False, str(report.get("gemini")))
    with open(GEMINI_SETTINGS, encoding="utf-8") as fh:
        g = json.load(fh)
    check("gemini file holds the server with a timeout",
          "weather" in (g.get("mcpServers") or {})
          and "timeout" in (g.get("mcpServers") or {}).get("weather", {}),
          str(g.get("mcpServers")))
    with open(CODEX_TOML, encoding="utf-8") as fh:
        toml_text = fh.read()
    check("codex file holds the [mcp_servers] table",
          "[mcp_servers.weather]" in toml_text, toml_text[:120])

    print()
    print("=== E. /mcp/delete removes it everywhere ===")
    st, body = post(PORT, "/mcp/delete", {"id": "weather"}, token=TOKEN)
    check("delete is 200", st == 200, "status=%s" % st)
    with open(GEMINI_SETTINGS, encoding="utf-8") as fh:
        g = json.load(fh)
    check("gemini file lost the server", "weather" not in (g.get("mcpServers") or {}),
          str(g.get("mcpServers")))

    print()
    print("=== F. regression: /providers/apply reads its body ===")
    # revert() can only restore a file the first apply() backed up, which
    # requires the live config to exist beforehand - like a real machine.
    settings = os.path.join(HOME, ".claude", "settings.json")
    with open(settings, "w", encoding="utf-8") as fh:
        json.dump({"env": {"FOO": "bar"}, "theme": "dark"}, fh)
    st, body = post(PORT, "/providers/apply", {"client": "claude"}, token=TOKEN)
    check("apply is 200 (was UnboundLocalError)", st == 200,
          "status=%s body=%s" % (st, body))
    with open(settings, encoding="utf-8") as fh:
        cfg = json.load(fh)
    text = json.dumps(cfg)
    check("claude settings point at the gateway", "127.0.0.1" in text, text[:160])
    st, body = post(PORT, "/providers/revert", {"client": "claude"}, token=TOKEN)
    check("revert is 200", st == 200, "status=%s" % st)
    with open(settings, encoding="utf-8") as fh:
        cfg = json.load(fh)
    check("revert restored the original config",
          cfg.get("env", {}).get("FOO") == "bar", json.dumps(cfg)[:120])
    st, body = post(PORT, "/providers/apply", {"client": "nosuch"}, token=TOKEN)
    check("unknown client is 400", st == 400, "status=%s" % st)

finally:
    gw.stop()
    os.path.expanduser = _real_expanduser

shutil.rmtree(ROOT, ignore_errors=True)

print()
if failures:
    print("RESULT: %d FAILURE(S)" % len(failures))
    for name in failures:
        print("  - %s" % name)
    sys.exit(1)
print("RESULT: ALL MCP HTTP CHECKS PASSED")
