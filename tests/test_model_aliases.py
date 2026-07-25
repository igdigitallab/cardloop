"""Opt-in ground-truth test: every model alias the cockpit offers must resolve to the
NEWEST model of its family, so the UI label and the model that actually runs never
silently diverge (memory `opus5-alias-staleness-2026-07-24`).

Excluded from the default `pytest tests/` run — it needs the subscription OAuth token
plus a real bundled-CLI probe per family (one 1-token completion each). Run explicitly:

    venv/bin/python -m pytest tests/test_model_aliases.py -m aliases

Skips (does not fail) when offline / no token / no bundled CLI, so it is safe to wire
into CI, a deploy canary, or a scheduled check without flaking in restricted envs.
"""
import importlib.util
import os

import pytest

_TOOL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools", "verify_model_aliases.py",
)


def _load_verifier():
    spec = importlib.util.spec_from_file_location("verify_model_aliases", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.aliases
def test_every_alias_runs_the_newest_model_of_its_family():
    v = _load_verifier()
    token, cli = v._oauth_token(), v._bundled_cli()
    if not token or not cli:
        pytest.skip("no OAuth token or bundled CLI — cannot verify alias resolution")
    newest = v.fetch_newest_per_family(token)
    if not newest:
        pytest.skip("could not fetch live /v1/models — cannot verify")

    mismatches = []
    for fam in v.FAMILIES:
        exp = newest.get(fam)
        if not exp:
            continue
        actual = v.resolve_alias(cli, fam)
        if actual != exp["id"]:
            mismatches.append(f"'{fam}': UI advertises {exp['id']} but the alias runs {actual}")

    assert not mismatches, (
        "UI label <-> actual model mismatch — the bundled CLI is stale; bump "
        "claude-agent-sdk in requirements.txt:\n  " + "\n  ".join(mismatches)
    )
