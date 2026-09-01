"""Which model actually ran — measured per turn, not assumed from the alias.

The cockpit sends a bare alias and the bundled CLI decides what it means, so the label and
reality can diverge silently (`is_error=False`). Two defenses existed: a family-level check
from the init message (fires once per live-client lifetime, and "opus" matches
"claude-opus-4-8", so it is blind to generation staleness) and a daily paid cron. This adds the
per-turn reading, and it must survive the multi-model `model_usage` map that CLI 2.1.252
introduced — helper Haiku traffic is billed alongside the turn's own model and lands first.
"""
import engine
import webapp


# ───────────────────────────── picking the turn's own model ─────────────────────────────────────

def test_helper_haiku_does_not_masquerade_as_the_turn_model():
    mu = {"claude-haiku-4-5-20251001": {"outputTokens": 9},
          "claude-sonnet-5": {"outputTokens": 4}}
    assert engine._pick_served_model(mu, "sonnet") == "claude-sonnet-5"


def test_a_full_model_id_works_as_the_requested_value():
    """Sub-agents are launched with an explicit id, not an alias."""
    mu = {"claude-haiku-4-5-20251001": {"outputTokens": 9},
          "claude-opus-5": {"outputTokens": 40}}
    assert engine._pick_served_model(mu, "claude-opus-5") == "claude-opus-5"


def test_haiku_alias_keeps_its_own_entry():
    mu = {"claude-haiku-4-5-20251001": {"outputTokens": 9}}
    assert engine._pick_served_model(mu, "haiku") == "claude-haiku-4-5-20251001"


def test_a_turn_that_ran_another_family_reports_the_heaviest_entry():
    """No entry from the requested family = a real substitution; do not hide it."""
    mu = {"claude-haiku-4-5-20251001": {"outputTokens": 9},
          "claude-opus-4-8": {"outputTokens": 40}}
    assert engine._pick_served_model(mu, "sonnet") == "claude-opus-4-8"


def test_no_usage_map_degrades_to_none():
    assert engine._pick_served_model(None, "opus") is None
    assert engine._pick_served_model({}, "opus") is None


def test_a_non_dict_usage_value_is_treated_as_no_data():
    """An older SDK types model_usage as Any, and a test double can be anything truthy —
    max() over a non-mapping would raise INSIDE the turn."""
    assert engine._pick_served_model(object(), "opus") is None
    assert engine._pick_served_model("not-a-map", "opus") is None


def test_missing_output_tokens_do_not_crash_the_pick():
    mu = {"claude-haiku-4-5-20251001": {}, "claude-opus-4-8": {"outputTokens": 3}}
    assert engine._pick_served_model(mu, "sonnet") == "claude-opus-4-8"


# ─────────────────────────────── the generation-level check ─────────────────────────────────────

def _warm_cache(monkeypatch, ids):
    monkeypatch.setitem(webapp._models_cache, "live", [{"id": i} for i in ids])


def test_stale_generation_is_reported(monkeypatch):
    _warm_cache(monkeypatch, ["claude-opus-5", "claude-opus-4-8"])
    out = webapp._check_model_freshness("opus", "claude-opus-4-8")
    assert out == {"requested": "opus", "served": "claude-opus-4-8",
                   "expected": "claude-opus-5"}


def test_newest_generation_is_silent(monkeypatch):
    _warm_cache(monkeypatch, ["claude-opus-5", "claude-opus-4-8"])
    assert webapp._check_model_freshness("opus", "claude-opus-5") is None


def test_a_cold_cache_never_alerts(monkeypatch):
    monkeypatch.setitem(webapp._models_cache, "live", None)
    assert webapp._check_model_freshness("opus", "claude-opus-4-8") is None


def test_absent_canonical_model_never_alerts(monkeypatch):
    """Older CLI or a third-party provider: no canonicalModel = no opinion, not an alarm."""
    _warm_cache(monkeypatch, ["claude-opus-5"])
    assert webapp._check_model_freshness("opus", None) is None
    assert webapp._check_model_freshness(None, "claude-opus-5") is None


def test_an_unknown_family_never_alerts(monkeypatch):
    _warm_cache(monkeypatch, ["claude-opus-5"])
    assert webapp._check_model_freshness("sonnet", "claude-sonnet-5") is None


# ───────────────────────────────── wiring stays in place ────────────────────────────────────────

def test_result_event_carries_the_per_turn_identity():
    src = open("engine.py").read()
    for field in ('"canonical_served": _canonical_served',
                  '"model_usage": _mu or None',
                  '"model_requested": resolved_model'):
        assert field in src, field


def test_both_consumers_run_the_freshness_check():
    """Chat has had a strip since July; cards had no alert path at all until now."""
    src = open("webapp.py").read()
    assert src.count("_check_model_freshness(") >= 3  # definition + chat + card
    assert 'event="model_stale"' in src
    assert '"generation": True' in src
