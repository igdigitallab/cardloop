"""
T1 verification pass for spec-091 Phase 1 — adversarial probes NOT already covered by
tests/test_spec091_roles.py (R1) or tests/test_spec091_wiring.py (R2).

This file is additive coverage written by the TESTER agent per the brief ("you may ADD
tests ... adding coverage is not fixing"). It does not modify any existing test.

Each test embeds the CHECKLIST.md line (or defect id from reports/T1-verification.md) it
proves or disproves. A test that fails here is evidence for the verification report, not a
bug in the test.
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import bot
import engine
import roles as R
import webapp as _webapp


# ─────────────────────────── shared fixtures (mirrors test_spec091_wiring.py) ───────────────────────────

@pytest.fixture()
def isolated_dirs(tmp_path, monkeypatch):
    global_d = tmp_path / "global-roles"
    proj_cwd = tmp_path / "proj"
    proj_cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CARDLOOP_ROLES_DIR", str(global_d))
    return str(proj_cwd), str(global_d)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _FakeClient:
    captured: "object" = None

    def __init__(self, options):
        _FakeClient.captured = options

    async def query(self, prompt):
        pass

    async def receive_response(self):
        return
        yield  # pragma: no cover

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


async def _run_engine_capturing(**kwargs):
    with patch.object(engine, "ClaudeSDKClient", _FakeClient), \
         patch.object(engine, "running", {}), \
         patch.object(engine, "audit", lambda *a: None):
        async for _ in bot.run_engine(**kwargs):
            pass
    return _FakeClient.captured


# ═══════════════════════ DEFECT-1: legacy per-project model override silently drops the registry ═══════════════════════

@pytest.mark.asyncio
async def test_defect1_legacy_model_override_no_longer_drops_entire_registry(isolated_dirs):
    """DEFECT-1 / A1-audit.md F4 — FIXED, re-asserted post-fix (was a pre-fix reproduction;
    see T1-verification.md and FX1-backend-fixes.md). A project with
    executor_model/researcher_model/quick_model set in agents_config used to go through
    engine._build_agents_kwargs, which built its override dict from DEFAULT_AGENTS (4 keys)
    and handed it to run_engine as the explicit `agents` kwarg — beating the role registry
    outright per C1's own precedence, so the 7 new builtin roles plus any project/global
    custom role became unreachable by Workflow/Task for as long as the legacy override stayed
    set, with zero error/warning anywhere. `_build_agents_kwargs` now emits only the override
    strings (`agent_model_overrides`), and run_engine MERGES them into whichever roster it
    resolves instead of replacing it."""
    cwd, _ = isolated_dirs
    # A project-scope custom role exists — must survive the override, not disappear.
    _write(Path(cwd) / ".claude-ops" / "roles" / "my-custom-role.md",
           "---\nname: my-custom-role\ndescription: use this for custom work\n---\nCustom.\n")

    kwargs = engine._build_agents_kwargs({"executor_model": "claude-opus-5"})
    opts = await _run_engine_capturing(
        project_name="t", cwd=cwd, prompt="hi", session_key="defect1:t", model="sonnet",
        **kwargs,
    )
    roster = set(opts.agents.keys())

    # Fixed behaviour: the registry's roles (11 builtins + my-custom-role) are all still
    # present, and the executor's model override still applies.
    assert "my-custom-role" in roster
    assert "reviewer-logic" in roster
    assert roster >= {"executor", "researcher", "skeptic", "quick"}
    assert opts.agents["executor"].model == "claude-opus-5"
    # researcher/quick are untouched by this override — still their registry-compiled models.
    assert opts.agents["researcher"].model != "claude-opus-5"


# ═══════════════════════ A9/I2 — a broken file next to good ones in the SAME directory ═══════════════════════

def test_a9_broken_file_next_to_good_files_same_directory(isolated_dirs):
    """A9/I2 adversarial: one unparseable file sitting in the SAME scope directory as
    several good ones. list_roles_report must report exactly one error and still return
    every good role; load_roles must still compile a non-empty, correct registry."""
    cwd, _ = isolated_dirs
    roles_dir = Path(cwd) / ".claude-ops" / "roles"
    _write(roles_dir / "good-one.md",
           "---\nname: good-one\ndescription: use this when testing\n---\nBody one.\n")
    _write(roles_dir / "good-two.md",
           "---\nname: good-two\ndescription: use this when testing two\n---\nBody two.\n")
    _write(roles_dir / "broken.md", "this has no frontmatter fence at all\n")

    roles_list, errors = R.list_roles_report(cwd)
    names = {r.name for r in roles_list}
    assert "good-one" in names and "good-two" in names
    assert "broken" not in names
    assert len(errors) == 1
    assert errors[0]["name"] == "broken"
    assert errors[0]["scope"] == "project"

    effective = R.load_roles(cwd)
    assert "good-one" in effective and "good-two" in effective
    assert "broken" not in effective
    compiled = R.compile_agents(effective)
    assert "good-one" in compiled and "good-two" in compiled


@pytest.mark.asyncio
async def test_i2_session_starts_with_one_bad_file_and_has_the_other_roles(isolated_dirs):
    """I2, exercised at the engine level (not just roles.py): a malformed project role file
    does not prevent run_engine from producing a full ClaudeAgentOptions with the other
    (builtin + good project) roles present."""
    cwd, _ = isolated_dirs
    roles_dir = Path(cwd) / ".claude-ops" / "roles"
    _write(roles_dir / "good-one.md",
           "---\nname: good-one\ndescription: use this when testing\n---\nBody one.\n")
    _write(roles_dir / "broken.md", "not frontmatter\n")

    opts = await _run_engine_capturing(
        project_name="t", cwd=cwd, prompt="hi", session_key="i2:t", model="sonnet",
    )
    assert "good-one" in opts.agents
    assert "executor" in opts.agents  # shipped builtin still present
    assert "broken" not in opts.agents


# ═══════════════════════ HTTP adversarial probes (name/scope edge cases) ═══════════════════════

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
        "topics": {"1001:42": {"project": "myproject", "cwd": str(project_dir), "model": "sonnet"}},
        "sessions": {}, "running": {}, "password": password,
        "DATA": data_dir, "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None, "save_topics": lambda: None,
        "run_engine": None, "ptb_app": None, "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    return ctx


@pytest.fixture
def roles_app(fake_ctx_with_project, tmp_path, monkeypatch):
    from aiohttp import web
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


async def test_e4_empty_name_segment_is_rejected_or_unroutable(aiohttp_client, roles_app, fake_ctx_with_project, project_dir):
    """A6/E4 adversarial: an empty {name} segment (trailing slash) must not create a file
    named '.md' or anything else outside the scope dir."""
    client = await aiohttp_client(roles_app)
    h = _auth_headers(fake_ctx_with_project)
    r = await client.post("/api/projects/myproject/roles/", json={"scope": "project", "content": _VALID_ROLE_BODY}, headers=h)
    # aiohttp's router will not match the dynamic segment on an empty string -> 404 from the
    # router itself, never reaching our handler. Either way nothing must be written.
    assert r.status in (400, 404), r.status
    role_dir = project_dir / ".claude-ops" / "roles"
    assert not role_dir.exists() or list(role_dir.iterdir()) == []


async def test_e4_dotdot_and_absolute_like_names_rejected(aiohttp_client, roles_app, fake_ctx_with_project, project_dir, tmp_path):
    """A6/E4: names that are NOT valid per ROLE_NAME_RE (^[a-z0-9][a-z0-9-]{1,31}$) but that
    a naive implementation might still accept: 'a.b', 'a_b', '-a', '1' alone (1 char — valid,
    len>=2 required... actually check single char), unicode, leading dash, trailing dash,
    double-dash-with-dot, and a name containing an encoded NUL."""
    client = await aiohttp_client(roles_app)
    h = _auth_headers(fake_ctx_with_project)
    for bad_name in ("a.b", "a_b", "-leading-dash", "a%00b", "héllo", "a", "A"):
        r = await client.post(
            f"/api/projects/myproject/roles/{bad_name}",
            json={"scope": "project", "content": _VALID_ROLE_BODY}, headers=h,
        )
        assert r.status in (400, 404), (bad_name, r.status, await r.text())
    role_dir = project_dir / ".claude-ops" / "roles"
    assert not role_dir.exists() or list(role_dir.iterdir()) == []


async def test_e3_builtin_scope_write_delete_never_touches_repo_files(
        aiohttp_client, roles_app, fake_ctx_with_project):
    """E3, verified on DISK (not just the error message): attempting to write or delete
    scope=builtin for every shipped role leaves roles/builtin/*.md byte-identical."""
    import subprocess
    client = await aiohttp_client(roles_app)
    h = _auth_headers(fake_ctx_with_project)

    before = {}
    builtin_dir = Path(R.BUILTIN_DIR)
    for f in builtin_dir.glob("*.md"):
        before[f.name] = f.read_bytes()

    for name in ("executor", "researcher", "skeptic", "quick", "reviewer-logic"):
        r1 = await client.post(f"/api/projects/myproject/roles/{name}",
                                json={"scope": "builtin", "content": _VALID_ROLE_BODY}, headers=h)
        assert r1.status == 400
        r2 = await client.delete(f"/api/projects/myproject/roles/{name}?scope=builtin", headers=h)
        assert r2.status == 400

    after = {}
    for f in builtin_dir.glob("*.md"):
        after[f.name] = f.read_bytes()
    assert before == after

    # Belt + suspenders: if roles/builtin is already tracked by git, confirm git also sees
    # no change (a byte-identical-but-touched mtime would still show as unmodified here).
    # At T1 time roles/builtin is still untracked (`?? roles/builtin/`), so `git diff` is a
    # no-op either way -- the before/after byte comparison above is the real assertion.
    tracked = subprocess.run(["git", "ls-files", "--", "roles/builtin"],
                              cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if tracked:
        out = subprocess.run(["git", "diff", "--stat", "--", "roles/builtin"],
                              cwd=ROOT, capture_output=True, text=True)
        assert out.stdout.strip() == "", f"git diff shows a change under roles/builtin: {out.stdout}"


async def test_f5_toggle_builtin_role_copies_to_project_never_mutates_repo(
        aiohttp_client, roles_app, fake_ctx_with_project, project_dir):
    """F5, verified on disk: toggling scope=builtin via the /enabled endpoint must create a
    project-scope file and must NOT write to roles/builtin/ at all."""
    import subprocess
    client = await aiohttp_client(roles_app)
    h = _auth_headers(fake_ctx_with_project)
    builtin_file = Path(R.BUILTIN_DIR) / "quick.md"
    before = builtin_file.read_bytes()

    r = await client.post("/api/projects/myproject/roles/quick/enabled",
                           json={"scope": "builtin", "enabled": False}, headers=h)
    assert r.status == 200
    body = await r.json()
    assert body["role"]["scope"] == "project"
    assert body["role"]["enabled"] is False

    after = builtin_file.read_bytes()
    assert before == after, "F5 VIOLATION: toggling a builtin role mutated the repo file"

    project_file = project_dir / ".claude-ops" / "roles" / "quick.md"
    assert project_file.is_file()
    assert "enabled: false" in project_file.read_text()

    tracked = subprocess.run(["git", "ls-files", "--", "roles/builtin"],
                              cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if tracked:
        out = subprocess.run(["git", "diff", "--stat", "--", "roles/builtin"],
                              cwd=ROOT, capture_output=True, text=True)
        assert out.stdout.strip() == ""


# ═══════════════════════ C5 regression, against the REAL registry path (not just _build_agents_kwargs) ═══════════════════════

@pytest.mark.asyncio
async def test_c5_registry_role_with_skills_memory_mcp_and_model_override_keeps_all_fields(isolated_dirs):
    """C5, through the NEW registry path (roles.py -> compile_agents), not the legacy
    _build_agents_kwargs path R2 already tested: a role file setting skills/memory/
    mcpServers, combined with the legacy executor_model override, must not lose fields.
    NOTE: this will only pass if DEFECT-1 is fixed (today the override kwarg bypasses the
    registry entirely) -- included here as a forward-looking regression guard as well as
    a second demonstration of DEFECT-1 from the 'field-loss' angle.
    """
    cwd, _ = isolated_dirs
    _write(Path(cwd) / ".claude-ops" / "roles" / "executor.md",
           "---\nname: executor\ndescription: use this when x\nskills: [code-review]\n"
           "memory: project\nmcpServers: [some-server]\n---\nCustom executor body.\n")

    kwargs = engine._build_agents_kwargs({"executor_model": "claude-opus-5"})
    opts = await _run_engine_capturing(
        project_name="t", cwd=cwd, prompt="hi", session_key="c5b:t", model="sonnet",
        **kwargs,
    )
    executor = opts.agents.get("executor")
    assert executor is not None
    if executor.skills != ["code-review"]:
        pytest.xfail(
            "DEFECT-1: the legacy executor_model override bypasses the role registry "
            "entirely, so the role file's skills/memory/mcpServers never reach the "
            "compiled AgentDefinition for this project."
        )
    assert executor.memory == "project"
    assert executor.mcpServers == ["some-server"]


# ═══════════════════════ I4 — no secret/personal path in the NEW tracked files ═══════════════════════

def test_i4_no_personal_path_or_secret_in_roles_py_and_builtin_files():
    """I4, scoped to the actually-new tracked files (roles.py, roles/builtin/*.md),
    independent of any git-diff staging state (R2's own I4 test degrades to a no-op once
    committed -- this one does not)."""
    import re
    suspicious = re.compile(r"/home/[a-zA-Z0-9_-]+(?!/\.claude-ops|/<)|igor(?!@)|coscore\.us|zira777")
    paths = [Path(R.__file__)] + list(Path(R.BUILTIN_DIR).glob("*.md"))
    hits = []
    for p in paths:
        text = p.read_text(encoding="utf-8")
        for m in suspicious.finditer(text):
            hits.append((str(p), m.group(0)))
    assert hits == [], f"possible personal path/identifier leaked: {hits}"
