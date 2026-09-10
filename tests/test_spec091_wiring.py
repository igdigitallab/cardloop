"""
Tests for spec-091 Phase 1 — engine + HTTP wiring of the declarative role registry
(`roles.py`, already covered separately by tests/test_spec091_roles.py).

Covers CHECKLIST.md sections C (engine wiring), E (HTTP API) and I (safety/regressions).
Every test name embeds the checklist line it proves.

Nothing here touches the real ~/.claude-ops/roles — CARDLOOP_ROLES_DIR is monkeypatched to a
tmp_path per test (roles.global_dir() reads the env var live), and the project tier uses its
own tmp_path cwd. Engine-level tests capture ClaudeAgentOptions via a FakeClient, following
the existing pattern in tests/test_sdk_options.py and tests/test_spec017_orchestrator.py.
"""
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk import AgentDefinition

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import bot
import engine
import roles as R
import webapp as _webapp

# N4/I1m fix (A2-audit.md): `engine.DEFAULT_AGENTS[name].model` reads EXECUTOR_MODEL/
# RESEARCHER_MODEL/QUICK_MODEL at import time — with one of those set, the registry-compiled
# roster (which reads the role FILES' fixed ids, unaffected by env) stops matching
# DEFAULT_AGENTS' `model` field, and that is NOT a code bug. Compare against this literal,
# env-independent baseline instead wherever a test asserts model equality against the registry.
_DEFAULT_MODEL_BY_ROLE = {
    "executor": "claude-sonnet-5",
    "researcher": "claude-sonnet-5",
    "skeptic": "claude-sonnet-5",
    "quick": "haiku",
}


# ─────────────────────────── shared engine-side fixtures/helpers ───────────────────────────

@pytest.fixture()
def isolated_dirs(tmp_path, monkeypatch):
    """Isolate global_dir() and give each test its own project cwd, mirroring
    tests/test_spec091_roles.py's fixture of the same shape."""
    global_d = tmp_path / "global-roles"
    proj_cwd = tmp_path / "proj"
    proj_cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CARDLOOP_ROLES_DIR", str(global_d))
    return str(proj_cwd), str(global_d)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _FakeClient:
    """Captures the ClaudeAgentOptions a run_engine call would hand the SDK. Same shape as
    the FakeClient in tests/test_sdk_options.py / tests/test_spec017_orchestrator.py."""
    captured: "object" = None

    def __init__(self, options):
        _FakeClient.captured = options

    async def query(self, prompt):
        pass

    async def receive_response(self):
        return
        yield  # pragma: no cover - makes this an async generator

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


async def _run_engine_capturing(**kwargs):
    """Runs bot.run_engine to completion with ClaudeSDKClient faked out, returns the
    captured ClaudeAgentOptions."""
    with patch.object(engine, "ClaudeSDKClient", _FakeClient), \
         patch.object(engine, "running", {}), \
         patch.object(engine, "audit", lambda *a: None):
        async for _ in bot.run_engine(**kwargs):
            pass
    return _FakeClient.captured


# ─────────────────────────── C. Engine wiring ───────────────────────────

@pytest.mark.asyncio
async def test_c1_explicit_agents_kwarg_beats_registry(isolated_dirs, tmp_path):
    """C1: explicit `agents` kwarg wins even when project role files exist."""
    cwd, _ = isolated_dirs
    _write(Path(cwd) / ".claude-ops" / "roles" / "executor.md",
           "---\nname: executor\ndescription: custom executor. use this when x\n---\nCustom body.\n")
    custom_agents = {
        "only_agent": AgentDefinition(description="d", prompt="p", model="haiku",
                                       permissionMode="bypassPermissions")
    }
    opts = await _run_engine_capturing(
        project_name="t", cwd=cwd, prompt="hi", session_key="c1:t", model="sonnet",
        agents=custom_agents,
    )
    assert opts.agents == custom_agents


@pytest.mark.asyncio
async def test_c2_zero_role_files_is_byte_identical_to_default_agents(isolated_dirs, monkeypatch):
    """C2: with zero role files present, the roster equals DEFAULT_AGENTS exactly (identity).
    F1 fix (A1-audit.md): the fallback now branches on `list_roles_report` (every file across
    all three tiers), not on the merged/enabled dict — so simulating "zero role files" must
    mock the walk itself, not just `load_roles`. Mocking only `load_roles` (the old way) would
    leave the real `roles/builtin/*.md` visible to `list_roles_report` and take the registry
    branch instead — see test_i1b below for exactly that case (roster ends up `{}`, not
    DEFAULT_AGENTS, when files exist but are all disabled)."""
    cwd, _ = isolated_dirs
    monkeypatch.setattr(R, "list_roles_report", lambda cwd: ([], []))
    opts = await _run_engine_capturing(
        project_name="t", cwd=cwd, prompt="hi", session_key="c2:t", model="sonnet",
    )
    assert opts.agents is engine.DEFAULT_AGENTS


@pytest.mark.asyncio
async def test_c3_shipped_builtins_match_default_agents_field_by_field(isolated_dirs):
    """C3: with the shipped builtins present (real roles/builtin/, no project/global
    overrides), executor/researcher/skeptic/quick match DEFAULT_AGENTS field by field."""
    cwd, _ = isolated_dirs
    opts = await _run_engine_capturing(
        project_name="t", cwd=cwd, prompt="hi", session_key="c3:t", model="sonnet",
    )
    for name in ("executor", "researcher", "skeptic", "quick"):
        got = opts.agents[name]
        want = engine.DEFAULT_AGENTS[name]
        assert got.prompt == want.prompt, name
        assert got.tools == want.tools, name
        assert got.disallowedTools == want.disallowedTools, name
        assert got.effort == want.effort, name
        assert got.maxTurns == want.maxTurns, name
        # F6 fix (A1-audit.md): the EXECUTOR_MODEL/RESEARCHER_MODEL/QUICK_MODEL preservation
        # loop that used to live in engine.py's roster-resolution block is gone — the role
        # FILES are the only source of truth for a role's model now (it never reached
        # `skeptic` anyway). N4/I1m fix (A2-audit.md): compare against the literal baseline,
        # not DEFAULT_AGENTS.model — DEFAULT_AGENTS itself still reads those three env vars as
        # the broken-install fallback's own knob, unrelated to the registry, and asserting
        # equality against it made this test env-sensitive for no code-bug reason.
        assert got.model == _DEFAULT_MODEL_BY_ROLE[name], name


@pytest.mark.asyncio
async def test_c4_plan_mode_still_drops_the_custom_roster(isolated_dirs):
    """C4: plan-mode turns drop the roster entirely, unchanged, even with builtins present."""
    cwd, _ = isolated_dirs
    opts = await _run_engine_capturing(
        project_name="t", cwd=cwd, prompt="hi", session_key="c4:t", model="sonnet",
        plan_mode=True,
    )
    assert opts.agents is None


def test_c5_build_agents_kwargs_emits_model_overrides_not_a_full_roster():
    """C5/F4 fix (A1-audit.md): _build_agents_kwargs used to rebuild a FULL
    {name: AgentDefinition} dict from DEFAULT_AGENTS and hand it to run_engine as the
    explicit `agents` kwarg — which beats the role registry outright per C1's own
    precedence, so setting just ONE of these three legacy fields silently hid every
    custom/builtin role for that project (A1-audit.md F4: "operator types opus into
    'Executor model' → every custom role and all 7 new builtins disappear"). It must now
    emit only the override strings; run_engine merges them into whichever roster it
    resolves — see test_c5_model_override_merges_into_registry_preserving_other_fields."""
    kwargs = engine._build_agents_kwargs({"executor_model": "opus", "quick_model": "haiku"})
    assert "agents" not in kwargs
    assert kwargs == {"agent_model_overrides": {"executor": "opus", "quick": "haiku"}}


def test_c5_build_agents_kwargs_no_override_set_is_empty():
    assert engine._build_agents_kwargs({"conductor_prompt": True}).get("agent_model_overrides") is None


@pytest.mark.asyncio
async def test_c5_model_override_merges_into_registry_preserving_other_fields(isolated_dirs):
    """C5/F4 regression, through the REAL merge point (engine.py's roster-resolution block),
    not _build_agents_kwargs (which no longer touches AgentDefinition fields at all — the
    7-of-13-field reconstruction this test used to guard against is now structurally
    unreachable there). A role file's skills/memory/mcpServers, and the REST of the
    registry, must survive a legacy executor_model override."""
    cwd, _ = isolated_dirs
    _write(Path(cwd) / ".claude-ops" / "roles" / "executor.md",
           "---\nname: executor\ndescription: use this when x\nskills: [code-review]\n"
           "memory: project\nmcpServers: [some-server]\n---\nCustom executor body.\n")
    opts = await _run_engine_capturing(
        project_name="t", cwd=cwd, prompt="hi", session_key="c5b:t", model="sonnet",
        agent_model_overrides={"executor": "claude-opus-5"},
    )
    executor = opts.agents["executor"]
    assert executor.model == "claude-opus-5"
    assert executor.skills == ["code-review"]
    assert executor.memory == "project"
    assert executor.mcpServers == ["some-server"]
    assert executor.prompt == "Custom executor body."
    # The whole point of F4: the override no longer hides every OTHER role.
    assert "reviewer-logic" in opts.agents
    assert "skeptic" in opts.agents


@pytest.mark.asyncio
async def test_c6_subagent_files_prompt_still_excludes_quick(isolated_dirs, monkeypatch):
    """C6: SUBAGENT_FILES_PROMPT is appended to every registry-sourced role except `quick`,
    exactly as it was for DEFAULT_AGENTS — role-name-based, not roster-index-based."""
    cwd, _ = isolated_dirs
    opts = await _run_engine_capturing(
        project_name="t", cwd=cwd, prompt="hi", session_key="c6:t", model="sonnet",
        env={"COPS_MEDIA_DIR": "/tmp/media"},
    )
    assert engine.SUBAGENT_FILES_PROMPT in opts.agents["executor"].prompt
    assert engine.SUBAGENT_FILES_PROMPT not in opts.agents["quick"].prompt


@pytest.mark.asyncio
async def test_c7_registry_fingerprint_changes_the_stable_append_hash(isolated_dirs):
    """C7: registry_fingerprint is part of _stable_append_pieces, so editing a role changes
    the hash handed to the live-client reuse gate (_get_or_create_live_client's
    stable_append_hash kwarg) — proving a reused live client cannot serve a stale roster."""
    cwd, _ = isolated_dirs
    captured: list = []

    async def _capture_and_fallthrough(*args, **kwargs):
        captured.append(kwargs.get("stable_append_hash"))
        return None  # fall through to the non-persistent path, same as PERSISTENT_CLIENT=0

    with patch.object(engine, "_get_or_create_live_client", AsyncMock(side_effect=_capture_and_fallthrough)):
        await _run_engine_capturing(
            project_name="t", cwd=cwd, prompt="hi", session_key="c7a:t", model="sonnet",
        )
        _write(Path(cwd) / ".claude-ops" / "roles" / "executor.md",
               "---\nname: executor\ndescription: edited executor. use this when x\n---\nEdited body.\n")
        await _run_engine_capturing(
            project_name="t", cwd=cwd, prompt="hi", session_key="c7b:t", model="sonnet",
        )
    assert len(captured) == 2
    assert captured[0] != captured[1]


@pytest.mark.asyncio
async def test_c8_workflow_and_task_share_one_compiled_dict(isolated_dirs):
    """C8: the Workflow tool's agentType resolves against the same compiled dict as the
    Task tool — there is exactly one `agents=` kwarg on ClaudeAgentOptions, and it IS the
    dict compiled from the registry (identity, not a copy)."""
    cwd, _ = isolated_dirs
    compiled_seen = {}
    real_compile = R.compile_agents

    def _spy_compile(roles_dict):
        result = real_compile(roles_dict)
        compiled_seen["dict"] = result
        return result

    with patch.object(R, "compile_agents", side_effect=_spy_compile):
        opts = await _run_engine_capturing(
            project_name="t", cwd=cwd, prompt="hi", session_key="c8:t", model="sonnet",
        )
    assert opts.agents is compiled_seen["dict"]


# ─────────────────────────── E. HTTP API ───────────────────────────

@pytest.fixture
def project_dir(tmp_path):
    pdir = tmp_path / "myproject"
    pdir.mkdir()
    return pdir


@pytest.fixture
def fake_ctx_with_project(tmp_path, project_dir):
    from webapp import _derive_token

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    password = "testpass"
    ctx = {
        "topics": {
            "1001:42": {
                "project": "myproject",
                "cwd": str(project_dir),
                "model": "sonnet",
            }
        },
        "sessions": {},
        "running": {},
        "password": password,
        "DATA": data_dir,
        "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    return ctx


@pytest.fixture
def roles_app(fake_ctx_with_project, tmp_path, monkeypatch):
    from aiohttp import web

    # Isolate the global tier so the real dev machine's ~/.claude-ops/roles never leaks in.
    monkeypatch.setenv("CARDLOOP_ROLES_DIR", str(tmp_path / "no-such-global"))

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = fake_ctx_with_project
    app.router.add_get("/api/health", _webapp.api_health)
    app.router.add_get("/api/projects/{id}/roles", _webapp.api_project_roles)
    app.router.add_get("/api/projects/{id}/roles/{name}", _webapp.api_project_role_get)
    app.router.add_post("/api/projects/{id}/roles/{name}", _webapp.api_project_role_write)
    app.router.add_delete("/api/projects/{id}/roles/{name}", _webapp.api_project_role_delete)
    app.router.add_post("/api/projects/{id}/roles/{name}/enabled", _webapp.api_project_role_enabled)
    return app


def _auth_headers(ctx):
    return {"Cookie": f"cops_auth={ctx['_auth_token']}"}


_VALID_ROLE_BODY = (
    "---\nname: helper\ndescription: use this when you need a second pair of eyes\n---\n"
    "You are a helper sub-agent.\n"
)


async def test_e1_all_five_endpoints_registered_and_respond(aiohttp_client, roles_app, fake_ctx_with_project):
    """E1: all five endpoints exist and are registered."""
    client = await aiohttp_client(roles_app)
    h = _auth_headers(fake_ctx_with_project)

    r_list = await client.get("/api/projects/myproject/roles", headers=h)
    assert r_list.status == 200

    r_post = await client.post("/api/projects/myproject/roles/helper",
                                json={"scope": "project", "content": _VALID_ROLE_BODY}, headers=h)
    assert r_post.status == 200

    r_get = await client.get("/api/projects/myproject/roles/helper?scope=project", headers=h)
    assert r_get.status == 200

    r_enabled = await client.post("/api/projects/myproject/roles/helper/enabled",
                                   json={"scope": "project", "enabled": False}, headers=h)
    assert r_enabled.status == 200

    r_del = await client.delete("/api/projects/myproject/roles/helper?scope=project", headers=h)
    assert r_del.status == 200


async def test_e2_unauthorized_without_cookie_for_every_endpoint(aiohttp_client, roles_app):
    """E2: auth behaves exactly like the memory endpoints — no cookie, no access."""
    client = await aiohttp_client(roles_app)
    assert (await client.get("/api/projects/myproject/roles")).status == 401
    assert (await client.get("/api/projects/myproject/roles/helper?scope=project")).status == 401
    assert (await client.post("/api/projects/myproject/roles/helper",
                               json={"scope": "project", "content": _VALID_ROLE_BODY})).status == 401
    assert (await client.delete("/api/projects/myproject/roles/helper?scope=project")).status == 401
    assert (await client.post("/api/projects/myproject/roles/helper/enabled",
                               json={"scope": "project", "enabled": True})).status == 401


async def test_e2b_authorized_with_cookie_succeeds(aiohttp_client, roles_app, fake_ctx_with_project):
    """E2 (positive half): the same requests succeed with the session cookie."""
    client = await aiohttp_client(roles_app)
    h = _auth_headers(fake_ctx_with_project)
    resp = await client.get("/api/projects/myproject/roles", headers=h)
    assert resp.status == 200


async def test_e3_scope_builtin_rejected_on_write_and_delete(aiohttp_client, roles_app, fake_ctx_with_project):
    """E3: scope=builtin on write or delete returns 400 with the documented message."""
    client = await aiohttp_client(roles_app)
    h = _auth_headers(fake_ctx_with_project)
    expected = "builtin roles are read-only — save it to the project or global scope instead"

    r_write = await client.post("/api/projects/myproject/roles/executor",
                                 json={"scope": "builtin", "content": _VALID_ROLE_BODY}, headers=h)
    assert r_write.status == 400
    assert (await r_write.json())["error"] == expected

    r_del = await client.delete("/api/projects/myproject/roles/executor?scope=builtin", headers=h)
    assert r_del.status == 400
    assert (await r_del.json())["error"] == expected


async def test_e4_path_traversal_in_name_rejected_and_touches_nothing(
        aiohttp_client, roles_app, fake_ctx_with_project, project_dir, tmp_path):
    """E4: path traversal in {name} returns 400 and touches no file outside the scope dir."""
    client = await aiohttp_client(roles_app)
    h = _auth_headers(fake_ctx_with_project)
    evil = tmp_path / "evil.md"

    # A raw ".." segment never reaches the server: HTTP clients normalize the URL first
    # (RFC 3986 dot-segment removal), so it collapses to a different, non-matching route —
    # itself a form of "touches nothing", but not a 400 from our handler. Percent-encoded
    # and otherwise-invalid names DO reach the handler and are the meaningful cases here.
    for bad_name in ("..%2Fevil", "a%2Fb", "Upper", "x" * 40):
        r = await client.post(
            f"/api/projects/myproject/roles/{bad_name}",
            json={"scope": "project", "content": _VALID_ROLE_BODY}, headers=h,
        )
        assert r.status == 400, bad_name
        r_del = await client.delete(f"/api/projects/myproject/roles/{bad_name}?scope=project", headers=h)
        assert r_del.status == 400, bad_name
        r_get = await client.get(f"/api/projects/myproject/roles/{bad_name}?scope=project", headers=h)
        assert r_get.status == 400, bad_name

    assert not evil.exists()
    # Nothing landed outside the project's own .claude-ops/roles/ directory either.
    role_dir = project_dir / ".claude-ops" / "roles"
    if role_dir.exists():
        assert list(role_dir.iterdir()) == []


async def test_e5_invalid_content_400_and_previous_file_unchanged(
        aiohttp_client, roles_app, fake_ctx_with_project, project_dir):
    """E5: a write with invalid content returns 400 and leaves the previous file unchanged."""
    client = await aiohttp_client(roles_app)
    h = _auth_headers(fake_ctx_with_project)

    ok = await client.post("/api/projects/myproject/roles/helper",
                            json={"scope": "project", "content": _VALID_ROLE_BODY}, headers=h)
    assert ok.status == 200
    role_path = project_dir / ".claude-ops" / "roles" / "helper.md"
    original = role_path.read_text()

    bad = await client.post("/api/projects/myproject/roles/helper",
                             json={"scope": "project", "content": "---\nname: helper\n---\n"},
                             headers=h)
    assert bad.status == 400
    assert role_path.read_text() == original


async def test_e5b_unrecognized_model_400_at_write_time(aiohttp_client, roles_app, fake_ctx_with_project):
    """CHECKLIST C9 (write-time model validation, exercised through the HTTP write path):
    a role naming a model the cockpit does not recognize is rejected with 400, not written."""
    client = await aiohttp_client(roles_app)
    h = _auth_headers(fake_ctx_with_project)
    bad_model_body = (
        "---\nname: helper\ndescription: use this when needed\nmodel: gpt-4o\n---\nBody.\n"
    )
    r = await client.post("/api/projects/myproject/roles/helper",
                           json={"scope": "project", "content": bad_model_body}, headers=h)
    assert r.status == 400
    assert "model" in (await r.json())["error"].lower()

    # But the legitimate explicit-id shape (as used by the shipped builtins) is accepted.
    good_model_body = (
        "---\nname: helper\ndescription: use this when needed\nmodel: claude-sonnet-5\n---\nBody.\n"
    )
    r2 = await client.post("/api/projects/myproject/roles/helper",
                            json={"scope": "project", "content": good_model_body}, headers=h)
    assert r2.status == 200


async def test_e5d_overwriting_an_existing_role_without_the_flag_is_409(
        aiohttp_client, roles_app, fake_ctx_with_project, project_dir):
    """I1k fix (A2-audit.md/F2 residual): the only guard used to be a client-side
    `shadowed_by` snapshot from the last GET — a stale tab, a second window or a direct API
    call defeated it outright. A second POST to the same name/scope with perfectly valid
    content must now be refused (409) unless the caller explicitly asks for overwrite."""
    client = await aiohttp_client(roles_app)
    h = _auth_headers(fake_ctx_with_project)

    first = await client.post("/api/projects/myproject/roles/helper",
                               json={"scope": "project", "content": _VALID_ROLE_BODY}, headers=h)
    assert first.status == 200

    second = await client.post("/api/projects/myproject/roles/helper",
                                json={"scope": "project", "content": _VALID_ROLE_BODY}, headers=h)
    assert second.status == 409

    role_path = project_dir / ".claude-ops" / "roles" / "helper.md"
    assert role_path.read_text(encoding="utf-8") == _VALID_ROLE_BODY  # untouched by the 409

    third = await client.post(
        "/api/projects/myproject/roles/helper",
        json={"scope": "project", "content": _VALID_ROLE_BODY, "overwrite": True}, headers=h,
    )
    assert third.status == 200


async def test_e6_write_is_atomic_no_leftover_tmp_file(aiohttp_client, roles_app, fake_ctx_with_project, project_dir):
    """E6: writes are atomic — no partially-written / leftover temp file after a write."""
    client = await aiohttp_client(roles_app)
    h = _auth_headers(fake_ctx_with_project)
    r = await client.post("/api/projects/myproject/roles/helper",
                           json={"scope": "project", "content": _VALID_ROLE_BODY}, headers=h)
    assert r.status == 200
    role_dir = project_dir / ".claude-ops" / "roles"
    names = [p.name for p in role_dir.iterdir()]
    assert names == ["helper.md"]
    assert not any(n.startswith(".tmp-role-") for n in names)


async def test_e7_get_roles_reports_errors_without_failing_the_request(
        aiohttp_client, roles_app, fake_ctx_with_project, project_dir):
    """E7: GET /roles returns `errors` for unparseable files rather than 500ing the request."""
    client = await aiohttp_client(roles_app)
    h = _auth_headers(fake_ctx_with_project)
    broken = project_dir / ".claude-ops" / "roles" / "broken.md"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("not even frontmatter", encoding="utf-8")

    r = await client.get("/api/projects/myproject/roles", headers=h)
    assert r.status == 200
    data = await r.json()
    assert any(e["name"] == "broken" for e in data["errors"])
    # And the rest of the (builtin) roster is still present and enabled — `effective` itself
    # was dropped from the payload (F12 leanness: no frontend reader, a whole extra walk).
    assert any(r["name"] == "executor" and r["enabled"] for r in data["roles"])


async def test_e8_deleting_project_role_restores_builtin_effective(
        aiohttp_client, roles_app, fake_ctx_with_project, project_dir):
    """E8: deleting a project role that shadows a builtin makes the builtin effective again."""
    client = await aiohttp_client(roles_app)
    h = _auth_headers(fake_ctx_with_project)
    override_body = (
        "---\nname: executor\ndescription: overridden executor. use this when x\nmodel: opus\n---\n"
        "Overridden body.\n"
    )
    w = await client.post("/api/projects/myproject/roles/executor",
                           json={"scope": "project", "content": override_body}, headers=h)
    assert w.status == 200

    before = await client.get("/api/projects/myproject/roles/executor?scope=builtin", headers=h)
    before_json = await before.json()
    assert before_json["role"]["shadowed_by"] == "project"

    d = await client.delete("/api/projects/myproject/roles/executor?scope=project", headers=h)
    assert d.status == 200

    after = await client.get("/api/projects/myproject/roles/executor?scope=builtin", headers=h)
    after_json = await after.json()
    assert after_json["role"]["shadowed_by"] is None
    assert after_json["role"]["model"] == "claude-sonnet-5"


# ─────────────────────────── I. Safety and regressions ───────────────────────────

@pytest.mark.asyncio
async def test_i1h_ultracode_prompt_names_the_effective_roster(isolated_dirs):
    """I1h fix (A2-audit.md/N1 second half): ULTRACODE_PROMPT used to hardcode the four
    original role names — with a project registry that differs (e.g. only a custom role, or
    all roles disabled), an ultracode turn advertised agent types that do not resolve. A
    per-call line naming the roster THIS turn actually resolved must be present alongside it."""
    cwd, _ = isolated_dirs
    _write(Path(cwd) / ".claude-ops" / "roles" / "solo.md",
           "---\nname: solo\ndescription: use this when solo is needed\n---\nSolo body.\n")
    for name in R.load_roles(None):  # disable every shipped builtin — only "solo" remains
        R.set_enabled(cwd, name, "builtin", False)
    opts = await _run_engine_capturing(
        project_name="t", cwd=cwd, prompt="hi", session_key="i1h:t", model="sonnet",
        ultracode=True,
    )
    appended = opts.system_prompt.get("append") or ""
    assert engine.ULTRACODE_PROMPT in appended  # the static complement text is untouched
    assert "Agent types actually available this turn: `solo`." in appended


@pytest.mark.asyncio
async def test_i1h_ultracode_prompt_reports_no_agents_when_roster_is_empty(isolated_dirs):
    """I1h + N1 companion: when every role is disabled, the roster note says so plainly
    instead of silently omitting it (and Agent/Task/Workflow are denied outright — see
    test_i1b_disabling_every_role_denies_agent_spawning_tools)."""
    cwd, _ = isolated_dirs
    for name in R.load_roles(None):
        R.set_enabled(cwd, name, "builtin", False)
    opts = await _run_engine_capturing(
        project_name="t", cwd=cwd, prompt="hi", session_key="i1h-empty:t", model="sonnet",
        ultracode=True,
    )
    appended = opts.system_prompt.get("append") or ""
    assert "No named agent types are wired for this turn" in appended


def test_i1m_env_model_override_warns_once_when_registry_is_in_play(isolated_dirs, monkeypatch, capsys):
    """I1m fix (A2-audit.md/N4): EXECUTOR_MODEL/RESEARCHER_MODEL/QUICK_MODEL only feed
    DEFAULT_AGENTS, the fallback used when zero role files resolve. roles/builtin/*.md always
    ship, so the registry is normally in play and that env pin is a silent no-op — warn once."""
    cwd, _ = isolated_dirs
    monkeypatch.setenv("EXECUTOR_MODEL", "opus")
    monkeypatch.setattr(engine, "_env_model_override_warned", False)

    async def _run():
        return await _run_engine_capturing(
            project_name="t", cwd=cwd, prompt="hi", session_key="i1m:t", model="sonnet",
        )

    import asyncio
    asyncio.run(_run())
    out = capsys.readouterr().out
    assert "EXECUTOR_MODEL" in out
    assert "only feeds the unreachable fallback roster" in out


def test_i1m_no_warning_when_env_override_unset(isolated_dirs, monkeypatch, capsys):
    cwd, _ = isolated_dirs
    monkeypatch.delenv("EXECUTOR_MODEL", raising=False)
    monkeypatch.delenv("RESEARCHER_MODEL", raising=False)
    monkeypatch.delenv("QUICK_MODEL", raising=False)
    monkeypatch.setattr(engine, "_env_model_override_warned", False)
    engine._warn_env_model_override_shadowed()
    assert "unreachable fallback roster" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_i1_role_tools_pass_through_unmodified_by_engine_wiring(isolated_dirs):
    """I1: engine.py does not widen a role's tools beyond what the role file (via
    roles.compile_agents) declares — the roster handed to ClaudeAgentOptions carries the
    same `tools`/`disallowedTools` the registry compiled, verbatim."""
    cwd, _ = isolated_dirs
    _write(Path(cwd) / ".claude-ops" / "roles" / "narrow.md",
           "---\nname: narrow\ndescription: use this when read-only access is enough\n"
           "tools: [Read]\ndisallowedTools: [Write, Edit, Bash]\n---\nRead-only helper.\n")
    opts = await _run_engine_capturing(
        project_name="t", cwd=cwd, prompt="hi", session_key="i1:t", model="sonnet",
    )
    assert opts.agents["narrow"].tools == ["Read"]
    assert opts.agents["narrow"].disallowedTools == ["Write", "Edit", "Bash"]


@pytest.mark.asyncio
async def test_i1b_disabling_every_role_denies_agent_spawning_tools(isolated_dirs, capsys):
    """I1b/F1 fix (A1-audit.md), amended after A2-audit.md's N1 finding: disabling EVERY role
    must not silently resurrect DEFAULT_AGENTS (still true — asserted below), but the OLD
    version of this test stopped at `opts.agents == {}` and called that sufficient. It is not:
    the SDK drops a falsy `agents` dict from the initialize request entirely
    (claude_agent_sdk/_internal/client.py, query.py), so the CLI would fall back to its OWN
    ~/.claude/agents + <cwd>/.claude/agents discovery plus built-in agent types — exactly the
    precedence collision spec-091 exists to eliminate. The real guarantee is that Agent/Task/
    Workflow are DENIED for this turn, so nothing can spawn a sub-agent through that fallback
    path at all."""
    cwd, _ = isolated_dirs
    for name in R.load_roles(None):  # builtins only (no cwd tier) — every shipped role name
        R.set_enabled(cwd, name, "builtin", False)
    opts = await _run_engine_capturing(
        project_name="t", cwd=cwd, prompt="hi", session_key="i1b:t", model="sonnet",
    )
    assert opts.agents == {}
    assert opts.agents is not engine.DEFAULT_AGENTS
    assert "Agent" in opts.disallowed_tools
    assert "Task" in opts.disallowed_tools
    assert "Workflow" in opts.disallowed_tools
    assert "empty agent roster — denying Agent/Task/Workflow" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_i2_malformed_role_file_does_not_break_the_turn(isolated_dirs):
    """I2: a session with one bad role file still starts and still has the other roles."""
    cwd, _ = isolated_dirs
    _write(Path(cwd) / ".claude-ops" / "roles" / "broken.md", "not even frontmatter")
    opts = await _run_engine_capturing(
        project_name="t", cwd=cwd, prompt="hi", session_key="i2:t", model="sonnet",
    )
    assert opts is not None
    assert "broken" not in opts.agents
    assert "executor" in opts.agents  # the rest of the (builtin) roster survives


async def test_e5c_invalid_effort_permission_mode_memory_maxturns_rejected_at_write_time(
        aiohttp_client, roles_app, fake_ctx_with_project):
    """I1e/F7 fix (A1-audit.md): a typo'd enum value (not just an unparseable file, CHECKLIST
    I2's own case) must be rejected at write time, in the same place `model` already is —
    write_role/compile_agents hand it to AgentDefinition verbatim otherwise, and the SDK's
    dataclass does not enforce its own Literal types at runtime."""
    client = await aiohttp_client(roles_app)
    h = _auth_headers(fake_ctx_with_project)
    for bad_field, bad_value in (
        ("effort", "banana"), ("permissionMode", "yolo"),
        ("memory", "nonsense"), ("maxTurns", "-5"),
    ):
        body = (
            f"---\nname: helper\ndescription: use this when needed\n{bad_field}: {bad_value}\n"
            "---\nBody.\n"
        )
        r = await client.post("/api/projects/myproject/roles/helper",
                               json={"scope": "project", "content": body}, headers=h)
        assert r.status == 400, (bad_field, r.status)
        assert bad_field.lower() in (await r.json())["error"].lower(), bad_field

    good_body = (
        "---\nname: helper\ndescription: use this when needed\neffort: xhigh\n"
        "permissionMode: acceptEdits\nmemory: project\nmaxTurns: 10\n---\nBody.\n"
    )
    r_ok = await client.post("/api/projects/myproject/roles/helper",
                              json={"scope": "project", "content": good_body}, headers=h)
    assert r_ok.status == 200


# spec-091 F13 (A1-audit.md): anchored to the pre-fix-round baseline commit instead of a bare
# working-tree `git diff`, which self-neutralizes (goes empty, assertion trivially green) the
# moment this work is committed. Also scans roles.py + roles/builtin/*.md by their on-disk
# content — both are untracked at spec-091 time, so a `git diff` never sees them at all.
_SPEC091_BASELINE_COMMIT = "88fd6c2"


def test_i4_no_personal_path_or_secret_in_new_code():
    """I4: no secret/token/personal path lands in this spec's surface. roles.py and
    roles/builtin/*.md are scanned whole (untracked, so `git diff` is blind to them);
    engine.py/webapp.py are diffed against a fixed baseline commit instead of the bare
    working tree, so the check does not go vacuous once committed."""
    import subprocess
    forbidden = re.compile(r"/home/[a-z][a-z0-9_-]*\b")

    hits = []
    for p in (ROOT / "roles.py", *sorted((ROOT / "roles" / "builtin").glob("*.md"))):
        text = p.read_text(encoding="utf-8")
        for ln, line in enumerate(text.splitlines(), start=1):
            if forbidden.search(line):
                hits.append(f"{p}:{ln}: {line}")
    assert hits == [], hits

    # N7 fix (A2-audit.md): a hardcoded commit SHA with check=True turns "history moved on"
    # (rebase/squash drops the object — this repo has squashed history before, memory
    # a1f0c0-history-squash-decision) into a CalledProcessError ERROR, not a clean failure or
    # skip. Probe the commit exists first and skip cleanly when it does not, instead of letting
    # the diff call raise.
    cat_file = subprocess.run(
        ["git", "cat-file", "-e", _SPEC091_BASELINE_COMMIT],
        cwd=ROOT, capture_output=True, text=True,
    )
    if cat_file.returncode != 0:
        pytest.skip(f"baseline commit {_SPEC091_BASELINE_COMMIT} is gone (history rewritten) — "
                    "skipping the diff-scoped half of I4")
    diff_proc = subprocess.run(
        ["git", "diff", _SPEC091_BASELINE_COMMIT, "--", "engine.py", "webapp.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert diff_proc.returncode == 0, diff_proc.stderr
    added_lines = [l for l in diff_proc.stdout.splitlines() if l.startswith("+") and not l.startswith("+++")]
    diff_hits = [l for l in added_lines if forbidden.search(l)]
    assert diff_hits == [], diff_hits


# ─────────────────────────── G7 (already-shipped section, our one addition) ───────────────────────────

def test_g7_agent_monitor_row_carries_role_model_when_registry_knows_it():
    """G7: the agent monitor row (`_monitor_delta`'s "Agent" branch) carries the role's model
    when the caller-supplied lookup (built from `effective_agents` in run_engine) knows the
    role name; None when it does not."""
    tr = {"status": "completed", "agentId": "aid1", "description": "x"}
    ti = {"description": "x", "subagent_type": "executor"}
    d = engine._monitor_delta("Agent", ti, tr, "orch", agent_model_lookup={"executor": "claude-sonnet-5"})
    assert d["model"] == "claude-sonnet-5"

    d_unknown = engine._monitor_delta("Agent", ti, tr, "orch", agent_model_lookup={})
    assert d_unknown["model"] is None

    d_no_lookup = engine._monitor_delta("Agent", ti, tr, "orch")
    assert d_no_lookup["model"] is None


@pytest.mark.asyncio
async def test_g7_run_engine_wires_the_model_lookup_from_effective_agents(isolated_dirs):
    """G7 end-to-end: run_engine builds the role->model lookup from whatever roster is
    actually in effect for this turn and threads it into the PostToolUse hook."""
    cwd, _ = isolated_dirs
    captured_lookup = {}
    real_maker = engine._make_post_tool_use_hook

    def _spy_maker(project_name, session_key, agent_model_lookup=None):
        captured_lookup["lookup"] = agent_model_lookup
        return real_maker(project_name, session_key, agent_model_lookup=agent_model_lookup)

    with patch.object(engine, "_make_post_tool_use_hook", side_effect=_spy_maker):
        await _run_engine_capturing(
            project_name="t", cwd=cwd, prompt="hi", session_key="g7:t", model="sonnet",
        )
    # N4/I1m fix (A2-audit.md): compare against the literal baseline, not DEFAULT_AGENTS.model
    # — see _DEFAULT_MODEL_BY_ROLE above.
    assert captured_lookup["lookup"]["executor"] == _DEFAULT_MODEL_BY_ROLE["executor"]
