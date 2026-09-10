"""
Tests for docs/internal/specs/spec-091-agent-roles/reports/FX7-project-creation.md
(fixes for AUD-project-creation.md, the new-project-creation audit).

Covers:
- F1 (blocker): _intent_is_thin + the STEP-1-only thin/rich fork in _build_onboarding_prompt.
  Includes a byte-for-byte proof that the rich path is untouched by the F1 mechanism itself
  (the only delta vs. the pre-fix baseline is the deliberate, separate F2 wording change).
- F2 (high): the onboarding prompt no longer flatly contradicts the card wrapper's
  "Do NOT edit TASKS.md manually" guard (webapp.py _run_card).
- F4 (high): POST /api/projects/{id}/settings {"type": ...} validates against the four
  archetypes instead of falling into the diagnostic-command validator.
- F5 (high): _intent_to_slug transliterates Cyrillic instead of dropping it to "".
- Cost fix: the onboarding card model is capped at 'sonnet' unless the project default is
  already cheaper.
- F3 (judgement call — landed as surgical): _move_card_after_run no longer writes a raw
  multi-line prompt into TASKS.md as a single card line when the running card vanished.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
from webapp import (
    _intent_is_thin,
    _intent_to_slug,
    _build_onboarding_prompt,
    _onboarding_model,
    _derive_token,
    _PROJECT_ARCHETYPES,
)
from board import _parse_tasks


# ═══════════════════════════ F1 — _intent_is_thin ═══════════════════════════

@pytest.mark.parametrize("intent", [
    "",
    "   ",
    "test",
    "asdf",
    "landing page",                # 2 words, no goal stated
    "тестовый проект",             # audit's exact repro case
    "новый тестовый проект",       # 3 words, all filler (Russian)
    "new test project",            # 3 words, all filler (English)
    "🚀🚀🚀",                       # emoji-only — one "word", no whitespace inside it
])
def test_intent_is_thin_true_cases(intent):
    assert _intent_is_thin(intent) is True


@pytest.mark.parametrize("intent", [
    "Build a Next.js landing page for Acme dental, copy from their IG",
    "Write a blog post about SEO for small dental clinics",
    "automate my nightly backup pipeline to S3",
    "Сделать лендинг для стоматологии Acme, взять контент из их Instagram",
    "исследовать конкурентов в нише доставки еды",
])
def test_intent_is_thin_false_cases(intent):
    assert _intent_is_thin(intent) is False


def test_intent_is_thin_is_script_agnostic_not_a_blanket_cyrillic_rule():
    """The rule must not treat every Russian phrase as thin, nor every Russian phrase as
    rich — it is the same word-count/filler test regardless of alphabet."""
    thin_ru = "тестовый проект"
    rich_ru = "Сделать лендинг для стоматологии Acme, взять контент из их Instagram"
    thin_en = "test project"
    rich_en = "Build a Next.js landing page for Acme dental"
    assert _intent_is_thin(thin_ru) is True
    assert _intent_is_thin(rich_ru) is False
    assert _intent_is_thin(thin_en) is True
    assert _intent_is_thin(rich_en) is False


# ═══════════════════ F1 — rich-path byte-identity proof ═══════════════════

# Captured from `webapp._build_onboarding_prompt("software", cwd, intent)` (2-arg
# pre-fix signature) BEFORE any of F1/F2 were implemented, for the audit's exact
# example intent. This is the "before" half of the required before/after proof.
_PRE_FIX_RICH_PROMPT = (
    "New software project initialized. Folder: /home/igor/projects/acme-landing.\n"
    "Intent: \"Build a Next.js landing page for Acme dental, copy from their IG\"\n\n"
    "Starter files are in place. Your job: be a proactive partner, not an interrogator.\n\n"
    "STEP 1 — Propose and scaffold immediately:\n"
    "- Based on the intent \"Build a Next.js landing page for Acme dental, copy from their IG\", "
    "infer the project goal, then:\n"
    "- Rewrite the Goal section in CLAUDE.md (1-2 sentences about what and why).\n"
    "- Add 3 real starter tasks to ## Backlog in TASKS.md (remove placeholder cards). "
    "Make them specific and actionable: verb + object + done-criterion.\n"
    "- End with ONE brief question: ask what's most important to clarify first, "
    "or suggest \"start with task 1?\"\n"
    "- After scaffolding: run `git init` + initial commit if not already done.\n"
    "- Ask about the stack (1 question) if not obvious from the intent.\n\n"
    "STEP 2 — After my response:\n"
    "- Adapt CLAUDE.md further based on what I say.\n"
    "- If I mentioned existing code/files → scan them (Read a few), brief summary.\n"
    "- Fill in README.md minimally.\n"
    "STEP 3 — error handler:\n"
    "- If this is a service or bot, add a global error handler "
    "(FastAPI/aiohttp middleware, PTB add_error_handler, or CLI try/except in main → logger.error). "
    "The cockpit scanner greps for `UNHANDLED exc_class=<Type> path=<route>`. "
    "Without it the cockpit is blind to runtime errors.\n"
    "- Update ## Cardloop Integration Status in CLAUDE.md once set up.\n\n"
    "Keep it lean. Propose, don't interrogate. Lead with action, not questions."
)

_RICH_INTENT = "Build a Next.js landing page for Acme dental, copy from their IG"
_RICH_CWD = "/home/igor/projects/acme-landing"


def test_f1_rich_path_default_thin_flag_is_false():
    """thin defaults to False — a caller that never learned about the new parameter (the
    old 3-arg call shape) still gets today's behaviour, not the thin one."""
    assert _build_onboarding_prompt("software", _RICH_CWD, _RICH_INTENT) == \
        _build_onboarding_prompt("software", _RICH_CWD, _RICH_INTENT, thin=False)


def test_f1_plus_f2_rich_prompt_diff_is_exactly_the_f2_line():
    """Proves two things at once:
    1) F1's thin/rich fork does not perturb the rich path at all (structurally the rich
       branch IS the original STEP-1 text, verified by diffing line-for-line).
    2) The ONLY delta from the pre-fix baseline is the F2 rewording of the single
       "Add 3 real starter tasks to TASKS.md" line — nothing else drifted.
    """
    new = _build_onboarding_prompt("software", _RICH_CWD, _RICH_INTENT, thin=False)
    old_lines = _PRE_FIX_RICH_PROMPT.splitlines()
    new_lines = new.splitlines()
    assert len(old_lines) == len(new_lines), "F2 must not add/remove lines, only reword one"

    changed = [i for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
    assert changed == [8], f"expected only the TASKS.md instruction line to change, got {changed}"
    # And that one line's rewording is the F2 fix, not noise: it must still ask for 3
    # backlog cards, but must no longer read as an unscoped "edit TASKS.md" instruction —
    # it must explicitly carve out the running card from that edit.
    assert "Add 3 real starter tasks to ## Backlog in TASKS.md" in new_lines[8]
    assert "Leave ## In Progress and this card alone" in new_lines[8]


def test_f2_rich_prompt_no_longer_bare_edit_tasks_md_instruction():
    """F2: the wrapper appended by _run_card says 'Do NOT edit TASKS.md manually — the
    cockpit handles the move.' The onboarding prompt must not read as a flat contradiction
    of that — it must scope its own TASKS.md edit to Backlog and explicitly say not to
    touch the running card / its move."""
    prompt = _build_onboarding_prompt("software", _RICH_CWD, _RICH_INTENT, thin=False)
    assert "Leave ## In Progress and this card alone" in prompt
    assert "the cockpit moves it for you" in prompt


# ═══════════════════════ F1 — thin path writes nothing ═══════════════════════

@pytest.mark.parametrize("intent", ["", "test", "тестовый проект"])
def test_thin_prompt_forbids_writing_claude_and_tasks_md(intent):
    prompt = _build_onboarding_prompt("software", "/tmp/proj", intent, thin=True)
    assert "Do NOT invent" in prompt
    assert "Do NOT edit CLAUDE.md, TASKS.md or README.md in this turn" in prompt
    assert "leave the Goal placeholder exactly as it is" in prompt
    assert "infer the project goal" not in prompt
    assert "Add 3 real starter tasks" not in prompt


def test_thin_prompt_asks_at_most_three_short_questions():
    prompt = _build_onboarding_prompt("software", "/tmp/proj", "test", thin=True)
    step1 = prompt.split("STEP 2")[0]
    # "what this project is for", "first deliverable", "the stack" — 3 items max
    assert step1.count("?") == 0  # it's an instruction to ask, not the questions themselves
    assert "at most 3 short questions" in step1


def test_thin_prompt_omits_git_and_stack_steps_for_software():
    """The rich-only git_step/stack_step suffixes must not leak into the thin STEP 1 —
    'do nothing, just ask' should not also say 'run git init' or 'ask about the stack'."""
    prompt = _build_onboarding_prompt("software", "/tmp/proj", "test", thin=True)
    step1 = prompt.split("STEP 2")[0]
    assert "git init" not in step1
    assert "Ask about the stack" not in step1


# ═══════════════════════════ F4 — settings 'type' validation ═══════════════════════════

async def _settings_client(aiohttp_client, tmp_path, project_type="software"):
    from aiohttp import web

    password = "testpass"
    cwd = tmp_path / "proj"
    cwd.mkdir()
    ctx = {
        "topics": {"proj": {"id": "proj", "project": "Proj", "cwd": str(cwd), "type": project_type}},
        "sessions": {},
        "running": {},
        "password": password,
        "DATA": tmp_path / "data",
        "save_topics": lambda: None,
    }
    ctx["_auth_token"] = _derive_token(password)
    (tmp_path / "data").mkdir(exist_ok=True)

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_post("/api/projects/{id}/settings", _webapp.api_project_settings_post)
    client = await aiohttp_client(app)
    return client, ctx


async def test_settings_type_valid_archetype_accepted(aiohttp_client, tmp_path):
    client, ctx = await _settings_client(aiohttp_client, tmp_path)
    resp = await client.post(
        "/api/projects/proj/settings",
        json={"type": "content"},
        headers={"Cookie": f"cops_auth={ctx['_auth_token']}"},
    )
    assert resp.status == 200
    assert ctx["topics"]["proj"]["type"] == "content"


@pytest.mark.parametrize("bad_type", ["cat", "javascript", "", "SOFTWARE!", "journalctl -u x"])
async def test_settings_type_invalid_value_rejected(aiohttp_client, tmp_path, bad_type):
    client, ctx = await _settings_client(aiohttp_client, tmp_path)
    resp = await client.post(
        "/api/projects/proj/settings",
        json={"type": bad_type},
        headers={"Cookie": f"cops_auth={ctx['_auth_token']}"},
    )
    assert resp.status == 400
    body = await resp.json()
    assert "type" in body.get("error", "")
    # Regression guard for F4's exact bug: a bad type must NOT be silently accepted
    # because it happened to look like a safe diagnostic command.
    assert ctx["topics"]["proj"]["type"] == "software"


def test_project_archetypes_constant_matches_infer_archetype_contract():
    """_infer_archetype's documented return values must stay in lockstep with the
    validator's allow-list."""
    assert set(_PROJECT_ARCHETYPES) == {"software", "content", "ops", "scratchpad"}


# ═══════════════════════════ F5 — Cyrillic slug transliteration ═══════════════════════════

@pytest.mark.parametrize("intent,expected", [
    ("тестовый проект", "testovyy-proekt"),
    ("Новый клиент — Иван Петров", "novyy-klient-ivan-petrov"),
    ("Сделать лендинг на Next.js", "sdelat-lending-na-nextjs"),
])
def test_intent_to_slug_cyrillic_transliterates(intent, expected):
    assert _intent_to_slug(intent) == expected


def test_intent_to_slug_mixed_ru_en_stays_readable():
    result = _intent_to_slug("Купить Next.js шаблон для сайта")
    assert result != ""
    assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in result)
    assert "nextjs" in result


def test_intent_to_slug_emoji_only_falls_back_to_empty():
    """Deterministic fallback: emoji-only input still can't produce a slug, so the caller's
    untitled-<ts> path is preserved — F5 must not turn this into a garbage slug."""
    assert _intent_to_slug("🚀🚀🚀") == ""
    assert _intent_to_slug("🎉") == ""


def test_intent_to_slug_latin_unaffected_by_translit_table():
    assert _intent_to_slug("Build a React app! (2024)") == "build-a-react-app-2024"


# ═══════════════════════════ Cost fix — onboarding model cap ═══════════════════════════

@pytest.mark.parametrize("default_model,expected", [
    ("opus", "sonnet"),
    ("fable", "sonnet"),
    ("sonnet", "sonnet"),
    ("haiku", "haiku"),   # already cheaper than the cap — respected, not bumped up
])
def test_onboarding_model_cap(default_model, expected):
    assert _onboarding_model({"DEFAULT_MODEL": default_model}) == expected


def test_onboarding_model_defaults_to_cap_when_no_default_model_set():
    assert _onboarding_model({}) == "sonnet"


# ═══════════════════ F3 — _move_card_after_run corruption guard ═══════════════════

async def test_move_card_after_run_missing_card_does_not_corrupt_board(tmp_path):
    """If the running card vanished from every column (agent deleted it against its own
    instructions), the fallback must never write embedded newlines into TASKS.md as a
    single card line — that is what corrupts the next parse (board.py _serialize_tasks /
    _parse_tasks, per AUD-project-creation.md F3)."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    tasks_path = cwd / "TASKS.md"
    tasks_path.write_text(
        "# Tasks — Proj\n\n## Backlog\n- [ ] Define project goal <!--ops:bl1-->\n\n"
        "## In Progress\n\n## Review\n\n## Failed\n",
        encoding="utf-8",
    )

    multiline_prompt = (
        "New software project initialized. Folder: " + str(cwd) + ".\n"
        "Intent: \"тестовый проект\"\n\n"
        "STEP 1 — Don't guess, ask:\n"
        "- The intent field says only \"тестовый проект\" — that is not enough to state a goal.\n"
        "- Do NOT invent one.\n"
    )
    card = {"id": "init1", "text": multiline_prompt}

    await _webapp._move_card_after_run(
        {}, str(cwd), "Proj", card, "init1", ok=True, session_key="proj",
    )

    raw = tasks_path.read_text(encoding="utf-8")
    # No physical line in the file may itself contain a literal embedded prompt fragment
    # spanning multiple markdown lines glued onto one card marker line.
    for line in raw.splitlines():
        # every non-blank, non-heading line is either a card line or a continuation the
        # parser understands (`- [ ]`, `- `, `  > `) — never a bare fragment like the
        # second physical line of the prompt appearing without its own card syntax.
        if line.strip() and not line.startswith("#"):
            assert line.lstrip().startswith(("- ", "> ")), f"unstructured leaked line: {line!r}"

    # Round-trip through the real parser: exactly one card must land in Review, with the
    # original id preserved and NO phantom extra cards spawned from leaked prompt lines.
    _, cols = _parse_tasks(raw)
    review_ids = [c["id"] for c in cols["review"]]
    assert review_ids == ["init1"]
    assert len(cols["review"]) == 1
    assert cols["review"][0]["text"] == "New software project initialized. Folder: " + str(cwd) + "."
    # Backlog untouched.
    assert [c["id"] for c in cols["backlog"]] == ["bl1"]


async def test_move_card_after_run_normal_path_unaffected(tmp_path):
    """Happy path (card still present) must still work exactly as before — the F3 guard
    only engages when _pop_card returns None."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    tasks_path = cwd / "TASKS.md"
    tasks_path.write_text(
        "# Tasks — Proj\n\n## Backlog\n\n"
        "## In Progress\n- [~] Initialise project <!--ops:init1-->\n\n"
        "## Review\n\n## Failed\n",
        encoding="utf-8",
    )
    card = {"id": "init1", "text": "Initialise project"}
    await _webapp._move_card_after_run(
        {}, str(cwd), "Proj", card, "init1", ok=True, session_key="proj",
    )
    raw = tasks_path.read_text(encoding="utf-8")
    _, cols = _parse_tasks(raw)
    assert [c["id"] for c in cols["review"]] == ["init1"]
    assert cols["review"][0]["text"] == "Initialise project"
    assert cols["in_progress"] == []


# ═══════════════════ end-to-end wiring: POST /api/projects/new → run_engine ═══════════════════

@pytest.fixture
def new_project_wired_app(tmp_path):
    """Like test_spec046's new_project_app, but run_engine is a capturing async generator
    instead of None, so the actual card run is spawned and we can inspect what prompt/model
    reached run_engine for real (not just the pure helper functions)."""
    from aiohttp import web

    password = "testpass"
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    calls: list = []

    async def mock_run_engine(**kwargs):
        calls.append(kwargs)
        yield {"type": "text", "text": "ok"}
        yield {"type": "result", "session_id": "sess-fx7-test"}

    ctx = {
        "topics": {},
        "sessions": {},
        "running": {},
        "password": password,
        "DATA": tmp_path / "data",
        "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": mock_run_engine,
        "ptb_app": None,
        "GROUP_CHAT_ID": 0,
        "rate_limits": {},
    }
    ctx["_auth_token"] = _derive_token(password)
    (tmp_path / "data").mkdir(exist_ok=True)

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_post("/api/projects/new", _webapp.api_new_project)
    return app, ctx, tmp_path, calls


async def _create_and_drain(aiohttp_client, new_project_wired_app, monkeypatch, intent, default_model=None):
    app, ctx, tmp_path, calls = new_project_wired_app
    monkeypatch.setattr(_webapp.Path, "home", staticmethod(lambda: tmp_path))
    if default_model is not None:
        ctx["DEFAULT_MODEL"] = default_model

    spawned: list = []

    def fake_spawn_bg(coro):
        import asyncio
        t = asyncio.ensure_future(coro)
        spawned.append(t)
        return t

    from unittest.mock import patch
    with patch.object(_webapp, "_spawn_bg", fake_spawn_bg), \
         patch.object(_webapp, "_secrets_read", lambda cwd: {}):
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/projects/new",
            json={"intent": intent},
            headers={"Cookie": f"cops_auth={ctx['_auth_token']}"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["started"] is True
        import asyncio
        if spawned:
            await asyncio.wait_for(spawned[0], timeout=5)
    return calls



async def test_new_project_thin_intent_reaches_run_engine_with_thin_prompt(aiohttp_client, new_project_wired_app, monkeypatch):
    calls = await _create_and_drain(aiohttp_client, new_project_wired_app, monkeypatch, "test")
    assert len(calls) == 1
    prompt = calls[0]["prompt"]
    assert "Do NOT invent" in prompt
    assert "infer the project goal" not in prompt
    assert calls[0]["model"] == "sonnet"  # cap (DEFAULT_MODEL default is sonnet in this fixture)


async def test_new_project_rich_intent_reaches_run_engine_with_full_prompt(aiohttp_client, new_project_wired_app, monkeypatch):
    calls = await _create_and_drain(
        aiohttp_client, new_project_wired_app, monkeypatch,
        "Build a Next.js landing page for Acme dental, copy from their IG",
    )
    assert len(calls) == 1
    prompt = calls[0]["prompt"]
    assert "infer the project goal" in prompt
    assert "Do NOT invent" not in prompt


async def test_new_project_model_capped_from_opus_default(aiohttp_client, new_project_wired_app, monkeypatch):
    calls = await _create_and_drain(
        aiohttp_client, new_project_wired_app, monkeypatch,
        "Build a Next.js landing page for Acme dental",
        default_model="opus",
    )
    assert calls[0]["model"] == "sonnet"


async def test_new_project_model_respects_cheaper_haiku_default(aiohttp_client, new_project_wired_app, monkeypatch):
    calls = await _create_and_drain(
        aiohttp_client, new_project_wired_app, monkeypatch,
        "Build a Next.js landing page for Acme dental",
        default_model="haiku",
    )
    assert calls[0]["model"] == "haiku"
