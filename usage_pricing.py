"""usage_pricing.py - Anthropic model pricing + per-turn cost estimation.

Single source of truth for cost math used by the usage dashboard (usage_scanner.py)
and its HTTP endpoints. Prices are Anthropic API list rates ($/MTok) as of June 2026
(https://claude.com/pricing#api).

NOTE: these are API prices. On a Max/Pro subscription the real cost structure is
flat (per-seat), not per-token — so the dollar figures here are a NOTIONAL, relative
signal ("which model/project/subagent ate the most"), not a literal bill. The same
caveat already applies to engine.append_usage_ledger's cost_usd field.

Cost logic ported from phuryn/claude-usage (MIT, (c) 2026 Pawel Huryn).
Only models whose name contains one of the billable keywords below are costed;
local / unknown models resolve to $0 (shown as n/a).
"""

from __future__ import annotations

# model id -> {input, output, cache_read, cache_write} in USD per million tokens.
PRICING: dict[str, dict[str, float]] = {
    # Fable / Mythos — Anthropic's most capable class, priced at 2x Opus.
    "claude-fable-5":    {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50},
    "claude-mythos-5":   {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50},
    "claude-opus-4-8":   {"input":  5.00, "output": 25.00, "cache_read": 0.50, "cache_write":  6.25},
    "claude-opus-4-7":   {"input":  5.00, "output": 25.00, "cache_read": 0.50, "cache_write":  6.25},
    "claude-opus-4-6":   {"input":  5.00, "output": 25.00, "cache_read": 0.50, "cache_write":  6.25},
    "claude-opus-4-5":   {"input":  5.00, "output": 25.00, "cache_read": 0.50, "cache_write":  6.25},
    "claude-sonnet-5":   {"input":  3.00, "output": 15.00, "cache_read": 0.30, "cache_write":  3.75},
    "claude-sonnet-4-7": {"input":  3.00, "output": 15.00, "cache_read": 0.30, "cache_write":  3.75},
    "claude-sonnet-4-6": {"input":  3.00, "output": 15.00, "cache_read": 0.30, "cache_write":  3.75},
    "claude-sonnet-4-5": {"input":  3.00, "output": 15.00, "cache_read": 0.30, "cache_write":  3.75},
    "claude-haiku-4-7":  {"input":  1.00, "output":  5.00, "cache_read": 0.10, "cache_write":  1.25},
    "claude-haiku-4-6":  {"input":  1.00, "output":  5.00, "cache_read": 0.10, "cache_write":  1.25},
    "claude-haiku-4-5":  {"input":  1.00, "output":  5.00, "cache_read": 0.10, "cache_write":  1.25},
}

# Pricing label surfaced in the UI footer / API payload.
PRICING_AS_OF = "June 2026"

# A model is costed only if its name contains one of these keywords.
_BILLABLE_KEYWORDS = ("fable", "mythos", "opus", "sonnet", "haiku")


def is_billable(model: str | None) -> bool:
    """True if the model name maps to a known Anthropic price (else cost = n/a)."""
    if not model:
        return False
    m = model.lower()
    return any(k in m for k in _BILLABLE_KEYWORDS)


def get_pricing(model: str | None) -> dict[str, float] | None:
    """Resolve a model id to its price row.

    Exact match first, then a startswith match (dated suffixes like
    `claude-opus-4-8-20260115`), then a keyword fallback onto the newest member
    of each family. Returns None for non-billable / unknown models.
    """
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    for key in PRICING:
        if model.startswith(key):
            return PRICING[key]
    m = model.lower()
    if "fable" in m or "mythos" in m:
        return PRICING["claude-fable-5"]
    if "opus" in m:
        return PRICING["claude-opus-4-8"]
    if "sonnet" in m:
        return PRICING["claude-sonnet-5"]
    if "haiku" in m:
        return PRICING["claude-haiku-4-5"]
    return None


def calc_cost(model: str | None, inp: int, out: int,
              cache_read: int, cache_creation: int) -> float:
    """Notional USD cost for one (model, token-counts) bucket. 0.0 if not billable."""
    p = get_pricing(model)
    if not p:
        return 0.0
    return (
        (inp or 0)            * p["input"]       / 1_000_000 +
        (out or 0)            * p["output"]      / 1_000_000 +
        (cache_read or 0)     * p["cache_read"]  / 1_000_000 +
        (cache_creation or 0) * p["cache_write"] / 1_000_000
    )
