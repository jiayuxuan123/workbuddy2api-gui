"""Verify client-config writing: the core of the provider-switching feature.

Points Claude Code, Codex CLI and Gemini CLI at this gateway by writing their
own config files, the way cc-switch does - so the user does not hand-edit JSON
or TOML.

The cases here encode behaviour read out of cc-switch's own source
(`services/provider/live.rs`, `codex_config.rs`, `gemini_config.rs`), because
those are the details a reimplementation gets wrong:

* Codex reads the key from `experimental_bearer_token` inside the provider
  table - not from a made-up shape in auth.json;
* Gemini keeps using OAuth unless `settings.json` says
  `security.auth.selectedType = "gemini-api-key"`, no matter what .env holds;
* switching away and back restores the user's file byte for byte.

Everything happens in a scratch home under one temp root, so nothing here
touches a real ~/.claude and friends. Every path below is a module constant
written out in full from plain names; file access calls read_text/write_text on
those constants directly.
"""

import json
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="wbprov_"))

HOME_CLAUDE = SANDBOX / "home-claude"
HOME_CODEX = SANDBOX / "home-codex"
HOME_GEMINI = SANDBOX / "home-gemini"
HOME_GEMINI_STALE = SANDBOX / "home-gemini-stale"
HOME_PREVIEW = SANDBOX / "home-preview"
HOME_DETECT = SANDBOX / "home-detect"
HOME_DAMAGED = SANDBOX / "home-damaged"
HOME_REVERT = SANDBOX / "home-revert"
HOME_EMPTY = SANDBOX / "home-empty"

CLAUDE_SETTINGS = SANDBOX / "home-claude" / ".claude" / "settings.json"
CODEX_CONFIG = SANDBOX / "home-codex" / ".codex" / "config.toml"
CODEX_AUTH = SANDBOX / "home-codex" / ".codex" / "auth.json"
GEMINI_ENV = SANDBOX / "home-gemini" / ".gemini" / ".env"
GEMINI_SETTINGS = SANDBOX / "home-gemini" / ".gemini" / "settings.json"

LIB_CLAUDE = SANDBOX / "lib-claude.json"
LIB_CODEX = SANDBOX / "lib-codex.json"
LIB_GEMINI = SANDBOX / "lib-gemini.json"
LIB_OPS = SANDBOX / "lib-ops.json"
LIB_PORT = SANDBOX / "lib-port.json"
LIB_DMG = SANDBOX / "lib-dmg.json"
LIB_REVERT = SANDBOX / "lib-revert.json"
LIB_BLEED_A = SANDBOX / "lib-bleed-a.json"
LIB_BLEED_B = SANDBOX / "lib-bleed-b.json"
LIB_MISSING = SANDBOX / "lib-missing.json"

failures = []


def check(name, ok, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


def main():
    import wb_providers as P

    print("=== the gateway endpoint resolves ===")
    check("an explicit port is honoured",
          P.gateway_endpoint(port=8799) == "http://127.0.0.1:8799",
          P.gateway_endpoint(port=8799))
    check("a default is available", bool(P.gateway_endpoint()))

    print()
    print("=== nothing configured: status is empty, not an error ===")
    HOME_EMPTY.mkdir(parents=True, exist_ok=True)
    status = P.status(base=str(HOME_EMPTY), port=8799)
    for client in ("claude", "codex", "gemini"):
        entry = status[client]
        check("%s reports not installed" % client,
              entry.get("installed") is False, str(entry.get("installed")))
        check("%s reports not the gateway" % client,
              entry.get("is_gateway") is False, str(entry.get("is_gateway")))

    print()
    print("=== applying to Claude Code ===")
    original = {
        "env": {"FOO": "bar"},
        "theme": "dark",
        "permissions": {"allow": ["Bash"]},
    }
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(json.dumps(original, indent=2), encoding="utf-8")
    base = str(HOME_CLAUDE)
    lib = str(LIB_CLAUDE)

    P.apply("claude", port=8799, key="wb-key", base=base, path=lib)
    data = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    check("the base URL is written", data["env"].get("ANTHROPIC_BASE_URL")
          == "http://127.0.0.1:8799", str(data["env"]))
    check("the token is written", data["env"].get("ANTHROPIC_AUTH_TOKEN") == "wb-key")
    check("unrelated settings survive", data.get("theme") == "dark"
          and data.get("permissions") == {"allow": ["Bash"]}, str(data))
    check("a backup was taken", P.has_backup("claude", base=base))
    check("status now reports the gateway",
          P.current_provider("claude", base=base, port=8799)["is_gateway"] is True)
    check("the original state was captured as a provider",
          any(p["id"].startswith("captured")
              for p in P.list_providers("claude", path=lib)),
          str([p["id"] for p in P.list_providers("claude", path=lib)]))

    print()
    print("=== switching Claude Code back to the captured provider ===")
    captured = [p for p in P.list_providers("claude", path=lib)
                if p["id"].startswith("captured")][0]
    P.switch_to("claude", captured["id"], base=base, path=lib)
    restored = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    check("the user's original config is back", restored == original, str(restored))
    check("the gateway is no longer reported",
          P.current_provider("claude", base=base, port=8799)["is_gateway"] is False)

    print()
    print("=== applying to Codex CLI ===")
    original_toml = (
        'model = "gpt-5"\n'
        'approval_policy = "never"\n'
        "\n"
        "# a comment the user cares about\n"
        "[model_providers.openai]\n"
        'name = "OpenAI"\n'
        'base_url = "https://api.openai.com/v1"\n'
    )
    CODEX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CODEX_CONFIG.write_text(original_toml, encoding="utf-8")
    base = str(HOME_CODEX)
    lib = str(LIB_CODEX)

    P.apply("codex", port=8799, key="wb-key", base=base, path=lib)
    body = CODEX_CONFIG.read_text(encoding="utf-8")
    check("model_provider points at the gateway",
          P._toml_value(body, "model_provider") == "workbuddy2api",
          P._toml_value(body, "model_provider"))
    check("the provider table is created",
          "[model_providers.workbuddy2api]" in body)
    check("the key is a bearer token inside the table",
          P._toml_value(P._toml_section(body, "model_providers.workbuddy2api"),
                        "experimental_bearer_token") == "wb-key")
    check("the table declares the responses wire API",
          P._toml_value(P._toml_section(body, "model_providers.workbuddy2api"),
                        "wire_api") == "responses")
    check("the user's comment survives", "# a comment the user cares about" in body)
    check("the user's other provider is untouched",
          "[model_providers.openai]" in body
          and P._toml_value(P._toml_section(body, "model_providers.openai"),
                            "base_url") == "https://api.openai.com/v1")
    check("auth.json was not invented", not CODEX_AUTH.is_file())
    check("model_provider is a top-level key, not inside a table",
          body.index("model_provider = ") < body.index("[model_providers.openai]"),
          body[:200])
    check("codex reports the gateway",
          P.current_provider("codex", base=base, port=8799)["is_gateway"] is True)

    print()
    print("=== switching Codex to another provider and back ===")
    P.upsert_provider("codex", {
        "id": "other", "name": "Other",
        "settings_config": {"provider_id": "other", "name": "Other",
                            "base_url": "https://example.com/v1",
                            "bearer_token": "sk-x"},
    }, path=lib)
    P.switch_to("codex", "other", base=base, path=lib)
    swapped = CODEX_CONFIG.read_text(encoding="utf-8")
    check("the other provider's URL is written",
          "https://example.com/v1" in swapped, swapped[:200])
    check("the other provider's key is written",
          'experimental_bearer_token = "sk-x"' in swapped)
    check("the gateway table is still present",
          "[model_providers.workbuddy2api]" in swapped)

    captured = [p for p in P.list_providers("codex", path=lib)
                if p["id"].startswith("captured")][0]
    P.switch_to("codex", captured["id"], base=base, path=lib)
    check("the original file is restored byte for byte",
          CODEX_CONFIG.read_text(encoding="utf-8") == original_toml,
          repr(CODEX_CONFIG.read_text(encoding="utf-8")))

    print()
    print("=== applying to Gemini CLI ===")
    GEMINI_ENV.parent.mkdir(parents=True, exist_ok=True)
    GEMINI_ENV.write_text("EXISTING=1\n", encoding="utf-8")
    GEMINI_SETTINGS.write_text(
        json.dumps({"mcpServers": {"x": {"command": "npx"}},
                    "theme": "dark"}, indent=2), encoding="utf-8")
    base = str(HOME_GEMINI)
    lib = str(LIB_GEMINI)

    result = P.apply("gemini", port=8799, key="wb-key", base=base, path=lib)
    env_text = GEMINI_ENV.read_text(encoding="utf-8")
    check("both files are written",
          len(result["written"]) == 2, str(result["written"]))
    check("the base URL is written",
          P._env_value(env_text, "GOOGLE_GEMINI_BASE_URL") == "http://127.0.0.1:8799",
          env_text)
    check("the key is written", P._env_value(env_text, "GEMINI_API_KEY") == "wb-key")
    check("unrelated env keys survive", "EXISTING=1" in env_text, env_text)
    check("env keys are written in sorted order",
          env_text.splitlines() == sorted(env_text.splitlines()), repr(env_text))
    check("env has no trailing newline", not env_text.endswith("\n"), repr(env_text))

    gsettings = json.loads(GEMINI_SETTINGS.read_text(encoding="utf-8"))
    check("the auth type is stamped as gemini-api-key",
          gsettings.get("security", {}).get("auth", {}).get("selectedType")
          == "gemini-api-key", str(gsettings.get("security")))
    check("mcpServers survive the settings merge",
          gsettings.get("mcpServers") == {"x": {"command": "npx"}}, str(gsettings))
    check("the theme survives the settings merge", gsettings.get("theme") == "dark")
    check("a completed switch is not flagged",
          P.current_provider("gemini", base=base, port=8799)
          .get("needs_auth_stamp") is False)

    print()
    print("=== a base URL without the auth stamp is flagged ===")
    stale_env = SANDBOX / "home-gemini-stale" / ".gemini" / ".env"
    stale_env.parent.mkdir(parents=True, exist_ok=True)
    stale_env.write_text("GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8799\n",
                         encoding="utf-8")
    (SANDBOX / "home-gemini-stale" / ".gemini" / "settings.json").write_text(
        "{}", encoding="utf-8")
    stale = P.current_provider("gemini", base=str(HOME_GEMINI_STALE), port=8799)
    check("needs_auth_stamp is true", stale.get("needs_auth_stamp") is True, str(stale))

    print()
    print("=== preview does not write anything ===")
    preview_file = SANDBOX / "home-preview" / ".claude" / "settings.json"
    preview_file.parent.mkdir(parents=True, exist_ok=True)
    preview_file.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://x.example"}}),
        encoding="utf-8")
    before = preview_file.read_text(encoding="utf-8")
    info = P.preview("claude", port=8799, key="k", base=str(HOME_PREVIEW))
    check("preview reports the current URL",
          info["before_base_url"] == "https://x.example", str(info))
    check("preview reports the target URL",
          info["after_base_url"] == "http://127.0.0.1:8799", str(info))
    check("preview lists the files it would touch",
          any(str(preview_file) == f for f in info["written_files"]),
          str(info["written_files"]))
    check("preview left the file alone",
          preview_file.read_text(encoding="utf-8") == before)

    print()
    print("=== a client already on the gateway is recognised ===")
    detect_file = SANDBOX / "home-detect" / ".claude" / "settings.json"
    detect_file.parent.mkdir(parents=True, exist_ok=True)
    # A trailing slash must still count as pointing at us.
    detect_file.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8799/"}}),
        encoding="utf-8")
    check("a trailing slash still counts",
          P.current_provider("claude", base=str(HOME_DETECT),
                             port=8799)["is_gateway"] is True)
    detect_file.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9999"}}),
        encoding="utf-8")
    check("a different port does not count",
          P.current_provider("claude", base=str(HOME_DETECT),
                             port=8799)["is_gateway"] is False)

    print()
    print("=== a damaged config is replaced, not propagated ===")
    damaged_env = SANDBOX / "home-damaged" / ".gemini" / ".env"
    damaged_env.parent.mkdir(parents=True, exist_ok=True)
    damaged_env.write_text("GOOGLE_GEMINI_BASE_URL=http://x\n", encoding="utf-8")
    damaged_settings = SANDBOX / "home-damaged" / ".gemini" / "settings.json"
    damaged_settings.write_text("{not json at all", encoding="utf-8")
    P.apply("gemini", port=8799, key="k", base=str(HOME_DAMAGED),
            path=str(LIB_DMG))
    gs = json.loads(damaged_settings.read_text(encoding="utf-8"))
    check("a broken settings.json is replaced by valid JSON",
          isinstance(gs, dict) and "security" in gs, str(gs))

    print()
    print("=== revert restores from the backup ===")
    revert_file = SANDBOX / "home-revert" / ".claude" / "settings.json"
    revert_file.parent.mkdir(parents=True, exist_ok=True)
    revert_file.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://keep.example"}}),
        encoding="utf-8")
    P.apply("claude", port=8799, key="k", base=str(HOME_REVERT),
            path=str(LIB_REVERT))
    P.revert("claude", base=str(HOME_REVERT))
    check("the original is back",
          json.loads(revert_file.read_text(encoding="utf-8"))
          ["env"]["ANTHROPIC_BASE_URL"] == "https://keep.example",
          revert_file.read_text(encoding="utf-8"))

    print()
    print("=== provider library operations ===")
    lib = str(LIB_OPS)
    P.upsert_provider("claude", {"id": "a", "name": "A",
                                 "settings_config":
                                     {"env": {"ANTHROPIC_BASE_URL": "https://a"}}},
                      path=lib)
    P.upsert_provider("claude", {"id": "b", "name": "B",
                                 "settings_config":
                                     {"env": {"ANTHROPIC_BASE_URL": "https://b"}}},
                      path=lib)
    check("providers are listed in order",
          [p["id"] for p in P.list_providers("claude", path=lib)] == ["a", "b"],
          str([p["id"] for p in P.list_providers("claude", path=lib)]))
    P.upsert_provider("claude", {"id": "a", "name": "A2",
                                 "settings_config":
                                     {"env": {"ANTHROPIC_BASE_URL": "https://a2"}}},
                      path=lib)
    items = P.list_providers("claude", path=lib)
    check("an upsert keeps the position", [p["id"] for p in items] == ["a", "b"])
    check("an upsert replaces the name",
          [p for p in items if p["id"] == "a"][0]["name"] == "A2")
    P.delete_provider("claude", "a", path=lib)
    check("delete removes the entry",
          [p["id"] for p in P.list_providers("claude", path=lib)] == ["b"])
    check("a missing library loads as empty",
          P.list_providers("codex", path=str(LIB_MISSING)) == [])

    print()
    print("=== two libraries do not bleed into each other ===")
    P.upsert_provider("claude", {"id": "onlyA", "name": "A",
                                 "settings_config": {"env": {}}},
                      path=str(LIB_BLEED_A))
    check("library B is untouched by a write to A",
          P.list_providers("claude", path=str(LIB_BLEED_B)) == [],
          str(P.list_providers("claude", path=str(LIB_BLEED_B))))

    print()
    print("=== the gateway provider tracks the live port ===")
    lib = str(LIB_PORT)
    P.ensure_gateway("claude", port=8791, key="k1", path=lib)
    first = [p for p in P.list_providers("claude", path=lib)
             if p["id"] == P.GATEWAY_ID][0]
    check("the first port is written",
          first["settings_config"]["env"]["ANTHROPIC_BASE_URL"]
          == "http://127.0.0.1:8791", str(first["settings_config"]))
    P.ensure_gateway("claude", port=8792, key="k1", path=lib)
    second = [p for p in P.list_providers("claude", path=lib)
              if p["id"] == P.GATEWAY_ID][0]
    check("re-ensuring refreshes the port",
          second["settings_config"]["env"]["ANTHROPIC_BASE_URL"]
          == "http://127.0.0.1:8792", str(second["settings_config"]))
    check("re-ensuring does not duplicate the entry",
          len([p for p in P.list_providers("claude", path=lib)
               if p["id"] == P.GATEWAY_ID]) == 1)

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
