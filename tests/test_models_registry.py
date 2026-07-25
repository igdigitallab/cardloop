"""Tests for GET /api/models — the live model registry with static fallback.

The live fetch helper (_fetch_live_models) is mocked so no real network/token is touched.
Covers: live path (display-name mapping + alias values + order) and the static fallback
path when the helper returns None.
"""
import json

import pytest

import webapp as _webapp


def _reset_cache():
    _webapp._models_cache["data"] = None
    _webapp._models_cache["ts"] = 0.0


class _FakeReq:
    """Minimal stand-in — api_models only needs to return a json_response (no app/ctx use)."""


def test_build_registry_live_maps_display_names_and_aliases():
    # Newest-first listing (as the API returns). Multiple versions per family → first wins.
    live = [
        {"id": "claude-fable-5-20260101", "display_name": "Claude Fable 5"},
        {"id": "claude-opus-4-8-20251201", "display_name": "Claude Opus 4.8"},
        {"id": "claude-opus-4-7-20250901", "display_name": "Claude Opus 4.7"},
        {"id": "claude-sonnet-4-6-20251101", "display_name": "Claude Sonnet 4.6"},
        # haiku intentionally absent → must fall back to static label for that family only
    ]
    reg = _webapp._build_model_registry(live)
    assert reg["source"] == "live"
    # Order is load-bearing: fable, sonnet, opus, haiku.
    assert [m["value"] for m in reg["models"]] == ["fable", "sonnet", "opus", "haiku"]
    by_value = {m["value"]: m["label"] for m in reg["models"]}
    # Leading "Claude " stripped; newest opus version chosen.
    assert by_value["fable"] == "Fable 5"
    assert by_value["sonnet"] == "Sonnet 4.6"
    assert by_value["opus"] == "Opus 4.8"
    # Missing family → static fallback.
    assert by_value["haiku"] == "Haiku 4.5"


def test_build_registry_static_fallback_when_none():
    reg = _webapp._build_model_registry(None)
    assert reg["source"] == "static"
    assert reg["models"] == [
        {"value": "fable", "label": "Fable 5"},
        {"value": "sonnet", "label": "Sonnet 5"},
        {"value": "opus", "label": "Opus 5"},
        {"value": "haiku", "label": "Haiku 4.5"},
    ]


@pytest.mark.asyncio
async def test_api_models_live_path(monkeypatch):
    _reset_cache()

    async def _fake_live():
        return [
            {"id": "claude-opus-4-8-20251201", "display_name": "Claude Opus 4.8"},
            {"id": "claude-haiku-4-5-20250801", "display_name": "Claude Haiku 4.5"},
        ]

    monkeypatch.setattr(_webapp, "_fetch_live_models", _fake_live)
    resp = await _webapp.api_models(_FakeReq())
    body = json.loads(resp.body.decode())
    assert body["source"] == "live"
    by_value = {m["value"]: m["label"] for m in body["models"]}
    assert by_value["opus"] == "Opus 4.8"
    assert by_value["haiku"] == "Haiku 4.5"
    # Families without a live match still present via static fallback.
    assert by_value["fable"] == "Fable 5"
    assert by_value["sonnet"] == "Sonnet 5"
    _reset_cache()


@pytest.mark.asyncio
async def test_api_models_static_fallback_path(monkeypatch):
    _reset_cache()

    async def _fake_none():
        return None

    monkeypatch.setattr(_webapp, "_fetch_live_models", _fake_none)
    resp = await _webapp.api_models(_FakeReq())
    body = json.loads(resp.body.decode())
    assert body["source"] == "static"
    assert [m["value"] for m in body["models"]] == ["fable", "sonnet", "opus", "haiku"]
    assert all(m["label"] for m in body["models"])
    _reset_cache()


# ── Static-label sync guard ──────────────────────────────────────────────────────────
# The model list is duplicated across three static spots (memory
# `model-labels-live-vs-static-fallback`). They must stay in lockstep, otherwise the
# alias a user picks (backend) and the label they see (frontend) can silently drift.
# This is the OFFLINE half of the "UI label must match reality" guard; the ONLINE half
# (does the alias actually run that model?) lives in tests/test_model_aliases.py.

import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _frontend_models():
    """Parse web/src/lib/models.ts -> [(value, label), ...] in declaration order."""
    src = open(os.path.join(_ROOT, "web", "src", "lib", "models.ts")).read()
    body = src.split("export const MODELS", 1)[1].split("]", 1)[0]
    return re.findall(r"value:\s*'([^']+)'\s*,\s*label:\s*'([^']+)'", body)


def test_static_label_spots_stay_in_sync():
    import engine as _engine

    frontend = _frontend_models()
    backend = list(_webapp._MODEL_FAMILIES)

    # Frontend models.ts and webapp._MODEL_FAMILIES must be identical (aliases, labels, order).
    assert frontend == backend, (
        "web/src/lib/models.ts and webapp._MODEL_FAMILIES diverged — update both together.\n"
        f"  frontend={frontend}\n  backend ={backend}"
    )

    aliases = [a for a, _ in backend]
    # The engine's alias table (engine.MODELS) and the webapp allow-list (_ALLOWED_MODELS)
    # must cover exactly the same aliases as the registry.
    assert set(_engine.MODELS) == set(aliases), (
        f"engine.MODELS keys {sorted(_engine.MODELS)} != registry aliases {sorted(aliases)}"
    )
    assert set(_webapp._ALLOWED_MODELS) == set(aliases), (
        f"webapp._ALLOWED_MODELS {sorted(_webapp._ALLOWED_MODELS)} != registry aliases {sorted(aliases)}"
    )


def test_ultracode_effort_label_matches_engine():
    """The composer pill must show the effort ultracode ACTUALLY runs at.

    It used to hardcode 'max' while the CLI pins xhigh — a whole rung off (~2.5k vs ~12.5k
    thinking tokens on the same prompt). Same label-vs-reality class as the Opus 5 alias bug.
    """
    import engine as _engine

    src = open(os.path.join(_ROOT, "web", "src", "tabs", "ChatTab.tsx")).read()
    m = re.search(r"const ULTRACODE_EFFORT = '([^']+)'", src)
    assert m, "ChatTab.tsx no longer declares ULTRACODE_EFFORT — the pill label guard is broken"
    assert m.group(1) == _engine.ULTRACODE_EFFORT, (
        f"ChatTab pill shows '{m.group(1)}' but engine pins '{_engine.ULTRACODE_EFFORT}' "
        "while ultracode is on — the UI would advertise an effort the run never uses."
    )
    # The pill must render that constant, not a literal that silently drifts from it.
    assert "ultracode ? ULTRACODE_EFFORT" in src, (
        "the ultracode pill no longer renders ULTRACODE_EFFORT — it can drift from engine again"
    )
