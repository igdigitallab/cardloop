"""
Tests for the default prompt template seed mechanism (Integration Roadmap items 1-2).

Covers:
- DEFAULT_PROMPT_TEMPLATES constant exists with the three expected slugs.
- _seed_default_prompts inserts defaults into an empty prompts.json.
- _seed_default_prompts does NOT modify existing operator entries.
- _seed_default_prompts does NOT re-insert on a second call (idempotent).
- Operator-deleted defaults (recorded in __deleted_defaults) are not re-seeded.
- _load_prompts returns a plain list regardless of file format (list vs dict).
- _save_prompts / _load_prompts_raw round-trip preserves deleted_defaults.
- api_prompt_delete records slug_id in __deleted_defaults when a default is deleted.
- executor agent prompt contains the three addendum keywords.
"""
import sys
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp
import bot as _bot


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def ctx(tmp_path: Path) -> dict:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return {"DATA": data_dir}


# ─── DEFAULT_PROMPT_TEMPLATES constant ───────────────────────────────────────

def test_default_templates_has_three_entries():
    assert len(_webapp.DEFAULT_PROMPT_TEMPLATES) == 3


def test_default_templates_slug_ids():
    slugs = {t["slug_id"] for t in _webapp.DEFAULT_PROMPT_TEMPLATES}
    assert "spec-writer" in slugs
    assert "debug-triage" in slugs
    assert "pre-deploy-gate" in slugs


def test_default_templates_have_required_fields():
    for tpl in _webapp.DEFAULT_PROMPT_TEMPLATES:
        assert "slug_id" in tpl, f"Missing slug_id in {tpl}"
        assert "id" in tpl, f"Missing id in {tpl}"
        assert "title" in tpl, f"Missing title in {tpl}"
        assert "text" in tpl, f"Missing text in {tpl}"


def test_default_slug_set_matches_templates():
    assert _webapp._DEFAULT_SLUGS == {t["slug_id"] for t in _webapp.DEFAULT_PROMPT_TEMPLATES}


# ─── Seed: empty file → all three defaults inserted ──────────────────────────

def test_seed_inserts_defaults_into_empty_file(ctx):
    _webapp._seed_default_prompts(ctx)
    prompts = _webapp._load_prompts(ctx)
    slugs = {p.get("slug_id") for p in prompts}
    assert "spec-writer" in slugs
    assert "debug-triage" in slugs
    assert "pre-deploy-gate" in slugs


def test_seed_creates_prompts_json_if_absent(ctx):
    p = _webapp._prompts_path(ctx)
    assert not p.exists()
    _webapp._seed_default_prompts(ctx)
    assert p.exists()


# ─── Seed: idempotent — second call does not duplicate defaults ───────────────

def test_seed_is_idempotent(ctx):
    _webapp._seed_default_prompts(ctx)
    _webapp._seed_default_prompts(ctx)  # second call
    prompts = _webapp._load_prompts(ctx)
    slug_list = [p.get("slug_id") for p in prompts if p.get("slug_id") in _webapp._DEFAULT_SLUGS]
    # Each default slug should appear exactly once
    for slug in _webapp._DEFAULT_SLUGS:
        assert slug_list.count(slug) == 1, f"Duplicate default slug: {slug}"


# ─── Seed: operator entries are never modified ────────────────────────────────

def test_seed_does_not_overwrite_operator_entries(ctx):
    # Operator already has a custom entry
    operator_entry = {"id": "op-001", "title": "My custom prompt", "text": "Do something useful."}
    _webapp._save_prompts(ctx, [operator_entry])

    _webapp._seed_default_prompts(ctx)

    prompts = _webapp._load_prompts(ctx)
    # Operator entry must still be present and unchanged
    ids = [p["id"] for p in prompts]
    assert "op-001" in ids
    found = next(p for p in prompts if p["id"] == "op-001")
    assert found["title"] == "My custom prompt"
    assert found["text"] == "Do something useful."


def test_seed_adds_defaults_alongside_operator_entries(ctx):
    operator_entry = {"id": "op-002", "title": "Another one", "text": "Do another thing."}
    _webapp._save_prompts(ctx, [operator_entry])

    _webapp._seed_default_prompts(ctx)

    prompts = _webapp._load_prompts(ctx)
    ids = {p["id"] for p in prompts}
    # Both operator and defaults present
    assert "op-002" in ids
    assert "default-spec-writer" in ids
    assert "default-debug-triage" in ids
    assert "default-pre-deploy-gate" in ids


# ─── Seed: deleted defaults are not re-inserted ──────────────────────────────

def test_seed_respects_deleted_defaults(ctx):
    """If operator deleted spec-writer, it must not come back after re-seed."""
    # Simulate state after deletion: file has __deleted_defaults
    payload = {
        "__deleted_defaults": ["spec-writer"],
        "prompts": [
            {"id": "default-debug-triage", "slug_id": "debug-triage", "title": "Debug triage", "text": "x"},
            {"id": "default-pre-deploy-gate", "slug_id": "pre-deploy-gate", "title": "Pre-deploy gate", "text": "y"},
        ],
    }
    _webapp._prompts_path(ctx).write_text(json.dumps(payload))

    _webapp._seed_default_prompts(ctx)

    prompts = _webapp._load_prompts(ctx)
    slugs = {p.get("slug_id") for p in prompts}
    assert "spec-writer" not in slugs, "Deleted default must not be re-seeded"
    assert "debug-triage" in slugs
    assert "pre-deploy-gate" in slugs


def test_seed_deleted_defaults_list_preserved_after_seed(ctx):
    """__deleted_defaults list must survive _seed_default_prompts."""
    payload = {
        "__deleted_defaults": ["spec-writer"],
        "prompts": [],
    }
    _webapp._prompts_path(ctx).write_text(json.dumps(payload))

    _webapp._seed_default_prompts(ctx)

    _, deleted = _webapp._load_prompts_raw(ctx)
    assert "spec-writer" in deleted


# ─── _load_prompts handles both file formats ─────────────────────────────────

def test_load_prompts_handles_plain_list(ctx):
    data = [{"id": "x1", "title": "T", "text": "t"}]
    _webapp._prompts_path(ctx).write_text(json.dumps(data))
    prompts = _webapp._load_prompts(ctx)
    assert isinstance(prompts, list)
    assert prompts[0]["id"] == "x1"


def test_load_prompts_handles_dict_format(ctx):
    data = {
        "__deleted_defaults": ["spec-writer"],
        "prompts": [{"id": "x2", "title": "T2", "text": "t2"}],
    }
    _webapp._prompts_path(ctx).write_text(json.dumps(data))
    prompts = _webapp._load_prompts(ctx)
    assert isinstance(prompts, list)
    assert prompts[0]["id"] == "x2"


def test_load_prompts_returns_empty_list_when_absent(ctx):
    assert _webapp._load_prompts(ctx) == []


# ─── _load_prompts_raw round-trip ─────────────────────────────────────────────

def test_load_prompts_raw_returns_deleted_defaults(ctx):
    data = {
        "__deleted_defaults": ["debug-triage"],
        "prompts": [{"id": "a", "title": "A", "text": "a"}],
    }
    _webapp._prompts_path(ctx).write_text(json.dumps(data))
    prompts, deleted = _webapp._load_prompts_raw(ctx)
    assert [p["id"] for p in prompts] == ["a"]
    assert "debug-triage" in deleted


def test_save_and_load_prompts_raw_roundtrip(ctx):
    prompts = [{"id": "b", "title": "B", "text": "b"}]
    deleted = ["pre-deploy-gate"]
    _webapp._save_prompts(ctx, prompts, deleted)
    loaded_prompts, loaded_deleted = _webapp._load_prompts_raw(ctx)
    assert [p["id"] for p in loaded_prompts] == ["b"]
    assert loaded_deleted == ["pre-deploy-gate"]


# ─── api_prompt_delete records deleted default slug ──────────────────────────

@pytest.mark.asyncio
async def test_api_prompt_delete_records_default_slug(ctx, aiohttp_client):
    """Deleting a default template must add its slug_id to __deleted_defaults."""
    from aiohttp import web

    # Set up with seeded defaults
    _webapp._seed_default_prompts(ctx)

    password = "testpw"
    ctx["password"] = password
    ctx["_auth_token"] = _webapp._derive_token(password)

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_get("/api/prompts", _webapp.api_prompts_list)
    app.router.add_post("/api/prompts", _webapp.api_prompt_create)
    app.router.add_delete("/api/prompts/{id}", _webapp.api_prompt_delete)

    client = await aiohttp_client(app)
    token = ctx["_auth_token"]
    headers = {"Cookie": f"cops_auth={token}"}

    # Get the list to find the spec-writer id
    resp = await client.get("/api/prompts", headers=headers)
    data = await resp.json()
    spec_writer = next(p for p in data["prompts"] if p.get("slug_id") == "spec-writer")

    # Delete it
    resp = await client.delete(f"/api/prompts/{spec_writer['id']}", headers=headers)
    assert resp.status == 200

    # Verify __deleted_defaults was updated
    _, deleted = _webapp._load_prompts_raw(ctx)
    assert "spec-writer" in deleted, f"spec-writer should be in deleted_defaults, got: {deleted}"


@pytest.mark.asyncio
async def test_api_prompt_delete_non_default_does_not_touch_deleted_list(ctx, aiohttp_client):
    """Deleting an operator entry (no slug_id) must not modify __deleted_defaults."""
    from aiohttp import web

    password = "testpw2"
    ctx["password"] = password
    ctx["_auth_token"] = _webapp._derive_token(password)

    # Start with an operator-only entry
    _webapp._save_prompts(ctx, [{"id": "op-x", "title": "Op", "text": "text"}])

    app = web.Application(middlewares=[_webapp.auth_middleware])
    app["ctx"] = ctx
    app.router.add_delete("/api/prompts/{id}", _webapp.api_prompt_delete)

    client = await aiohttp_client(app)
    token = ctx["_auth_token"]
    resp = await client.delete("/api/prompts/op-x", headers={"Cookie": f"cops_auth={token}"})
    assert resp.status == 200

    _, deleted = _webapp._load_prompts_raw(ctx)
    assert deleted == [], f"Expected empty deleted_defaults, got: {deleted}"


# ─── Executor prompt addendums ────────────────────────────────────────────────

def test_executor_prompt_contains_planning_mode():
    prompt = _bot.DEFAULT_AGENTS["executor"].prompt
    assert "PLANNING MODE" in prompt, "Executor prompt must include PLANNING MODE addendum"


def test_executor_prompt_contains_source_driven():
    prompt = _bot.DEFAULT_AGENTS["executor"].prompt
    assert "SOURCE-DRIVEN" in prompt, "Executor prompt must include SOURCE-DRIVEN addendum"


def test_executor_prompt_contains_doubt_check():
    prompt = _bot.DEFAULT_AGENTS["executor"].prompt
    assert "DOUBT CHECK" in prompt, "Executor prompt must include DOUBT CHECK addendum"


def test_researcher_and_quick_prompts_unchanged():
    """researcher and quick agents must NOT contain executor addendums."""
    for name in ("researcher", "quick"):
        prompt = _bot.DEFAULT_AGENTS[name].prompt
        assert "PLANNING MODE" not in prompt, f"{name} prompt must not have PLANNING MODE"
        assert "SOURCE-DRIVEN" not in prompt, f"{name} prompt must not have SOURCE-DRIVEN"
        assert "DOUBT CHECK" not in prompt, f"{name} prompt must not have DOUBT CHECK"
