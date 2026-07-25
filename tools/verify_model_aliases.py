#!/usr/bin/env python3
"""Ground-truth check: does every model alias the cockpit offers actually resolve to
the NEWEST model of its family?

Guards against the silent-downgrade class of bug (memory `opus5-alias-staleness-2026-07-24`):
the UI shows a live label ("Opus 5") pulled from /v1/models, but the cockpit sends the
BARE alias `opus` to the SDK, and the *bundled CLI* decides what that alias means. A stale
bundle resolves `opus` to a previous-generation id (e.g. claude-opus-4-8) with
is_error=False — so the label and the model that actually runs silently diverge.

What it does:
  1. Fetch the subscription's /v1/models listing (newest-first) -> newest id per family.
  2. For each family alias, ask the bundled CLI what it ACTUALLY runs
     (`claude -p ... --model <alias> --output-format json` -> the modelUsage key).
  3. Compare. Any alias that runs a non-newest id of its family = mismatch.

Exit codes:
  0  all aliases resolve to the newest id of their family (UI <-> reality match)
  1  MISMATCH — at least one alias runs an older model than the UI advertises
  2  could not verify (no OAuth token / offline / bundled CLI missing)

Run:  venv/bin/python tools/verify_model_aliases.py
Cost: one 1-token completion per family, billed to the subscription (never the API).
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import urllib.request

# Aliases the cockpit offers — must mirror engine.MODELS / webapp._MODEL_FAMILIES.
FAMILIES = ["fable", "sonnet", "opus", "haiku"]
MODELS_URL = "https://api.anthropic.com/v1/models?limit=50"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _oauth_token() -> "str | None":
    try:
        with open(os.path.expanduser("~/.claude/.credentials.json")) as f:
            return json.load(f).get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


def _bundled_cli() -> "str | None":
    hits = glob.glob(os.path.join(
        _REPO_ROOT, "venv/lib/python*/site-packages/claude_agent_sdk/_bundled/claude"))
    return hits[0] if hits else None


def fetch_newest_per_family(token: str) -> "dict[str, dict] | None":
    """Return {family: {id, display_name}} using the newest-first /v1/models listing."""
    req = urllib.request.Request(MODELS_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except Exception as e:  # noqa: BLE001 — offline/expired token must not raise
        print(f"  could not fetch /v1/models: {e}", file=sys.stderr)
        return None
    newest: "dict[str, dict]" = {}
    for m in data.get("data", []):  # API returns newest first
        mid = str(m.get("id", ""))
        for fam in FAMILIES:
            if mid.startswith(f"claude-{fam}") and fam not in newest:
                newest[fam] = {"id": mid, "display_name": m.get("display_name") or mid}
    return newest


def resolve_alias(cli: str, alias: str) -> "str | None":
    """Ask the bundled CLI which model the alias ACTUALLY runs (via the modelUsage key)."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # subscription only — never bill the API
    try:
        out = subprocess.run(
            [cli, "-p", "Reply with exactly: ok", "--model", alias, "--output-format", "json"],
            capture_output=True, text=True, timeout=120, env=env,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  probe failed for '{alias}': {e}", file=sys.stderr)
        return None
    try:
        d = json.loads(out.stdout)
    except Exception:
        return None
    mu = d.get("modelUsage") or {}
    return next(iter(mu.keys())) if mu else d.get("model")


def check() -> "tuple[int, list[tuple[str, str, str]]]":
    """Returns (exit_code, mismatches). mismatches = [(family, advertised_id, actual_id)]."""
    token, cli = _oauth_token(), _bundled_cli()
    if not token:
        print("SKIP: no OAuth token (~/.claude/.credentials.json) — cannot verify.")
        return 2, []
    if not cli:
        print("SKIP: bundled CLI not found under venv — cannot verify.")
        return 2, []
    newest = fetch_newest_per_family(token)
    if not newest:
        print("SKIP: could not fetch live model list — cannot verify.")
        return 2, []

    print(f"{'family':8} {'UI shows (newest id)':30} {'alias actually runs':30} status")
    print("-" * 82)
    mismatches: "list[tuple[str, str, str]]" = []
    for fam in FAMILIES:
        exp = newest.get(fam)
        if not exp:
            print(f"{fam:8} {'(not in /v1/models)':30} {'-':30} skip")
            continue
        actual = resolve_alias(cli, fam) or "?"
        ok = actual == exp["id"]
        print(f"{fam:8} {exp['id']:30} {actual:30} {'OK' if ok else 'MISMATCH'}")
        if not ok:
            mismatches.append((fam, exp["id"], actual))
    print("-" * 82)
    return (1 if mismatches else 0), mismatches


def main() -> int:
    code, mismatches = check()
    if code == 1:
        print(f"\n[FAIL] {len(mismatches)} alias(es) run an OLDER model than the UI advertises:")
        for fam, exp, act in mismatches:
            print(f"   '{fam}': UI/label => {exp}   but actually runs => {act}")
        print("\nFix: bump `claude-agent-sdk` in requirements.txt (the alias->id table ships")
        print("with the bundled CLI), recreate the venv, and restart. See memory")
        print("`opus5-alias-staleness-2026-07-24`.")
    elif code == 0:
        print("\n[OK] every alias resolves to the newest model of its family (UI <-> reality match).")
    return code


if __name__ == "__main__":
    sys.exit(main())
