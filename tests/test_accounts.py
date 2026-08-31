"""Multi-subscription account switching.

The invariants worth protecting, in order of how badly they bite:

1. A single-account install is untouched — `main` injects NO env, so the CLI resolves
   credentials exactly as before.
2. The active account is part of the live-client fingerprint. It rides in `env`, which the
   fingerprint deliberately ignores, so without an explicit field a switch would leave a
   connected subprocess quietly burning the OLD subscription.
3. A broken account degrades to `main` instead of taking every run down, and cannot be
   selected in the first place.
4. An extra account's identity is never read from the main account's state file.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def acct(tmp_path, monkeypatch):
    """accounts module bound to an isolated DATA dir + accounts root + fake ~/.claude."""
    import accounts as mod

    data = tmp_path / "data"
    data.mkdir()
    root = tmp_path / "accts"
    root.mkdir()
    home_claude = tmp_path / "home" / ".claude"
    (home_claude / "projects").mkdir(parents=True)
    (home_claude / "skills").mkdir()
    (home_claude / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-main",
                                      "subscriptionType": "max", "expiresAt": 1787288775067}})
    )

    monkeypatch.setenv("_CARDLOOP_DATA_DIR", str(data))
    monkeypatch.setenv("CLAUDE_ACCOUNTS_DIR", str(root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home_claude))
    monkeypatch.delenv("CLAUDE_CREDENTIALS_PATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return mod


def _login(mod, aid, token="sk-ant-oat01-second", email="second@example.com"):
    """Simulate `claude /login` inside that account's config dir."""
    cdir = mod.accounts_root() / aid
    (cdir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": token, "subscriptionType": "max",
                                      "expiresAt": 1787288775067}})
    )
    (cdir / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": email}}))


# ── 1. the single-account install must not change ──────────────────────────────────────────

def test_main_injects_no_env(acct):
    assert acct.active_id() == acct.MAIN_ID
    assert acct.env_overrides() == {}
    assert acct.env_overrides(acct.MAIN_ID) == {}


def test_usage_block_absent_until_second_account(acct):
    assert len(acct.list_accounts()) == 1


def test_main_creds_path_honours_env_override(acct, monkeypatch):
    monkeypatch.setenv("CLAUDE_CREDENTIALS_PATH", "/custom/creds.json")
    assert acct.creds_path("main") == "/custom/creds.json"


# ── 2. registering, scaffolding, switching ─────────────────────────────────────────────────

def test_scaffold_shares_projects_and_keeps_credentials_private(acct):
    cdir, linked = acct.scaffold("work")
    assert "projects" in linked
    assert (cdir / "projects").is_symlink()
    assert (cdir / "projects").resolve() == (acct.main_config_dir() / "projects").resolve()
    # The one thing that must NOT be shared.
    assert not (cdir / ".credentials.json").exists()
    assert oct(cdir.stat().st_mode)[-3:] == "700"


def test_scaffold_is_idempotent(acct):
    acct.scaffold("work")
    cdir, linked = acct.scaffold("work")
    assert linked == []  # already linked, nothing clobbered
    assert (cdir / "projects").is_symlink()


def test_switch_binds_runs_to_the_other_config_dir(acct):
    cdir, _ = acct.scaffold("work")
    acct.register("work", "Work", str(cdir))
    _login(acct, "work")
    ok, reason = acct.set_active("work")
    assert ok, reason
    assert acct.active_id() == "work"
    assert acct.env_overrides() == {"CLAUDE_CONFIG_DIR": str(cdir)}
    assert acct.creds_path("work") == str(cdir / ".credentials.json")


def test_unregister_falls_back_to_main(acct):
    cdir, _ = acct.scaffold("work")
    acct.register("work", "Work", str(cdir))
    _login(acct, "work")
    acct.set_active("work")
    acct.unregister("work")
    assert acct.active_id() == acct.MAIN_ID
    assert acct.env_overrides() == {}
    # The files themselves are left alone — losing a login by removing a row would be rude.
    assert (cdir / ".credentials.json").exists()


def test_cannot_register_or_remove_main(acct):
    assert acct.register("main", "x", str(acct.main_config_dir()))[0] is False
    assert acct.unregister("main")[0] is False


def test_rejects_dodgy_ids(acct):
    acct.accounts_root().joinpath("ok").mkdir()
    assert acct.register("../escape", "x", str(acct.accounts_root() / "ok"))[0] is False
    assert acct.register("has space", "x", str(acct.accounts_root() / "ok"))[0] is False


# ── 3. broken accounts must fail safe, not fail loud ───────────────────────────────────────

def test_cannot_activate_an_account_without_login(acct):
    cdir, _ = acct.scaffold("work")
    acct.register("work", "Work", str(cdir))
    ok, reason = acct.set_active("work")
    assert not ok and "not logged in" in reason
    assert acct.active_id() == acct.MAIN_ID


def test_active_account_that_breaks_later_degrades_to_main(acct):
    cdir, _ = acct.scaffold("work")
    acct.register("work", "Work", str(cdir))
    _login(acct, "work")
    acct.set_active("work")
    (cdir / ".credentials.json").unlink()          # e.g. operator wiped it
    assert acct.active_id() == acct.MAIN_ID        # runs keep working…
    assert acct.env_overrides() == {}              # …on the main subscription
    assert acct.inspect("work")["ok"] is False     # …and the UI shows why


def test_corrupt_state_file_degrades_to_main(acct):
    (Path(os.environ["_CARDLOOP_DATA_DIR"]) / "accounts.json").write_text("{not json")
    assert acct.active_id() == acct.MAIN_ID
    assert acct.env_overrides() == {}


def test_missing_shared_projects_link_is_reported(acct):
    cdir = acct.accounts_root() / "manual"
    cdir.mkdir()
    (cdir / "projects").mkdir()                    # a real dir, NOT shared with ~/.claude
    acct.register("manual", "Manual", str(cdir))
    _login(acct, "manual")
    info = acct.inspect("manual")
    assert info["ok"] is True
    assert info["shared_ok"] is False and "projects" in info["shared_broken"]


# ── 4. identity is never borrowed from the other account ───────────────────────────────────

def test_extra_account_never_shows_the_main_accounts_email(acct):
    (Path.home() / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "main@example.com"}}))
    cdir, _ = acct.scaffold("work")
    acct.register("work", "Work", str(cdir))
    # Logged in, but the CLI has not written its state file yet — no email is known.
    (cdir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "t", "subscriptionType": "max"}}))
    assert acct.inspect("work")["email"] == ""
    assert acct.inspect("main")["email"] == "main@example.com"


def test_inspect_never_leaks_a_token(acct):
    cdir, _ = acct.scaffold("work")
    acct.register("work", "Work", str(cdir))
    _login(acct, "work", token="sk-ant-oat01-SECRET")
    assert "SECRET" not in json.dumps(acct.inspect("work"))


# ── 5. the live-client fingerprint must move with the account ──────────────────────────────

def test_account_changes_the_live_client_fingerprint():
    """Without this, switching accounts would leave a connected subprocess on the old one."""
    import engine

    class _Opts:
        cwd = "/tmp/x"
        model = "opus"
        permission_mode = "bypassPermissions"
        setting_sources = ["user"]
        disallowed_tools = []
        skills = None
        plugins = []
        system_prompt = {"type": "preset", "preset": "claude_code"}
        settings = None

    base = engine._compute_fingerprint(_Opts(), stable_append_hash="h", effort="high",
                                       memory_mode="auto", account="main")
    other = engine._compute_fingerprint(_Opts(), stable_append_hash="h", effort="high",
                                        memory_mode="auto", account="work")
    assert base != other
    # Same account → stable (no spurious reconnect churn).
    assert base == engine._compute_fingerprint(_Opts(), stable_append_hash="h", effort="high",
                                               memory_mode="auto", account="main")


# ── 6. per-project pinning ─────────────────────────────────────────────────────────────────

def test_project_override_wins_over_global(acct):
    cdir, _ = acct.scaffold("work")
    acct.register("work", "Work", str(cdir))
    _login(acct, "work")
    # Global stays on main; the project is pinned to work.
    assert acct.resolve("work") == "work"
    assert acct.env_overrides(acct.resolve("work")) == {"CLAUDE_CONFIG_DIR": str(cdir)}
    # No override → follow the global choice.
    assert acct.resolve(None) == acct.MAIN_ID
    assert acct.resolve("") == acct.MAIN_ID


def test_project_can_pin_main_while_global_is_elsewhere(acct):
    cdir, _ = acct.scaffold("work")
    acct.register("work", "Work", str(cdir))
    _login(acct, "work")
    acct.set_active("work")
    assert acct.resolve(None) == "work"          # everything else follows the global switch
    assert acct.resolve("main") == "main"        # …but this project stays on main
    assert acct.env_overrides(acct.resolve("main")) == {}


def test_broken_project_override_degrades_to_global_not_to_a_dead_run(acct):
    cdir, _ = acct.scaffold("work")
    acct.register("work", "Work", str(cdir))
    _login(acct, "work")
    acct.set_active("work")
    # The project points at an account that was never registered (renamed/removed).
    assert acct.resolve("ghost") == "work"
    assert acct.env_overrides(acct.resolve("ghost")) == {"CLAUDE_CONFIG_DIR": str(cdir)}


# ── 5. MCP config is mirrored from main into every extra account ───────────────────────────
# Regression 2026-08-30: work profile had `mcpServers: {}` while ~/.claude.json held eleven
# servers, so every cockpit session on that account started with NO mail/webmail/ticktick/canva
# and the failure was invisible — the tools simply were not in the prompt.

def _seed_main_mcp(mod, servers, oauth=None):
    home_claude = mod.main_config_dir()
    (home_claude / ".claude.json").write_text(json.dumps({"mcpServers": servers}))
    creds = json.loads((home_claude / ".credentials.json").read_text())
    if oauth is not None:
        creds["mcpOAuth"] = oauth
    (home_claude / ".credentials.json").write_text(json.dumps(creds))


def test_env_overrides_mirrors_mcp_servers_into_the_account(acct):
    _seed_main_mcp(acct, {"mail": {"type": "stdio", "command": "/x/py"}},
                   oauth={"canva|abc": {"accessToken": "t"}})
    acct.scaffold("second")
    _login(acct, "second")
    acct.register("second", "Second", str(acct.accounts_root() / "second"))

    acct.env_overrides("second")

    cdir = acct.accounts_root() / "second"
    assert json.loads((cdir / ".claude.json").read_text())["mcpServers"] == {
        "mail": {"type": "stdio", "command": "/x/py"}
    }
    assert json.loads((cdir / ".credentials.json").read_text())["mcpOAuth"] == {
        "canva|abc": {"accessToken": "t"}
    }


def test_sync_never_touches_the_account_identity(acct):
    """The whole point of separate config dirs: `main`'s tokens must not leak into account #2."""
    _seed_main_mcp(acct, {"mail": {"command": "/x/py"}}, oauth={"canva|abc": {"accessToken": "t"}})
    acct.scaffold("second")
    _login(acct, "second", token="sk-ant-oat01-second", email="second@example.com")
    acct.register("second", "Second", str(acct.accounts_root() / "second"))

    acct.env_overrides("second")

    cdir = acct.accounts_root() / "second"
    creds = json.loads((cdir / ".credentials.json").read_text())
    state = json.loads((cdir / ".claude.json").read_text())
    assert creds["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-second"
    assert state["oauthAccount"]["emailAddress"] == "second@example.com"


def test_mcp_servers_mirror_drops_a_server_deleted_in_main(acct):
    _seed_main_mcp(acct, {"mail": {"command": "/x/py"}, "old": {"command": "/gone"}})
    acct.scaffold("second")
    _login(acct, "second")
    acct.register("second", "Second", str(acct.accounts_root() / "second"))
    acct.env_overrides("second")

    _seed_main_mcp(acct, {"mail": {"command": "/x/py"}})
    acct.env_overrides("second")

    cdir = acct.accounts_root() / "second"
    assert set(json.loads((cdir / ".claude.json").read_text())["mcpServers"]) == {"mail"}


def test_mcp_oauth_is_a_union_so_local_logins_survive(acct):
    """A token this account obtained itself must not be wiped by the mirror."""
    _seed_main_mcp(acct, {"mail": {"command": "/x/py"}}, oauth={"canva|abc": {"accessToken": "t"}})
    acct.scaffold("second")
    _login(acct, "second")
    acct.register("second", "Second", str(acct.accounts_root() / "second"))
    cdir = acct.accounts_root() / "second"
    creds = json.loads((cdir / ".credentials.json").read_text())
    creds["mcpOAuth"] = {"ticktick|zzz": {"accessToken": "local"}}
    (cdir / ".credentials.json").write_text(json.dumps(creds))

    acct.env_overrides("second")

    got = json.loads((cdir / ".credentials.json").read_text())["mcpOAuth"]
    assert got == {"ticktick|zzz": {"accessToken": "local"}, "canva|abc": {"accessToken": "t"}}


def test_sync_is_idempotent_and_does_not_rewrite_a_file_in_sync(acct):
    """Called on every turn — a no-op sync must not churn the file (concurrent CLI writers)."""
    _seed_main_mcp(acct, {"mail": {"command": "/x/py"}})
    acct.scaffold("second")
    _login(acct, "second")
    acct.register("second", "Second", str(acct.accounts_root() / "second"))
    acct.env_overrides("second")

    assert acct.sync_shared_config(acct.accounts_root() / "second") == []
