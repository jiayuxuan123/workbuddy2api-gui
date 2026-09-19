"""Verify client-config writing: the core of the provider-switching feature.

Points Claude Code, Codex CLI and Gemini CLI at this gateway by writing their
own config files, the way cc-switch does - so the user does not hand-edit JSON
or TOML.

Each case works in a scratch home so nothing here touches a real ~/.claude and
friends. All file access goes through ``scratch()``, which builds a path from a
fixed root plus plain names and refuses anything that resolves outside that
root, so a case cannot be pointed at a real file by accident.
"""

import json
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ROOT = pathlib.Path(tempfile.mkdtemp(prefix="wbprov_")).resolve()

failures = []


def check(name, ok, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


def scratch(*names):
    """Build a path under the scratch root, refusing traversal.

    Components are names, not paths: an absolute value or a parent reference
    is rejected, and the result must still resolve inside ROOT.
    """
    for name in names:
        text = str(name)
        if not text or text.startswith(("/", "\\")) or ":" in text:
            raise ValueError("refusing an absolute path component: %r" % text)
        if ".." in text.replace("\\", "/").split("/"):
            raise ValueError("refusing a parent reference: %r" % text)
    target = (ROOT.joinpath(*[str(n) for n in names])).resolve()
    if target != ROOT and ROOT not in target.parents:
        raise ValueError("path resolves outside the scratch root")
    return target


def fresh_home(tag):
    """A fresh scratch home directory for one case."""
    home = scratch("home_%s" % tag)
    home.mkdir(parents=True, exist_ok=True)
    return home


def main():
    import wb_providers as P

    print("=== the gateway endpoint resolves ===")
    endpoint = P.gateway_endpoint(port=8787)
    check("an explicit port is honoured", endpoint == "http://127.0.0.1:8787",
          endpoint)
    check("a default is available", bool(P.gateway_endpoint()))

    print()
    print("=== nothing configured: status is empty, not an error ===")
    base = fresh_home("empty")
    st = P.status(base=str(base))
    for cid in ("claude", "codex", "gemini"):
        info = st.get(cid) or {}
        check("%s reports not installed" % cid,
              info.get("installed") is False, str(info))
        check("%s reports not the gateway" % cid,
              info.get("is_gateway") is False)

    print()
    print("=== applying to Claude Code ===")
    base = fresh_home("claude")
    claude_dir = scratch("home_claude", ".claude")
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings = claude_dir / "settings.json"
    settings.write_text(
        json.dumps({"theme": "dark", "env": {"USER_OWN_KEY": "keep-me"}}),
        encoding="utf-8")

    result = P.apply("claude", port=8787, key="secret-1", base=str(base))
    check("apply reports the fields it wrote",
          "env.ANTHROPIC_BASE_URL" in result["changed"], str(result["changed"]))
    check("a backup path is returned", bool(result["backup"]))

    data = json.loads(settings.read_text(encoding="utf-8"))
    env = data.get("env") or {}
    check("base url written", env.get("ANTHROPIC_BASE_URL")
          == "http://127.0.0.1:8787", str(env.get("ANTHROPIC_BASE_URL")))
    check("auth token written", env.get("ANTHROPIC_AUTH_TOKEN") == "secret-1")
    check("user's own environment key preserved",
          env.get("USER_OWN_KEY") == "keep-me", str(env))
    check("user's other settings preserved", data.get("theme") == "dark")

    print()
    print("=== detection recognises a pointer back at us ===")
    cur = P.current_provider("claude", base=str(base), port=8787)
    check("is_gateway true", cur["is_gateway"] is True, str(cur))
    check("base_url reported", cur["base_url"] == "http://127.0.0.1:8787")
    check("has_key true", cur["has_key"] is True)

    print()
    print("=== a path suffix or trailing slash still counts ===")
    for url, label, expected in (
            ("http://127.0.0.1:8787/v1", "with a /v1 suffix", True),
            ("http://127.0.0.1:8787/", "with a trailing slash", True),
            ("https://api.other.com", "a different provider", False),
            ("http://127.0.0.1:9999", "the same host, another port", False)):
        settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": url}}),
                            encoding="utf-8")
        check(label, P.current_provider("claude", base=str(base), port=8787)
              ["is_gateway"] is expected)

    print()
    print("=== reverting restores the original file ===")
    settings.write_text(
        json.dumps({"theme": "dark", "env": {"USER_OWN_KEY": "keep-me"}}),
        encoding="utf-8")
    P.apply("claude", port=8787, key="secret-1", base=str(base))
    P.revert("claude", base=str(base))
    restored = json.loads(settings.read_text(encoding="utf-8"))
    check("the gateway's url is gone",
          (restored.get("env") or {}).get("ANTHROPIC_BASE_URL") is None,
          str(restored.get("env")))
    check("the user's own key survived the round trip",
          (restored.get("env") or {}).get("USER_OWN_KEY") == "keep-me",
          str(restored.get("env")))
    check("the user's theme survived", restored.get("theme") == "dark")

    print()
    print("=== applying to Codex CLI ===")
    base = fresh_home("codex")
    codex_dir = scratch("home_codex", ".codex")
    codex_dir.mkdir(parents=True, exist_ok=True)
    toml_path = codex_dir / "config.toml"
    toml_path.write_text('model = "gpt-5.6-sol"\n\n[history]\n'
                         'persistence = "save-all"\n', encoding="utf-8")

    P.apply("codex", port=8787, key="secret-2", base=str(base))
    toml = toml_path.read_text(encoding="utf-8")
    check("a provider table was added",
          "[model_providers.workbuddy2api]" in toml, toml[:160])
    check("the base url is in that table",
          'base_url = "http://127.0.0.1:8787"' in toml)
    check("model_provider selects it",
          'model_provider = "workbuddy2api"' in toml)

    # The decisive check: model_provider must be a root key, i.e. before the
    # first table header. Inside a table the client would never read it.
    lines = toml.splitlines()
    first_table = next((i for i, l in enumerate(lines)
                        if l.strip().startswith("[")), len(lines))
    mp_index = next((i for i, l in enumerate(lines)
                     if l.strip().startswith("model_provider")), -1)
    check("model_provider is a top-level key", 0 <= mp_index < first_table,
          "table starts at line %d, key at line %d" % (first_table, mp_index))
    check("the user's existing model is preserved",
          'model = "gpt-5.6-sol"' in toml)
    check("the user's existing table is preserved",
          "[history]" in toml and 'persistence = "save-all"' in toml)

    auth_path = codex_dir / "auth.json"
    check("auth.json written", auth_path.is_file())
    if auth_path.is_file():
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
        check("the key is stored for the provider",
              (auth.get("workbuddy2api") or {}).get("api_key") == "secret-2",
              str(auth))
    check("codex is detected as pointed at us",
          P.current_provider("codex", base=str(base), port=8787)["is_gateway"] is True)

    print()
    print("=== re-applying is idempotent ===")
    P.apply("codex", port=8787, key="secret-2", base=str(base))
    again = toml_path.read_text(encoding="utf-8")
    check("only one provider table exists",
          again.count("[model_providers.workbuddy2api]") == 1,
          "found %d" % again.count("[model_providers.workbuddy2api]"))
    check("only one model_provider line exists",
          again.count("model_provider = ") == 1,
          "found %d" % again.count("model_provider = "))

    print()
    print("=== applying to Gemini CLI ===")
    base = fresh_home("gemini")
    gemini_dir = scratch("home_gemini", ".gemini")
    gemini_dir.mkdir(parents=True, exist_ok=True)
    env_path = gemini_dir / ".env"
    env_path.write_text("# my settings\nGEMINI_MODEL=gemini-3.5-flash\n",
                        encoding="utf-8")

    P.apply("gemini", port=8787, key="secret-3", base=str(base))
    env_text = env_path.read_text(encoding="utf-8")
    check("base url written",
          "GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8787" in env_text, env_text)
    check("api key written", "GEMINI_API_KEY=secret-3" in env_text)
    check("the user's comment preserved", "# my settings" in env_text)
    check("the user's own variable preserved",
          "GEMINI_MODEL=gemini-3.5-flash" in env_text)
    check("gemini is detected as pointed at us",
          P.current_provider("gemini", base=str(base), port=8787)["is_gateway"] is True)

    print()
    print("=== a key is optional ===")
    base = fresh_home("nokey")
    scratch("home_nokey", ".claude").mkdir(parents=True, exist_ok=True)
    P.apply("claude", port=8787, key="", base=str(base))
    cur = P.current_provider("claude", base=str(base), port=8787)
    check("base url still written without a key",
          cur["base_url"] == "http://127.0.0.1:8787", str(cur))
    check("no key reported when none was set", cur["has_key"] is False)
    check("still recognised as the gateway", cur["is_gateway"] is True)

    print()
    print("=== a corrupt file is refused, not overwritten ===")
    base = fresh_home("broken")
    broken_dir = scratch("home_broken", ".claude")
    broken_dir.mkdir(parents=True, exist_ok=True)
    broken = broken_dir / "settings.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    try:
        P.apply("claude", port=8787, key="k", base=str(base))
        check("refuses to rewrite a corrupt file", False, "no exception raised")
    except P.ProviderError:
        check("refuses to rewrite a corrupt file", True)
    check("the corrupt file was left alone",
          broken.read_text(encoding="utf-8") == "{ this is not json")

    print()
    print("=== bad input is rejected ===")
    try:
        P.apply("not-a-client", base=str(fresh_home("bad")))
        check("unknown client rejected", False, "accepted")
    except P.ProviderError:
        check("unknown client rejected", True)
    try:
        P.revert("claude", base=str(fresh_home("nobackup")))
        check("revert without a backup rejected", False, "accepted")
    except P.ProviderError:
        check("revert without a backup rejected", True)

    print()
    print("=== the guard keeps writes inside the client folder ===")
    guard_home = fresh_home("guard")
    guarded = P._guarded("claude", base=str(guard_home))
    check("resolves inside the scratch root",
          guarded.startswith(str(guard_home)), guarded)
    try:
        P._guarded("..", base=str(guard_home))
        check("a traversal-shaped client id is refused", False, "accepted")
    except Exception:
        check("a traversal-shaped client id is refused", True)


try:
    main()
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print()
if failures:
    print("RESULT: %d FAILURE(S)" % len(failures))
    for name in failures:
        print("  - %s" % name)
    sys.exit(1)
print("RESULT: ALL PROVIDER CHECKS PASSED")
