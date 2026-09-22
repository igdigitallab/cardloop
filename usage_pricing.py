"""usage_pricing.py - Anthropic model pricing + per-turn cost estimation.

Single source of truth for cost math used by the usage dashboard (usage_scanner.py)
and its HTTP endpoints. Prices are Anthropic API list rates ($/MTok) as of September 2026
(https://platform.claude.com/docs/en/about-claude/pricing). cache_write is the 5-minute
write; the 1-hour write costs more and is not modelled here.

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
    # Fable / Mythos — Anthropic's most capable class. cache_read is 0.025x base input on the
    # 5.1 line (a published per-model exception), 0.1x on 5 — not a typo, check the table.
    "claude-fable-5-1":  {"input": 10.00, "output": 50.00, "cache_read": 0.25, "cache_write": 12.50},
    "claude-mythos-5-1": {"input": 10.00, "output": 50.00, "cache_read": 0.25, "cache_write": 12.50},
    "claude-fable-5":    {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50},
    "claude-mythos-5":   {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50},
    # Opus 5.5 is CHEAPER than Opus 5 it replaced, and its cache read is 0.05x base input.
    "claude-opus-5-5":   {"input":  4.00, "output": 20.00, "cache_read": 0.20, "cache_write":  5.00},
    "claude-opus-5":     {"input":  5.00, "output": 25.00, "cache_read": 0.50, "cache_write":  6.25},
    "claude-opus-4-8":   {"input":  5.00, "output": 25.00, "cache_read": 0.50, "cache_write":  6.25},
    "claude-opus-4-7":   {"input":  5.00, "output": 25.00, "cache_read": 0.50, "cache_write":  6.25},
    "claude-opus-4-6":   {"input":  5.00, "output": 25.00, "cache_read": 0.50, "cache_write":  6.25},
    "claude-opus-4-5":   {"input":  5.00, "output": 25.00, "cache_read": 0.50, "cache_write":  6.25},
    # Sonnet 5's $2/$10 launch price became the standard price on 2026-09-01 (the announced
    # rise to $3/$15 was cancelled) — the older Sonnets below stayed at $3/$15.
    "claude-sonnet-5":   {"input":  2.00, "output": 10.00, "cache_read": 0.20, "cache_write":  2.50},
    "claude-sonnet-4-7": {"input":  3.00, "output": 15.00, "cache_read": 0.30, "cache_write":  3.75},
    "claude-sonnet-4-6": {"input":  3.00, "output": 15.00, "cache_read": 0.30, "cache_write":  3.75},
    "claude-sonnet-4-5": {"input":  3.00, "output": 15.00, "cache_read": 0.30, "cache_write":  3.75},
    "claude-haiku-4-7":  {"input":  1.00, "output":  5.00, "cache_read": 0.10, "cache_write":  1.25},
    "claude-haiku-4-6":  {"input":  1.00, "output":  5.00, "cache_read": 0.10, "cache_write":  1.25},
    "claude-haiku-4-5":  {"input":  1.00, "output":  5.00, "cache_read": 0.10, "cache_write":  1.25},
}

# Pricing label surfaced in the UI footer / API payload.
PRICING_AS_OF = "September 2026"

# A model is costed only if its name contains one of these keywords.
_BILLABLE_KEYWORDS = ("fable", "mythos", "opus", "sonnet", "haiku")


def is_local_model(model: str | None) -> bool:
    """True for a model served by a LOCAL backend (spec-092 P3) — always $0.

    Identified by the `name:tag` shape every Ollama/llama.cpp style id carries
    (`qwen3.8:27b-q4_K_M`), which no Anthropic alias or dated id ever has. This is a
    positive test on purpose: the keyword table below matches on substrings, so a local
    model whose name merely CONTAINS "sonnet" (a finetune, a mirror, a vanity tag) would
    otherwise be priced at cloud Sonnet rates and quietly inflate every cost figure in the
    dashboard. The spec named this exact trap.
    """
    if not model:
        return False
    head, sep, tail = model.partition(":")
    return bool(sep and tail and "/" not in tail and not head.startswith("claude-"))


def is_billable(model: str | None) -> bool:
    """True if the model name maps to a known Anthropic price (else cost = n/a)."""
    if not model:
        return False
    if is_local_model(model):
        return False
    m = model.lower()
    return any(k in m for k in _BILLABLE_KEYWORDS)


def get_pricing(model: str | None) -> dict[str, float] | None:
    """Resolve a model id to its price row.

    Exact match first, then a startswith match (dated suffixes like
    `claude-opus-5-5-20260921`), then a keyword fallback onto the newest member
    of each family. Returns None for non-billable / unknown models.
    """
    if not model:
        return None
    if is_local_model(model):
        return None  # a local endpoint costs nothing; never fall through to a keyword match
    if model in PRICING:
        return PRICING[model]
    for key in PRICING:
        if model.startswith(key):
            return PRICING[key]
    m = model.lower()
    if "fable" in m or "mythos" in m:
        return PRICING["claude-fable-5-1"]
    if "opus" in m:
        return PRICING["claude-opus-5-5"]
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
