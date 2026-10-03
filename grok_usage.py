"""Readers/aggregator for the Grok ledgers (spec-095 phase P4: D9, §5.9, M13).

Pure logic over two append-only files the ENGINE writes (``grok_engine._append_usage`` and
``_capture_limit_error``) in the cockpit data dir:

* ``grok_usage.jsonl``        one row per turn: ``ts, provider, session_id, project, session_key,
                              entrypoint, model, input, output, cached, reasoning, total,
                              duration_ms, notional_usd``
* ``grok_limit_errors.jsonl`` the RAW text of anything quota-shaped: ``ts, source, session_id,
                              project, model, text``

What it can and cannot say (D9, M13):

* xAI reports NO rate-limit / remaining-quota signal, so ``limits`` is always ``None`` and the UI
  shows "limits not reported" - never a bar. The ``local_counters`` are OUR OWN count of turns and
  tokens in the last 5 h / 7 d from the row timestamps: a hint about burn rate, not a quota.
* ``input`` INCLUDES the cache reads (``cached`` is a subset of it, same convention as Codex), so
  nothing here adds ``cached`` on top of ``input``.
* ``notional_usd`` is what the same tokens would cost at API list prices. The plan is a flat
  subscription, so it is exposed under that name only and never summed into anything called
  cost/spend.

Shapes: ``usage_rows`` returns the rows as the engine wrote them (like
``codex_engine.usage_rows``). ``aggregate`` is the block the Usage tab reads as ``providers.grok``;
``by_model`` is a RECORD keyed by model id (the frontend's ``normalizeProviderUsage`` reconciles it
with Codex's array shape).

Robustness: a missing, half-written or garbage file never raises - bad lines are skipped. Reads are
bounded (the newest ``MAX_USAGE_BYTES`` of a huge ledger) and symlink/FIFO-proof (``grok_jsonl``),
but still blocking file IO: the web layer calls these in an executor thread.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import grok_jsonl

USAGE_FILE = "grok_usage.jsonl"
LIMIT_ERRORS_FILE = "grok_limit_errors.jsonl"

# --- knobs (module constants so tests can shrink them) -----------------------------------------
# ~300 B per usage row: 16 MiB is ~53k turns, above the 10 MiB at which `doctor` already tells the
# operator to archive the ledger, so a truncated read only ever happens after that warning.
MAX_USAGE_BYTES = 16 * 1024 * 1024
MAX_LIMIT_ERROR_BYTES = 4 * 1024 * 1024   # rows are up to ~8000 chars of text
MAX_USAGE_LINE_BYTES = 256 * 1024
MAX_LIMIT_ERROR_LINE_BYTES = 256 * 1024
MAX_MODELS = 64                           # distinct by_model keys; the rest is folded into "other"
MODEL_NAME_CHARS = 100
LIMIT_ERROR_TEXT_CHARS = 8000             # == grok_engine.LIMIT_ERROR_MAX_CHARS (pinned by a test)
MAX_LIMIT_ERRORS = 200

FIVE_HOUR_SEC = 5 * 3600
SEVEN_DAY_SEC = 7 * 86400
DAY_SEC = 86400


# ------------------------------------------------------------------------------------------
# value hygiene
# ------------------------------------------------------------------------------------------

def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _is_ts(x) -> bool:
    return _is_number(x) and x >= 0


def _count(x) -> int:
    """A token/turn count from a ledger value: a finite non-negative number, else 0."""
    return int(x) if _is_number(x) and x > 0 else 0


def _valid_days(days) -> bool:
    return _is_number(days) and days > 0


def _str_or_none(x) -> "str | None":
    return x if isinstance(x, str) else None


def _model_name(row: dict) -> str:
    model = row.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()[:MODEL_NAME_CHARS]
    return "unknown"


# ------------------------------------------------------------------------------------------
# usage ledger
# ------------------------------------------------------------------------------------------

def usage_rows(data_dir, *, days: "float | None" = None, now: "float | None" = None) -> list[dict]:
    """Rows of ``<data_dir>/grok_usage.jsonl`` with ``ts >= now - days*86400`` (all of them when
    ``days`` is falsy/invalid), oldest first, as written by the engine.

    Same contract as ``codex_engine.usage_rows``, plus: never raises, a line that is not a JSON
    object or has no usable ``ts`` is skipped, and only the newest ``MAX_USAGE_BYTES`` are read.
    ``now`` is injectable for tests.
    """
    if data_dir is None:
        return []
    cutoff = None
    if _valid_days(days):
        cutoff = (time.time() if now is None else now) - days * DAY_SEC
    rows: list[dict] = []
    try:
        for _offset, row in grok_jsonl.iter_jsonl(Path(data_dir) / USAGE_FILE, max_bytes=MAX_USAGE_BYTES,
                                                  max_line=MAX_USAGE_LINE_BYTES):
            ts = row.get("ts")
            if not _is_ts(ts) or (cutoff is not None and ts < cutoff):
                continue
            rows.append(row)
    except OSError:
        pass  # a read that dies half way keeps what it already parsed
    return rows


def aggregate(rows, *, days: "float | None" = None, now: "float | None" = None,
              last_limit_error: "dict | None" = None) -> dict:
    """The ``providers.grok`` block of ``/api/usage/dashboard``.

    ``rows`` is the ledger as ``usage_rows`` returns it - pass at least the last 7 days even when
    ``days`` is shorter: ``days`` narrows only the TOTALS and ``by_model``, while ``local_counters``
    always look at 5 h / 7 d from ``now`` over every row given. A turn exactly 5 h (7 d) old still
    counts; one a second older does not; a turn stamped in the future (clock step) counts.
    """
    now = time.time() if now is None else now
    cutoff = now - days * DAY_SEC if _valid_days(days) else None
    five_from, seven_from = now - FIVE_HOUR_SEC, now - SEVEN_DAY_SEC
    turns = inp = out = cached = reasoning = 0
    notional = 0.0
    priced = False
    by_model: dict[str, dict] = {}
    five = {"turns": 0, "tokens": 0}
    seven = {"turns": 0, "tokens": 0}
    try:
        stream = iter(rows)
    except TypeError:
        stream = iter(())
    for row in stream:
        if not isinstance(row, dict):
            continue
        ts = row.get("ts")
        if not _is_ts(ts):
            continue  # a row that cannot be placed in time is counted nowhere
        r_in, r_out = _count(row.get("input")), _count(row.get("output"))
        if ts >= seven_from:
            seven["turns"] += 1
            seven["tokens"] += r_in + r_out
        if ts >= five_from:
            five["turns"] += 1
            five["tokens"] += r_in + r_out
        if cutoff is not None and ts < cutoff:
            continue
        turns += 1
        inp += r_in
        out += r_out
        cached += _count(row.get("cached"))
        reasoning += _count(row.get("reasoning"))
        usd = row.get("notional_usd")
        if _is_number(usd) and usd >= 0:
            notional += usd
            priced = True
        name = _model_name(row)
        if name not in by_model and len(by_model) >= MAX_MODELS:
            name = "other"
        bucket = by_model.setdefault(name, {"turns": 0, "input": 0, "output": 0})
        bucket["turns"] += 1
        bucket["input"] += r_in
        bucket["output"] += r_out
    return {
        "turns": turns, "input": inp, "output": out, "cached": cached, "reasoning": reasoning,
        # API-equivalent price of the same tokens - NOT spend (flat subscription). None = unpriced.
        "notional_usd": round(notional, 6) if priced else None,
        "by_model": dict(sorted(by_model.items(), key=lambda kv: (-kv[1]["turns"], kv[0]))),
        "limits": None,  # D9/M13: xAI reports no subscription window; unknown is never a green bar
        "local_counters": {"five_hour": five, "seven_day": seven},
        "last_limit_error": _limit_view(last_limit_error),
    }


def summary(data_dir, *, days: "float | None" = None, now: "float | None" = None) -> dict:
    """``aggregate`` over the ledger of ``data_dir`` with the newest captured limit error - the one
    call the usage endpoint makes. Reads at least 7 days of rows so the counters stay right when
    the dashboard asks for a shorter window."""
    now = time.time() if now is None else now
    span = max(days, 7) if _valid_days(days) else None
    errors = limit_errors(data_dir, limit=1)
    return aggregate(usage_rows(data_dir, days=span, now=now), days=days, now=now,
                     last_limit_error=errors[0] if errors else None)


# ------------------------------------------------------------------------------------------
# limit-error capture (M13: the shape of a real quota error is unknown, the text is kept raw)
# ------------------------------------------------------------------------------------------

def _limit_view(row) -> "dict | None":
    """``{ts, text}`` of a captured error, or None when the row has no usable text/time."""
    if not isinstance(row, dict):
        return None
    ts, text = row.get("ts"), row.get("text")
    if not _is_ts(ts) or not isinstance(text, str) or not text.strip():
        return None
    return {"ts": float(ts), "text": text[:LIMIT_ERROR_TEXT_CHARS]}


def limit_errors(data_dir, *, limit: int = 20) -> list[dict]:
    """The newest ``limit`` rows of ``grok_limit_errors.jsonl``, newest first, as
    ``{ts, source, session_id, project, model, text}`` (a missing/non-text field is None; a row
    without a usable ``ts`` or ``text`` is skipped). Never raises."""
    if data_dir is None:
        return []
    n = max(1, min(limit, MAX_LIMIT_ERRORS)) if isinstance(limit, int) and not isinstance(limit, bool) else 20
    found: list[dict] = []
    try:
        for _offset, row in grok_jsonl.iter_jsonl(Path(data_dir) / LIMIT_ERRORS_FILE,
                                                  max_bytes=MAX_LIMIT_ERROR_BYTES,
                                                  max_line=MAX_LIMIT_ERROR_LINE_BYTES):
            view = _limit_view(row)
            if view is None:
                continue
            view.update({"source": _str_or_none(row.get("source")),
                         "session_id": _str_or_none(row.get("session_id")),
                         "project": _str_or_none(row.get("project")),
                         "model": _str_or_none(row.get("model"))})
            found.append(view)
    except OSError:
        pass
    found.reverse()  # equal stamps: the later line is the newer one
    found.sort(key=lambda r: r["ts"], reverse=True)  # stable
    return found[:n]
