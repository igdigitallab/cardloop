#!/usr/bin/env python3
"""Daily activity journal — Haiku digest of the day's Cardloop cockpit work.

Scans everything the operator did through Cardloop on a given local calendar
day — every project (topic-bound or ad-hoc "free chat"), IT or non-IT — and
writes one Markdown diary entry into the operator's Obsidian vault
(``~/vault/Journal/YYYY-MM-DD.md`` by default), plus a newest-first
``_index.md`` line. A small model (Haiku, via the Claude Agent SDK on the
operator's subscription) turns a bounded, fact-only EVIDENCE DIGEST into
readable prose; the frontmatter, title, and every number in it are computed
by this script, never by the model.

Data sources (all read-only, all local to this host):
  1. data/registry.json + data/topics.json          — project id -> cwd/label
  2. ~/.claude/projects/<slug>/*.jsonl               — SDK session transcripts
  3. data/usage_ledger.jsonl                         — per-turn cost/time ledger
  4. `git log` in each project cwd                   — commits shipped that day
  5. TASKS.md / DONE.md per project cwd              — board cards closed that day
  6. docs/internal/specs/*.md (+ ~/vault/01-Projects/*/specs/*.md) mtimes
  7. optional: `<vault>/YYYY-MM-DD<suffix>.md` — the operator's own notes for the day
     (default suffix `-notes`; `--notes-suffix` / $JOURNAL_NOTES_SUFFIX). Printed
     VERBATIM, by code, in a section above the generated chronology and in the
     Telegram ping; the model only ever sees them as context. No file, or an empty
     one -> the output is byte-for-byte what it was without this input.

Safety:
  - Never overwrites a hand-written vault note: a generated note always
    carries frontmatter `generated: cardloop-daily-journal`; a file at the
    primary path that lacks that marker is left untouched and the note is
    written to `YYYY-MM-DD-cardloop.md` instead (see `choose_note_path`).
  - The notes file is read-only for this tool: it is never created, edited or deleted,
    and a file carrying our own `generated:` marker is never read back as notes.
  - The model call is read-only (no tools, one turn) and hard-timed out
    (`--model-timeout`, default 180s) so a hung SDK call cannot wedge a cron.
  - `ANTHROPIC_API_KEY` is popped before the model call — this tool always
    rides the operator's Claude subscription, never pay-per-token billing
    (mirrors bot.py's CLAUDE_AUTH_MODE=subscription guard).

Usage:
    venv/bin/python tools/daily-journal.py                  # yesterday (LA)
    venv/bin/python tools/daily-journal.py --date 2026-08-30
    venv/bin/python tools/daily-journal.py --days 7          # backfill last 7 days
    venv/bin/python tools/daily-journal.py --dry-run --no-model --date 2026-08-30
    venv/bin/python tools/daily-journal.py --notes-suffix=-notes   # operator-notes file suffix (note the `=`)

Exit code 1 (with a `[daily-journal]`-prefixed message on stderr) on any
day that fails to generate; 0 otherwise.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

LOG_PREFIX = "[daily-journal]"

DEFAULT_TZ = "America/Los_Angeles"
# Hard cap on the evidence digest handed to the model. Every turn row is
# already clipped to PROMPT_SNIPPET_CHARS + REPLY_SNIPPET_CHARS (~720 chars), so
# a 160-turn day is ~115k chars — well inside Haiku's context. The old 40k cap
# silently dropped the oldest 60-80% of turns on any busy day, so the note
# described only the evening.
DIGEST_CHAR_BUDGET = 150_000
PROMPT_SNIPPET_CHARS = 300
REPLY_SNIPPET_CHARS = 400
MODEL_CALL_TIMEOUT_S = 180
GENERATED_MARKER = "cardloop-daily-journal"
SOURCE_MARKER = "cardloop"

# ─────────────────────────────────────────────────────────────────────────────
# Service-noise filtering — mirrors webapp._strip_service_blocks / _display_prompt
# / _BG_CONTINUE_PREFIX / _CROSS_AGENT_MSG_RE exactly, EXCEPT for one deliberate
# divergence: the cockpit feed keeps a cross-session teammate delivery (isMeta but
# matching _CROSS_AGENT_MSG_RE) so the operator can see it happened; this journal
# is about what the OPERATOR personally did, so a cross-agent block never counts
# as an operator prompt here, isMeta or not.
# ─────────────────────────────────────────────────────────────────────────────

_SERVICE_BLOCK_RE = re.compile(
    r"<(?P<tag>task-notification|prior-session-summary|context-pack|system-reminder"
    r"|command-name|command-message|command-args)"
    r"[^>]*>.*?</(?P=tag)>",
    re.DOTALL | re.IGNORECASE,
)
_BG_CONTINUE_PREFIX = "[auto-continue]"
_CROSS_AGENT_MSG_RE = re.compile(r"<(?:agent|teammate)-message\b", re.IGNORECASE)


def clean_operator_prompt(text: "str | None") -> str:
    """Human-safe copy of an operator-authored string, or "" if it is pure noise.

    Drops: an auto-continue wake-up (whole), any cross-agent/teammate delivery
    (whole), and embedded SDK service XML blocks (surrounding text survives).
    """
    if not text:
        return ""
    if text.startswith(_BG_CONTINUE_PREFIX):
        return ""
    if _CROSS_AGENT_MSG_RE.search(text):
        return ""
    return _SERVICE_BLOCK_RE.sub("", text).strip()


def iso_to_epoch_ms(ts_raw: "str | None") -> "int | None":
    """SDK transcript lines carry an ISO-8601 "timestamp"; convert to epoch ms."""
    if not ts_raw:
        return None
    try:
        s = str(ts_raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def extract_operator_line(obj: dict) -> "str | None":
    """Cleaned operator-authored text for one parsed transcript JSON line, or
    None if the line carries no human-typed content.

    Covers: type=="user" with string content; type=="user" with a list of
    text-only blocks (a list containing a tool_result block is a tool reply,
    never a human one — skipped whole); and type=="attachment" with
    attachment.type=="queued_command" — a mid-turn steer, which the CLI never
    gives a plain "user" line (spec-086: see webapp._session_history).
    """
    t = obj.get("type")
    if t == "attachment":
        att = obj.get("attachment")
        if isinstance(att, dict) and att.get("type") == "queued_command":
            cleaned = clean_operator_prompt(att.get("prompt") or "")
            return cleaned or None
        return None
    if t != "user":
        return None
    if obj.get("isMeta") is True:
        return None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        raw = "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        )
    else:
        return None
    cleaned = clean_operator_prompt(raw)
    return cleaned or None


def extract_assistant_text(obj: dict) -> str:
    """Concatenated TextBlock text of an assistant transcript line, or ""."""
    if obj.get("type") != "assistant":
        return ""
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        b.get("text", "") for b in content
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
    ]
    return "\n".join(parts).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Slugs — two DIFFERENT schemes exist in this codebase for two different dirs;
# mixing them up silently makes a directory look empty. See webapp.py:
#   _sdk_sessions_dir      -> re.sub(r"[^a-zA-Z0-9]", "-", cwd)   (SDK transcripts)
#   _timeline_slug_from_cwd -> cwd.replace("/", "-")              (data/timeline/)
# ─────────────────────────────────────────────────────────────────────────────

def sdk_session_slug(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def timeline_slug(cwd: str) -> str:
    return cwd.replace("/", "-")


# ─────────────────────────────────────────────────────────────────────────────
# LA-day -> UTC bounds (pure, DST-safe: wall-clock date arithmetic, not UTC math,
# so a spring-forward day is naturally 23h and a fall-back day 25h in UTC terms).
# ─────────────────────────────────────────────────────────────────────────────

def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def la_day_bounds_utc(day: date, tz_name: str = DEFAULT_TZ) -> "tuple[datetime, datetime]":
    tz = ZoneInfo(tz_name)
    start_local = datetime(day.year, day.month, day.day, tzinfo=tz)
    end_local = datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def ms_to_local_hhmm(ts_ms: int, tz_name: str) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz_name))
    return dt.strftime("%H:%M")


def compute_target_days(date_str: "str | None", days_n: "int | None", yesterday: date) -> "list[date]":
    """Pure day-selection: --date wins outright; else the last N days ending
    yesterday (N=1 with neither flag, i.e. "yesterday")."""
    if date_str:
        return [parse_date(date_str)]
    n = days_n if days_n and days_n > 0 else 1
    return [yesterday - timedelta(days=i) for i in range(n)]


# ─────────────────────────────────────────────────────────────────────────────
# Turn model — one operator prompt + the last assistant text that followed it
# before the next operator prompt, within a time window.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Turn:
    ts_ms: int
    prompt: str
    reply: str = ""


def turns_from_lines(lines: "list[dict]", start_ms: int, end_ms: int) -> "list[Turn]":
    """Pairs each operator prompt with the LAST assistant text that follows it
    (before the next operator prompt), restricted to [start_ms, end_ms).
    `lines` must be time-ordered (as SDK transcripts naturally are)."""
    turns: "list[Turn]" = []
    current: "Turn | None" = None
    for obj in lines:
        ts_ms = iso_to_epoch_ms(obj.get("timestamp"))
        if ts_ms is None or not (start_ms <= ts_ms < end_ms):
            continue
        prompt = extract_operator_line(obj)
        if prompt is not None:
            if current is not None:
                turns.append(current)
            current = Turn(ts_ms=ts_ms, prompt=prompt[:PROMPT_SNIPPET_CHARS])
            continue
        if current is not None:
            reply = extract_assistant_text(obj)
            if reply:
                current.reply = reply[:REPLY_SNIPPET_CHARS]
    if current is not None:
        turns.append(current)
    return turns


def iter_jsonl(path: Path):
    """Streams a JSONL file one parsed object at a time. Never loads the whole
    file into memory; malformed lines and I/O errors are skipped silently."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except OSError:
        return


def read_transcript_turns(cwd: str, start_utc: datetime, end_utc: datetime) -> "tuple[list[Turn], int]":
    """Turns for every SDK session transcript under cwd's slug dir, plus how
    many distinct session files contributed at least one turn ("sessions")."""
    sdk_dir = Path.home() / ".claude" / "projects" / sdk_session_slug(cwd)
    if not sdk_dir.is_dir():
        return [], 0
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(end_utc.timestamp() * 1000)
    all_turns: "list[Turn]" = []
    session_count = 0
    for jf in sorted(sdk_dir.glob("*.jsonl")):
        try:
            # A file whose last write predates the window cannot contain a line
            # inside it — skip without opening. (Files only ever get appended.)
            if jf.stat().st_mtime * 1000 < start_ms:
                continue
        except OSError:
            continue
        file_turns = turns_from_lines(list(iter_jsonl(jf)), start_ms, end_ms)
        if file_turns:
            session_count += 1
            all_turns.extend(file_turns)
    all_turns.sort(key=lambda t: t.ts_ms)
    return all_turns, session_count


# ─────────────────────────────────────────────────────────────────────────────
# data/timeline/*.jsonl — run start/end + outcome events (epoch-seconds `ts`).
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TimelineEvent:
    ts: float
    session_key: str
    kind: str
    text: str = ""
    prompt: str = ""
    outcome: str = ""
    run_id: str = ""
    chat_id: str = ""


def parse_timeline_events(lines: "list[dict]", start_ts: float, end_ts: float) -> "list[TimelineEvent]":
    out: "list[TimelineEvent]" = []
    for obj in lines:
        ts = obj.get("ts")
        if not isinstance(ts, (int, float)) or not (start_ts <= ts < end_ts):
            continue
        kind = obj.get("kind") or obj.get("type") or ""
        out.append(TimelineEvent(
            ts=float(ts), session_key=str(obj.get("session_key") or ""), kind=str(kind),
            text=obj.get("text") or "", prompt=obj.get("prompt") or "",
            outcome=obj.get("outcome") or "", run_id=str(obj.get("run_id") or ""),
            chat_id=str(obj.get("chat_id") or ""),
        ))
    return out


def read_timeline_file(path: Path, start_ts: float, end_ts: float) -> "list[TimelineEvent]":
    if not path.exists():
        return []
    return parse_timeline_events(list(iter_jsonl(path)), start_ts, end_ts)


def turns_from_timeline_events(events: "list[TimelineEvent]") -> "list[Turn]":
    """Builds Turn rows for a free chat purely from its timeline events (no raw
    SDK transcript join — see the design note in gather_day): run_start.prompt
    is the operator prompt, a later `text`/run_end for the same run_id is the
    outcome/reply, and a `steer` becomes its own operator-authored row."""
    events = sorted(events, key=lambda e: e.ts)
    by_run: "dict[str, Turn]" = {}
    order: "list[str]" = []
    extra: "list[Turn]" = []
    for ev in events:
        if ev.kind == "run_start":
            prompt = clean_operator_prompt(ev.prompt)
            if not prompt:
                continue
            t = Turn(ts_ms=int(ev.ts * 1000), prompt=prompt[:PROMPT_SNIPPET_CHARS])
            by_run[ev.run_id] = t
            order.append(ev.run_id)
        elif ev.kind == "steer":
            txt = clean_operator_prompt(ev.text)
            if txt:
                extra.append(Turn(ts_ms=int(ev.ts * 1000), prompt=f"[steer] {txt[:PROMPT_SNIPPET_CHARS]}"))
        elif ev.kind == "text" and ev.run_id in by_run:
            if ev.text:
                by_run[ev.run_id].reply = ev.text[:REPLY_SNIPPET_CHARS]
        elif ev.kind == "run_end" and ev.run_id in by_run:
            t = by_run[ev.run_id]
            if not t.reply:
                t.reply = f"[{ev.outcome or 'done'}]"
    turns = [by_run[rid] for rid in order] + extra
    turns.sort(key=lambda t: t.ts_ms)
    return turns


# ─────────────────────────────────────────────────────────────────────────────
# Project / free-chat registry
# ─────────────────────────────────────────────────────────────────────────────

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def home_sub(rel: str) -> str:
    return str(Path.home().joinpath(rel))


@dataclass
class ProjectInfo:
    project_id: str
    cwd: str
    label: str
    model: str = ""
    source: str = "topic"  # topic | registry


def load_projects(data_dir: Path) -> "dict[str, ProjectInfo]":
    """id -> ProjectInfo. Primary source: data/topics.json (LAYER 1 project
    bindings — id, cwd, display name). Secondary: data/registry.json (id ->
    folder-under-$HOME aliases) fills in any id that has no topic binding and
    whose resolved cwd is not already covered, e.g. a project only ever
    reached by typed alias."""
    topics = load_json(data_dir / "topics.json", {})
    out: "dict[str, ProjectInfo]" = {}
    # `"private": true` on a topic keeps that project OUT of the digest entirely (personal
    # therapy sessions must never be summarised into the vault journal or the Telegram
    # report). Its cwd is also remembered so the registry fallback below cannot re-add it.
    private_cwds: "set[str]" = set()
    if isinstance(topics, dict):
        for pid, t in topics.items():
            if not isinstance(t, dict):
                continue
            cwd = t.get("cwd")
            if not cwd:
                continue
            if t.get("private"):
                private_cwds.add(cwd)
                continue
            out[pid] = ProjectInfo(
                project_id=pid, cwd=cwd, label=t.get("project") or pid,
                model=t.get("model") or "", source="topic",
            )
    registry = load_json(data_dir / "registry.json", {})
    known_cwds = {p.cwd for p in out.values()} | private_cwds
    if isinstance(registry, dict):
        for alias, rel in registry.items():
            if alias in out or not isinstance(rel, str):
                continue
            cwd = home_sub(rel)
            if cwd in known_cwds:
                continue
            out[alias] = ProjectInfo(project_id=alias, cwd=cwd, label=Path(cwd).name, source="registry")
            known_cwds.add(cwd)
    return out


@dataclass
class FreeChatInfo:
    session_key: str
    label: str
    cwd: str
    model: str = ""


def load_free_chats(data_dir: Path) -> "dict[str, FreeChatInfo]":
    raw = load_json(data_dir / "free_chats.json", {})
    out: "dict[str, FreeChatInfo]" = {}
    if isinstance(raw, dict):
        for key, v in raw.items():
            if not isinstance(v, dict):
                continue
            out[key] = FreeChatInfo(
                session_key=key, label=v.get("label") or key,
                cwd=v.get("cwd") or "", model=v.get("model") or "",
            )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# data/usage_ledger.jsonl — per-turn cost/time ledger (source #3)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LedgerStats:
    turns: int = 0
    duration_ms: int = 0
    cost_usd: float = 0.0
    models: "dict[str, int]" = field(default_factory=dict)
    first_ts: "float | None" = None
    last_ts: "float | None" = None


def _accumulate_ledger(stats: LedgerStats, obj: dict, ts: float) -> None:
    stats.turns += 1
    dur = obj.get("duration_ms")
    if isinstance(dur, (int, float)):
        stats.duration_ms += int(dur)
    cost = obj.get("cost_usd")
    if isinstance(cost, (int, float)):
        stats.cost_usd += float(cost)
    model = obj.get("model")
    if isinstance(model, str) and model:
        stats.models[model] = stats.models.get(model, 0) + 1
    stats.first_ts = ts if stats.first_ts is None else min(stats.first_ts, ts)
    stats.last_ts = ts if stats.last_ts is None else max(stats.last_ts, ts)


def read_usage_ledger(data_dir: Path, start_ts: float, end_ts: float) -> "tuple[LedgerStats, dict[str, LedgerStats]]":
    """Whole-day totals plus a per-project-id breakdown (id = usage_ledger's
    "project" field, the same short id used as a data/topics.json key)."""
    total = LedgerStats()
    per_project: "dict[str, LedgerStats]" = {}
    for obj in iter_jsonl(data_dir / "usage_ledger.jsonl"):
        ts = obj.get("ts")
        if not isinstance(ts, (int, float)) or not (start_ts <= ts < end_ts):
            continue
        _accumulate_ledger(total, obj, ts)
        pid = obj.get("project")
        if isinstance(pid, str) and pid:
            _accumulate_ledger(per_project.setdefault(pid, LedgerStats()), obj, ts)
    return total, per_project


# ─────────────────────────────────────────────────────────────────────────────
# Git commits (source #4)
# ─────────────────────────────────────────────────────────────────────────────

def read_git_commits(cwd: str, start_utc: datetime, end_utc: datetime) -> "list[dict]":
    if not (Path(cwd) / ".git").exists():
        return []
    since = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    until = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        proc = subprocess.run(
            ["git", "log", f"--since={since}", f"--until={until}",
             "--format=%h%x1f%ad%x1f%s", "--date=format:%H:%M"],
            cwd=cwd, capture_output=True, text=True, timeout=15, check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    commits = []
    for line in proc.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            commits.append({"hash": parts[0], "time": parts[1], "subject": parts[2]})
    return commits


# ─────────────────────────────────────────────────────────────────────────────
# Board cards (source #5) — only DONE.md carries a per-card date stamp
# ("- [x] text <!--ops:ID--> · YYYY-MM-DD"); TASKS.md tracks open cards with
# no date, so it is not a signal for "what moved today" and is not read here.
# ─────────────────────────────────────────────────────────────────────────────

def read_done_cards(cwd: str, day: date) -> "list[str]":
    p = Path(cwd) / "DONE.md"
    if not p.exists():
        return []
    needle = day.isoformat()
    out = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("- [x]") and needle in s:
                out.append(s)
    except OSError:
        pass
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Specs & docs touched (source #6) — mtime inside the day window
# ─────────────────────────────────────────────────────────────────────────────

def _glob_touched(pattern: str, start_ts: float, end_ts: float) -> "list[str]":
    out = []
    for f in glob.glob(pattern):
        try:
            mt = Path(f).stat().st_mtime
        except OSError:
            continue
        if start_ts <= mt < end_ts:
            out.append(f)
    return sorted(out)


def read_specs_touched(cwd: str, start_utc: datetime, end_utc: datetime) -> "list[str]":
    pattern = str(Path(cwd) / "docs" / "internal" / "specs" / "*.md")
    return _glob_touched(pattern, start_utc.timestamp(), end_utc.timestamp())


def read_vault_specs_touched(start_utc: datetime, end_utc: datetime) -> "list[str]":
    pattern = str(Path.home() / "vault" / "01-Projects" / "*" / "specs" / "*.md")
    return _glob_touched(pattern, start_utc.timestamp(), end_utc.timestamp())


# ─────────────────────────────────────────────────────────────────────────────
# Operator notes (source #7, optional) — the operator's own words for the day, written
# by another system into `<journal dir>/YYYY-MM-DD<suffix>.md`. READ-ONLY here: this tool
# never creates, edits or deletes that file, and the model never rewrites a note — the
# section is assembled by code, line for line, in time order.
# ─────────────────────────────────────────────────────────────────────────────

NOTES_SUFFIX_DEFAULT = "-notes"
NOTES_FILE_MAX_BYTES = 1_000_000      # a day's notes are a few KB; never slurp a runaway file
NOTES_HEADING_DEFAULT = "Operator notes"   # the model localizes it; --no-model / failure keep this
NOTES_HEADING_MAX_CHARS = 80
NOTES_HEADING_PREFIX = "NOTES_HEADING:"
NOTES_DIGEST_CHAR_BUDGET = 20_000     # notes given to the model as context (file keeps them all)

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(?:.*?\n)?---[ \t]*(?:\n|\Z)", re.DOTALL)
_HIDDEN_COMMENT_RE = re.compile(r"[ \t]*<!--.*?-->", re.DOTALL)
_NOTE_TIME_RE = re.compile(r"^\s*(?:[-*+]\s+)?(\d{1,2}):(\d{2})(?!\d)")
_NOTE_BULLET_RE = re.compile(r"^\s*[-*+]\s+")
_NOTES_HEADING_LINE_RE = re.compile(
    rf"\A\s*{re.escape(NOTES_HEADING_PREFIX)}[ \t]*(.*?)[ \t]*(?:\n|\Z)")


@dataclass
class DayNote:
    """One note: its first line plus any indented continuation lines, all verbatim."""
    lines: "list[str]"
    sort_minute: int  # minute of the day from the `HH:MM` prefix; an untimed line inherits its predecessor's


def resolve_notes_suffix(cli_value: "str | None") -> str:
    """The notes-file suffix: the CLI flag wins (an explicit empty value switches notes
    off), then $JOURNAL_NOTES_SUFFIX, then the default."""
    if cli_value is not None:
        return cli_value.strip()
    return (os.environ.get("JOURNAL_NOTES_SUFFIX") or "").strip() or NOTES_SUFFIX_DEFAULT


def notes_file_path(vault_dir: Path, day: date, suffix: str) -> "Path | None":
    """`<vault_dir>/YYYY-MM-DD<suffix>.md`, or None for a suffix that cannot name a sibling
    file (empty — that would be the journal itself — or one that climbs or nests a path)."""
    if not suffix or "/" in suffix or "\\" in suffix or ".." in suffix:
        return None
    return vault_dir / f"{day.isoformat()}{suffix}.md"


def parse_day_notes(text: str) -> "list[DayNote]":
    """Notes from a notes file's text: frontmatter and hidden `<!-- -->` comments removed,
    blank lines dropped, everything else kept verbatim (trailing whitespace aside) and
    stable-sorted by its `HH:MM` prefix. An indented line continues the previous note."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    text = _FRONTMATTER_RE.sub("", text, count=1)
    text = _HIDDEN_COMMENT_RE.sub("", text)
    notes: "list[DayNote]" = []
    minute = -1
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        if line[0] in " \t" and notes:
            notes[-1].lines.append(line)
            continue
        m = _NOTE_TIME_RE.match(line)
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            minute = int(m.group(1)) * 60 + int(m.group(2))
        notes.append(DayNote(lines=[line], sort_minute=minute))
    notes.sort(key=lambda n: n.sort_minute)  # stable: equal minutes keep file order
    return notes


def read_day_notes(vault_dir: Path, day: date, suffix: str) -> "list[DayNote]":
    """The day's notes, or [] when there is no (usable) notes file. Never writes, and
    never raises: a broken notes file must not cost the day its journal."""
    path = notes_file_path(vault_dir, day, suffix)
    if path is None:
        if suffix:
            print(f"{LOG_PREFIX} notes ignored: invalid suffix {suffix!r}", file=sys.stderr)
        return []
    try:
        if not path.is_file():
            return []
        with open(path, "rb") as f:
            raw = f.read(NOTES_FILE_MAX_BYTES + 1)
        if len(raw) > NOTES_FILE_MAX_BYTES:
            print(f"{LOG_PREFIX} {path.name}: larger than {NOTES_FILE_MAX_BYTES} bytes, "
                  f"only the start is read", file=sys.stderr)
        text = raw[:NOTES_FILE_MAX_BYTES].decode("utf-8", errors="replace")
        if is_generated_note(text.lstrip("\ufeff")):
            return []  # our own output (e.g. a suffix pointed at the journal) is never "notes"
        return parse_day_notes(text)
    except Exception as e:  # noqa: BLE001 — notes are optional input
        print(f"{LOG_PREFIX} could not read notes file {path}: {e}", file=sys.stderr)
        return []


def render_notes_section(notes: "list[DayNote]", heading: str) -> str:
    """The journal-file section: a heading, then every note line verbatim."""
    return f"## {heading or NOTES_HEADING_DEFAULT}\n\n" + "\n".join(
        ln for n in notes for ln in n.lines)


def render_digest_notes(notes: "list[DayNote]") -> "list[str]":
    """Digest lines carrying the notes to the model as context, bounded by
    NOTES_DIGEST_CHAR_BUDGET (the earliest notes win; the rest are counted, not shown)."""
    rows: "list[str]" = []
    used = 0
    for n in notes:
        row = "\n".join("  " + ln for ln in n.lines)[:NOTES_DIGEST_CHAR_BUDGET]
        if rows and used + len(row) + 1 > NOTES_DIGEST_CHAR_BUDGET:
            break
        rows.append(row)
        used += len(row) + 1
    head = (f"OPERATOR NOTES ({len(notes)} — the operator's own words; the script prints them "
            f"verbatim, so use as context only and do not repeat them):")
    out = [head] + rows
    if len(rows) < len(notes):
        out.append(f"  (+{len(notes) - len(rows)} more notes omitted to fit the budget)")
    return out


def split_notes_heading(body: str) -> "tuple[str, str]":
    """(heading, body) — peels the `NOTES_HEADING: ...` first line the model was asked for.
    ("", body) when it is absent; a mangled heading falls back to the caller's default."""
    m = _NOTES_HEADING_LINE_RE.match(body)
    if not m:
        return "", body
    heading = re.sub(r"[#<>`*_\"]+", "", m.group(1)).strip()[:NOTES_HEADING_MAX_CHARS].strip()
    return heading, body[m.end():].lstrip("\n")


# ─────────────────────────────────────────────────────────────────────────────
# Gather + render
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProjectDay:
    project_id: str
    label: str
    cwd: str
    kind: str  # "project" | "free"
    turns: "list[Turn]" = field(default_factory=list)
    commits: "list[dict]" = field(default_factory=list)
    done_cards: "list[str]" = field(default_factory=list)
    specs_touched: "list[str]" = field(default_factory=list)
    run_outcomes: "dict[str, int]" = field(default_factory=dict)
    session_count: int = 0


def gather_day(day: date, tz_name: str, data_dir: Path) -> dict:
    start_utc, end_utc = la_day_bounds_utc(day, tz_name)
    start_ts, end_ts = start_utc.timestamp(), end_utc.timestamp()

    projects = load_projects(data_dir)
    free_chats = load_free_chats(data_dir)
    total_ledger, per_project_ledger = read_usage_ledger(data_dir, start_ts, end_ts)
    vault_specs = read_vault_specs_touched(start_utc, end_utc)

    by_project: "dict[str, ProjectDay]" = {}

    for pid, info in projects.items():
        turns, session_count = read_transcript_turns(info.cwd, start_utc, end_utc)
        commits = read_git_commits(info.cwd, start_utc, end_utc)
        done = read_done_cards(info.cwd, day)
        specs = read_specs_touched(info.cwd, start_utc, end_utc)
        tl_events = read_timeline_file(
            data_dir / "timeline" / f"{timeline_slug(info.cwd)}.jsonl", start_ts, end_ts,
        )
        outcomes: "dict[str, int]" = {}
        for ev in tl_events:
            if ev.kind == "run_end" and ev.outcome:
                outcomes[ev.outcome] = outcomes.get(ev.outcome, 0) + 1
        ledger = per_project_ledger.get(pid, LedgerStats())
        if not (turns or commits or done or specs or ledger.turns):
            continue  # untouched that day — omit entirely, never pad
        by_project[pid] = ProjectDay(
            project_id=pid, label=info.label, cwd=info.cwd, kind="project",
            turns=turns, commits=commits, done_cards=done, specs_touched=specs,
            run_outcomes=outcomes, session_count=session_count,
        )

    # Free chats share one cwd (DEFAULT_CWD) so cannot be told apart in the raw
    # SDK transcript dir; data/timeline/free-*.jsonl carries the per-chat
    # session_key that free_chats.json labels, so free-chat turns are built
    # from timeline events only (coarser than a named project's turns: no
    # cross-join to the raw transcript for a longer assistant reply).
    active_by_key: "dict[str, list[TimelineEvent]]" = {}
    timeline_dir = data_dir / "timeline"
    if timeline_dir.is_dir():
        for f in sorted(timeline_dir.glob("free-*.jsonl*")):
            key = f.name.split(".jsonl")[0]
            events = read_timeline_file(f, start_ts, end_ts)
            if events:
                active_by_key.setdefault(key, []).extend(events)

    for key, events in active_by_key.items():
        turns = turns_from_timeline_events(events)
        if not turns:
            continue
        info = free_chats.get(key)
        label = f"{info.label} (free chat)" if info else f"{key} (free chat)"
        by_project[f"free:{key}"] = ProjectDay(
            project_id=f"free:{key}", label=label, cwd=(info.cwd if info else ""),
            kind="free", turns=turns, session_count=1,
        )

    return {
        "day": day, "start_utc": start_utc, "end_utc": end_utc,
        "projects": by_project, "total_ledger": total_ledger,
        "vault_specs_touched": vault_specs,
    }


@dataclass
class DayNumbers:
    sessions: int
    turns: int
    active_minutes: int
    first_activity: str
    last_activity: str
    commits: int
    model_mix: "dict[str, int]"
    cost_usd: "float | None"
    projects_count: int


def compute_numbers(g: dict, tz_name: str) -> DayNumbers:
    projects: "dict[str, ProjectDay]" = g["projects"]
    ledger: LedgerStats = g["total_ledger"]
    turns_total = sum(len(p.turns) for p in projects.values())
    sessions_total = sum(max(p.session_count, 1) for p in projects.values())
    commits_total = sum(len(p.commits) for p in projects.values())

    all_ts_ms = [t.ts_ms for p in projects.values() for t in p.turns]
    first_ms = min(all_ts_ms) if all_ts_ms else None
    last_ms = max(all_ts_ms) if all_ts_ms else None
    if ledger.first_ts is not None:
        cand = int(ledger.first_ts * 1000)
        first_ms = cand if first_ms is None else min(first_ms, cand)
    if ledger.last_ts is not None:
        cand = int(ledger.last_ts * 1000)
        last_ms = cand if last_ms is None else max(last_ms, cand)

    return DayNumbers(
        sessions=sessions_total,
        turns=turns_total,
        active_minutes=round(ledger.duration_ms / 60000) if ledger.duration_ms else 0,
        first_activity=ms_to_local_hhmm(first_ms, tz_name) if first_ms is not None else "",
        last_activity=ms_to_local_hhmm(last_ms, tz_name) if last_ms is not None else "",
        commits=commits_total,
        model_mix=dict(ledger.models),
        cost_usd=(ledger.cost_usd if ledger.cost_usd else None),
        projects_count=len(projects),
    )


def truncate_blocks(blocks: "list[str]", max_chars: int) -> "tuple[list[str], int]":
    """`blocks` ordered oldest -> newest. Drops from the FRONT (oldest first)
    until the joined text fits max_chars. Returns (kept, dropped_count)."""
    kept = list(blocks)
    dropped = 0

    def total_len(xs: "list[str]") -> int:
        return sum(len(x) for x in xs) + max(0, len(xs) - 1)  # + join newlines

    while kept and total_len(kept) > max_chars:
        kept.pop(0)
        dropped += 1
    return kept, dropped


def render_digest(g: dict, tz_name: str,
                  notes: "list[DayNote] | None" = None) -> "tuple[str, DayNumbers]":
    day: date = g["day"]
    projects: "dict[str, ProjectDay]" = g["projects"]
    numbers = compute_numbers(g, tz_name)
    weekday = day.strftime("%A")

    parts = [f"DAY: {day.isoformat()} ({weekday}) — timezone {tz_name}"]
    parts.append(f"PROJECTS TOUCHED ({len(projects)}):")
    for pd in sorted(projects.values(), key=lambda p: -(len(p.turns) + len(p.commits))):
        bits = [pd.label]
        if pd.commits:
            bits.append(f"{len(pd.commits)} commits")
        if pd.turns:
            bits.append(f"{len(pd.turns)} turns")
        parts.append("  - " + ", ".join(bits))
    parts.append("")

    parts.append("NUMBERS (computed, ground truth — reuse verbatim, never recompute):")
    parts.append(
        f"  sessions={numbers.sessions} turns={numbers.turns} "
        f"active_minutes={numbers.active_minutes} "
        f"first_activity={numbers.first_activity or 'n/a'} last_activity={numbers.last_activity or 'n/a'} "
        f"commits={numbers.commits} model_mix={numbers.model_mix or '{}'} "
        f"cost_usd={numbers.cost_usd if numbers.cost_usd is not None else 'n/a'}"
    )
    parts.append("")

    if notes:  # context for the model only — the note section itself is built by code
        parts.extend(render_digest_notes(notes))
        parts.append("")

    for pd in sorted(projects.values(), key=lambda p: (p.turns[0].ts_ms if p.turns else 0)):
        if not (pd.commits or pd.done_cards or pd.specs_touched or pd.run_outcomes):
            continue  # nothing beyond turns (already in TIMELINE below) — skip the empty header
        parts.append(f"### {pd.label} (cwd={pd.cwd or 'n/a'})")
        if pd.commits:
            parts.append("  commits:")
            for c in pd.commits:
                parts.append(f"    {c['time']} {c['hash']} {c['subject']}")
        if pd.done_cards:
            parts.append("  done-cards:")
            for line in pd.done_cards:
                parts.append(f"    {line}")
        if pd.specs_touched:
            parts.append("  specs-touched:")
            for s in pd.specs_touched:
                parts.append(f"    {s}")
        if pd.run_outcomes:
            parts.append(f"  run-outcomes: {pd.run_outcomes}")
        parts.append("")

    if g.get("vault_specs_touched"):
        parts.append("VAULT SPECS TOUCHED:")
        for s in g["vault_specs_touched"]:
            parts.append(f"  {s}")
        parts.append("")

    rows: "list[tuple[int, str]]" = []
    for pd in projects.values():
        for t in pd.turns:
            hhmm = ms_to_local_hhmm(t.ts_ms, tz_name)
            row = f"{hhmm} [{pd.label}] PROMPT: {t.prompt}"
            if t.reply:
                row += f"\n       REPLY: {t.reply}"
            rows.append((t.ts_ms, row))
    rows.sort(key=lambda r: r[0])
    row_texts = [r[1] for r in rows]

    header_text = "\n".join(parts)
    budget_for_rows = max(0, DIGEST_CHAR_BUDGET - len(header_text) - 200)
    kept_rows, dropped = truncate_blocks(row_texts, budget_for_rows)

    tl_header = "TIMELINE (chronological, oldest -> newest"
    if dropped:
        tl_header += f"; {dropped} earliest turn(s) omitted to fit the digest budget"
    tl_header += "):"

    digest = header_text + "\n\n" + "\n".join([tl_header] + kept_rows)
    return digest, numbers


def build_index_summary(projects: "dict[str, ProjectDay]", notes_count: int = 0) -> str:
    notes_bit = f"notes: {notes_count}" if notes_count else ""
    if not projects:
        return ", ".join(["no recorded activity"] + ([notes_bit] if notes_bit else []))
    top = max(projects.values(), key=lambda p: (len(p.turns) + len(p.commits) * 3))
    commits_total = sum(len(p.commits) for p in projects.values())
    bits = [f"busiest: {top.label}"]
    if commits_total:
        bits.append(f"{commits_total} commit{'s' if commits_total != 1 else ''}")
    if notes_bit:
        bits.append(notes_bit)
    return ", ".join(bits)


def render_index_line(day: date, summary: str, sessions: int, projects_count: int) -> str:
    return f"- [[{day.isoformat()}]] — {summary} · {sessions} sessions · {projects_count} projects"


_INDEX_LINE_RE = re.compile(r"^- \[\[(\d{4}-\d{2}-\d{2})\]\]")


def upsert_index(existing_text: str, day: date, new_line: str) -> str:
    """Inserts/replaces the entry for `day`, kept newest-first. Idempotent:
    regenerating the same day twice produces byte-identical output. Any
    leading non-entry lines (a header) are preserved verbatim above the list."""
    lines = existing_text.splitlines() if existing_text else []
    header: "list[str]" = []
    entries: "dict[str, str]" = {}
    seen_entry = False
    for ln in lines:
        m = _INDEX_LINE_RE.match(ln)
        if m:
            seen_entry = True
            entries[m.group(1)] = ln
        elif not seen_entry:
            header.append(ln)
        # a stray non-entry line once entries have started is dropped — should
        # not happen since this function is the only writer of the entry block.
    entries[day.isoformat()] = new_line

    ordered = [entries[k] for k in sorted(entries.keys(), reverse=True)]
    if not header:
        header = ["# Journal index", "", "Auto-maintained by tools/daily-journal.py — newest first.", ""]
    elif header[-1].strip() != "":
        header = header + [""]
    return "\n".join(header + ordered) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Frontmatter guard — never overwrite a hand-written vault note
# ─────────────────────────────────────────────────────────────────────────────

def is_generated_note(text: str) -> bool:
    """True iff `text` carries this tool's `generated:` marker in a LEADING
    frontmatter block. A hand-written note that merely mentions the marker
    string in prose is never mistaken for a generated one — only a genuine
    `---`-delimited frontmatter block at the top of the file counts."""
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end == -1:
        return False
    frontmatter = text[:end]
    return re.search(
        rf"^generated:\s*{re.escape(GENERATED_MARKER)}\s*$", frontmatter, re.MULTILINE,
    ) is not None


def choose_note_path(vault_dir: Path, day: date) -> "tuple[Path, bool]":
    """(path, used_fallback). The primary `YYYY-MM-DD.md` is used unless it
    already exists and is NOT a generated note (i.e. it's hand-written) — then
    the note goes to `YYYY-MM-DD-cardloop.md` instead, never clobbering the
    operator's own writing."""
    primary = vault_dir / f"{day.isoformat()}.md"
    if primary.exists():
        try:
            existing = primary.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
        if not is_generated_note(existing):
            return vault_dir / f"{day.isoformat()}-cardloop.md", True
    return primary, False


def render_frontmatter(day: date, numbers: DayNumbers) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        "---\n"
        f"date: {day.isoformat()}\n"
        f"generated: {GENERATED_MARKER}\n"
        f"source: {SOURCE_MARKER}\n"
        f"generated_at: {generated_at}\n"
        f"sessions: {numbers.sessions}\n"
        f"projects: {numbers.projects_count}\n"
        f"active_minutes: {numbers.active_minutes}\n"
        f"commits: {numbers.commits}\n"
        "---\n"
    )


def compose_note(day: date, numbers: DayNumbers, body: str, notes_section: str = "") -> str:
    """`notes_section` (the operator's own notes, already rendered) goes right under the
    title, ahead of the generated chronology; empty = the note is exactly what it always was."""
    weekday = day.strftime("%A")
    title = f"# {day.isoformat()} ({weekday})"
    content = body.strip()
    if notes_section:
        content = notes_section.strip() + "\n\n" + content
    return render_frontmatter(day, numbers) + "\n" + title + "\n\n" + content + "\n"


def write_note_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


# ─────────────────────────────────────────────────────────────────────────────
# Model call (Haiku, subscription) + digest-only fallback body
# ─────────────────────────────────────────────────────────────────────────────

NOTE_RULES = """You will be given an EVIDENCE DIGEST of everything the operator did today \
across every project in his Cardloop cockpit (an aiohttp UI over Claude Agent SDK sessions).

Write ONLY the note BODY below, entirely in {lang} — translate every section heading into \
{lang} too, do not leave them in English. A title line and YAML frontmatter are already \
written for you elsewhere; do not repeat them and do not add a title of your own.

Sections, in this order:

## Day at a glance
Up to 6 bullets — the most consequential things done (shipped, decided, learned), each \
naming its project. Fewer is correct on a quiet day, and one is a complete answer: never \
stretch a single event across several bullets or promote noise to reach a count.

## Timeline
A Markdown table `| Time | Project | What | Outcome |`, one row per meaningful block of work \
(merge consecutive turns on the same topic within about 20 minutes into a single row), ordered \
by time, at most 40 rows. Outcome = a commit hash, a file, a decision, or "unfinished".

## Per project
One short paragraph per project touched: what was done, what is still open (from the last \
prompts/replies of the day), and any commits.

## Specs & docs touched
The specs-touched / VAULT SPECS TOUCHED lists from the digest, if any. Omit this whole section \
if none were touched.

## Numbers
A small Markdown table built EXACTLY from the "NUMBERS:" line in the digest — never recompute \
or guess: sessions, turns, active time, first/last activity, commits, model mix, and cost \
(only include the cost row if a cost figure is present).

## Open threads
What the day left genuinely unfinished — only what the digest itself shows as open, never a \
guess.

Hard rules: every statement must trace to a fact in the digest below — if something is \
unknown, omit it, never invent or pad. If a section (other than Numbers) would be empty, omit \
that whole section. Keep the total output under 550 lines. Output ONLY the note body — no \
preamble such as "Here is the note", no code fence around the whole output, no title, no \
frontmatter.

"""
DIGEST_LABEL = "EVIDENCE DIGEST:\n"
NOTE_INSTRUCTIONS = NOTE_RULES + DIGEST_LABEL

# Appended (before the digest) ONLY on a day that has operator notes — a day without them
# sends the model exactly the prompt it always did.
NOTES_ADDENDUM = """The digest also carries "OPERATOR NOTES": the operator's own words for the day. The \
script prints them verbatim in a section of its own above your body, so never quote, paraphrase \
or list them and do not add a section for them — treat them purely as context for the day. That \
section's heading is localized too, so the ONE exception to "output ONLY the note body" is this: \
begin your output with a single line `NOTES_HEADING: <the phrase "Operator notes" translated into \
{lang}>`, then a blank line, then the body.

"""


async def _call_model_async(prompt: str, model: str, timeout_s: int) -> str:
    # This tool always rides the operator's Claude subscription
    # (~/.claude/.credentials.json), never pay-per-token API billing — pop any
    # stray ANTHROPIC_API_KEY right before the one place it could matter
    # (mirrors bot.py's CLAUDE_AUTH_MODE=subscription guard).
    os.environ.pop("ANTHROPIC_API_KEY", None)
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    options = ClaudeAgentOptions(
        model=model,
        # Same CLI the cockpit runs on (runtime.resolve_cli_path / CLAUDE_CLI_PATH): the SDK's
        # bundled binary gates which model ids the API will serve, and this cron must not be the
        # one place left behind on an older one. Not imported from runtime.py on purpose — this
        # tool stays stdlib-only so it can run from cron without the repo's venv layout.
        cli_path=_cli_path(),
        # Zero tools: an empty allowed_tools is dropped by the SDK and grants the full default
        # toolset plus every MCP server; this pair is what actually yields none.
        tools=[],
        extra_args={"strict-mcp-config": None},
        max_turns=1,
        # Isolation: no CLAUDE.md / settings.json from any cwd — this is a
        # single bounded summarization call, not an agentic session, and it
        # must not be steered by filesystem-discovered instructions.
        setting_sources=[],
    )
    parts: "list[str]" = []

    async def _run() -> None:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for blk in msg.content:
                    if isinstance(blk, TextBlock) and blk.text:
                        parts.append(blk.text)

    await asyncio.wait_for(_run(), timeout=timeout_s)
    return "".join(parts).strip()


def call_model(prompt: str, model: str, timeout_s: int = MODEL_CALL_TIMEOUT_S) -> str:
    return asyncio.run(_call_model_async(prompt, model, timeout_s))


def render_fallback_body(g: dict, numbers: DayNumbers, tz_name: str) -> str:
    """Digest-only note body — no model call. Deterministic, English, plain
    templating straight off the gathered facts (used by --no-model and as the
    safety fallback when the model returns an empty response)."""
    projects: "dict[str, ProjectDay]" = g["projects"]
    lines: "list[str]" = ["## Day at a glance", ""]
    ranked = sorted(projects.values(), key=lambda p: -(len(p.commits) * 10 + len(p.turns)))[:6]
    glance = []
    for pd in ranked:
        bit = f"- **{pd.label}**: {len(pd.turns)} turn(s)"
        if pd.commits:
            bit += f", {len(pd.commits)} commit(s)"
        glance.append(bit)
    lines.extend(glance or ["- no recorded activity"])

    lines += ["", "## Timeline", "", "| Time | Project | What | Outcome |", "| --- | --- | --- | --- |"]
    rows = []
    for pd in projects.values():
        for t in pd.turns:
            what = t.prompt.replace("|", "/").replace("\n", " ")[:80]
            outcome = (t.reply.replace("|", "/").replace("\n", " ")[:60] if t.reply else "unfinished")
            rows.append((t.ts_ms, pd.label, what, outcome))
    for ts_ms, label, what, outcome in sorted(rows)[:40]:
        hhmm = ms_to_local_hhmm(ts_ms, tz_name)
        lines.append(f"| {hhmm} | {label} | {what} | {outcome} |")

    lines += ["", "## Per project", ""]
    for pd in sorted(projects.values(), key=lambda p: (p.turns[0].ts_ms if p.turns else 0)):
        bits = [f"**{pd.label}** — {len(pd.turns)} turn(s)"]
        if pd.commits:
            bits.append(f"{len(pd.commits)} commit(s)")
        last = pd.turns[-1].prompt if pd.turns else ""
        row = f"- {', '.join(bits)}."
        if last:
            row += f" Last: {last[:150]}"
        lines.append(row)

    specs = [s for pd in projects.values() for s in pd.specs_touched] + list(g.get("vault_specs_touched") or [])
    if specs:
        lines += ["", "## Specs & docs touched", ""]
        lines += [f"- {s}" for s in specs]

    lines += ["", "## Numbers", "",
              "| sessions | turns | active_minutes | first | last | commits | cost_usd |",
              "| --- | --- | --- | --- | --- | --- | --- |",
              f"| {numbers.sessions} | {numbers.turns} | {numbers.active_minutes} | "
              f"{numbers.first_activity or 'n/a'} | {numbers.last_activity or 'n/a'} | "
              f"{numbers.commits} | {numbers.cost_usd if numbers.cost_usd is not None else 'n/a'} |"]

    lines += ["", "## Open threads", ""]
    open_lines = [f"- {pd.label}: {pd.turns[-1].prompt[:150]}" for pd in projects.values() if pd.turns]
    lines += open_lines or ["- none recorded"]

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Optional Telegram ping ("the journal for <day> is written")
# ─────────────────────────────────────────────────────────────────────────────

TG_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
TG_TEXT_LIMIT = 3500          # Telegram hard limit is 4096; leave room for markup
TG_NOTES_MAX_ITEMS = 10       # operator notes shown in the ping; the rest become "+N more"
TG_NOTES_CHAR_BUDGET = 1500   # ...and at most this many characters of them
TG_NOTE_ITEM_CHARS = 300      # one long note is clipped (raw text, before escaping)


def _cli_path() -> "str | None":
    """CLAUDE_CLI_PATH, resolved with runtime.resolve_cli_path's exact rule (executable file
    or None). Mirrors it rather than importing it — cron runs this tool standalone.
    tests/test_cli_path_override.py pins the two implementations to the same answers."""
    env_value = os.environ.get("CLAUDE_CLI_PATH")
    raw = (env_value or "").strip()
    if env_value is None:  # .env fills a GAP only — mirrors bot.py's os.environ.setdefault
        raw = _dotenv_value(Path(__file__).resolve().parent.parent, "CLAUDE_CLI_PATH")
    raw = raw.strip().strip('"').strip("'")
    if not raw:
        return None
    path = os.path.expanduser(raw)
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def _dotenv_value(repo_root: Path, key: str) -> str:
    """Read one key from the repo's gitignored .env (no personal value in code)."""
    path = repo_root / ".env"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _section_bullets(body: str) -> "list[list[str]]":
    """Bullets of every `## ` section, in order (headings are localized, so
    sections are addressed by position, never by title)."""
    sections: "list[list[str]]" = []
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            sections.append([])
        elif sections and line.startswith(("- ", "* ")):
            sections[-1].append(line[2:].strip())
    return sections


def extract_glance(body: str) -> "list[str]":
    """Bullets of the note's FIRST section ('Day at a glance', heading localized)."""
    sections = _section_bullets(body)
    return sections[0][:6] if sections else []


def extract_open_threads(body: str) -> "list[str]":
    """Bullets of the LAST section — 'Open threads' when the day left anything
    open. It is optional, and the section before it (Numbers) is a table with
    no bullets, so an empty result means 'nothing open', not a parse miss."""
    sections = _section_bullets(body)
    return sections[-1][:6] if len(sections) > 1 else []


def _tg_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tg_html(text: str) -> str:
    """Escape for Telegram HTML, then re-apply **bold** as <b>."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", _tg_escape(text))


def _tg_note_text(note: DayNote) -> str:
    """One note as a single ping line: list marker dropped, continuation lines joined,
    clipped to TG_NOTE_ITEM_CHARS. The words themselves are not touched."""
    first, *rest = note.lines
    text = " ".join([_NOTE_BULLET_RE.sub("", first, count=1).strip()] + [ln.strip() for ln in rest])
    if len(text) > TG_NOTE_ITEM_CHARS:
        text = text[:TG_NOTE_ITEM_CHARS - 1].rstrip() + "\u2026"
    return text


def _tg_notes_rows(notes: "list[DayNote]") -> "tuple[list[str], int]":
    """(rows to show, how many notes are left out) under the item and character caps."""
    rows: "list[str]" = []
    used = 0
    for n in notes:
        text = _tg_note_text(n)
        if len(rows) >= TG_NOTES_MAX_ITEMS or (rows and used + len(text) > TG_NOTES_CHAR_BUDGET):
            break
        rows.append(text)
        used += len(text) + 3
    return rows, len(notes) - len(rows)


def build_telegram_message(day: date, numbers: DayNumbers, body: str,
                            note_path: Path, public_url: str, warning: str = "",
                            notes: "list[DayNote] | None" = None,
                            notes_heading: str = "") -> str:
    hours, minutes = divmod(max(numbers.active_minutes, 0), 60)
    active = f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"
    head = (f"\U0001F4D4 <b>{day.isoformat()} ({day.strftime('%A')})</b>\n"
            f"{numbers.projects_count} projects \u00b7 {numbers.sessions} sessions \u00b7 "
            f"{active} \u00b7 {numbers.commits} commits")
    if warning:
        head += f"\n\u26a0\ufe0f {_tg_html(warning)}"

    done = ["\u2022 " + _tg_html(b) for b in extract_glance(body)]
    still_open = ["\u2022 " + _tg_html(b) for b in extract_open_threads(body)]
    obsidian = "obsidian://open?file=" + quote(f"Journal/{day.isoformat()}", safe="")
    home = str(Path.home())
    shown = str(note_path)
    if shown.startswith(home):
        shown = "~" + shown[len(home):]
    tail = f"<code>{_tg_html(shown)}</code>\n<code>{_tg_html(obsidian)}</code>"
    if public_url:
        tail += f'\n<a href="{public_url}">cockpit</a>'

    note_rows, notes_hidden = _tg_notes_rows(notes) if notes else ([], 0)
    notes_title = f"\U0001F4DD <b>{_tg_escape(notes_heading or NOTES_HEADING_DEFAULT)}</b>\n"

    def assemble() -> str:
        blocks = [head]
        if note_rows:  # the operator's own words come first, as in the journal file
            shown = ["\u2022 " + _tg_escape(t) for t in note_rows]
            if notes_hidden:
                shown.append(f"\u2026 +{notes_hidden} more")
            blocks.append(notes_title + "\n".join(shown))
        if done:
            blocks.append("\n".join(done))
        if still_open:
            blocks.append("\u23f3 <b>Open</b>\n" + "\n".join(still_open))
        return "\n\n".join(blocks + [tail])

    # Over the limit: drop whole bullets (open threads first), never slice the
    # text — a cut through a <b> tag makes Telegram reject the entire message. Notes go
    # last, and one of them always stays.
    msg = assemble()
    while len(msg) > TG_TEXT_LIMIT and (done or still_open or len(note_rows) > 1):
        if still_open or done:
            (still_open or done).pop()
        else:
            note_rows.pop()
            notes_hidden += 1
        msg = assemble()
    return msg


def notify_telegram(day: date, numbers: DayNumbers, body: str, note_path: Path,
                     repo_root: Path, warning: str = "",
                     notes: "list[DayNote] | None" = None, notes_heading: str = "") -> None:
    """Best-effort ping; never fails the journal run."""
    token = os.environ.get("JOURNAL_TG_BOT_TOKEN") or _dotenv_value(repo_root, "BOT_TOKEN")
    chat = (os.environ.get("JOURNAL_TG_CHAT_ID")
            or _dotenv_value(repo_root, "ALLOWED_USERS").split(",")[0].strip())
    if not token or not chat:
        print(f"{LOG_PREFIX} telegram notify skipped: no token/chat id "
              f"(set JOURNAL_TG_BOT_TOKEN / JOURNAL_TG_CHAT_ID or BOT_TOKEN / ALLOWED_USERS)",
              file=sys.stderr)
        return
    public_url = os.environ.get("JOURNAL_PUBLIC_URL") or _dotenv_value(repo_root, "PUBLIC_URL")
    payload = urlencode({
        "chat_id": chat,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        "text": build_telegram_message(day, numbers, body, note_path, public_url, warning,
                                       notes, notes_heading),
    }).encode()
    try:
        with urlopen(TG_SEND_URL.format(token=token), data=payload, timeout=20) as r:
            ok = json.loads(r.read().decode()).get("ok")
        print(f"{LOG_PREFIX} telegram notify: {'sent' if ok else 'rejected'}")
    except Exception as e:  # noqa: BLE001 — a failed ping must not fail the journal
        print(f"{LOG_PREFIX} telegram notify failed: {e}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate a daily activity journal note from Cardloop cockpit data.",
    )
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--date", help="single calendar day, YYYY-MM-DD, in --tz")
    grp.add_argument("--days", type=int, help="backfill the last N calendar days ending yesterday")
    p.add_argument("--data-dir", default=None, help="Cardloop data/ dir (default: <repo>/data)")
    p.add_argument("--vault-dir", default=None,
                    help="output dir (default: $JOURNAL_DIR or $HOME/vault/Journal)")
    p.add_argument("--tz", default=None, help="IANA timezone (default: $JOURNAL_TZ or America/Los_Angeles)")
    p.add_argument("--notes-suffix", default=None,
                    help="suffix of the operator's notes file next to the journal: "
                         "<vault-dir>/YYYY-MM-DD<suffix>.md, read-only and optional "
                         f"(default: $JOURNAL_NOTES_SUFFIX or {NOTES_SUFFIX_DEFAULT!r}; "
                         "an empty value switches notes off)")
    p.add_argument("--dry-run", action="store_true",
                    help="print the digest and the note to stdout; write nothing")
    p.add_argument("--no-model", action="store_true",
                    help="skip the Haiku call; emit a deterministic digest-only note")
    p.add_argument("--model", default="haiku", help="model alias for the SDK call (default: haiku)")
    p.add_argument("--model-timeout", type=int, default=MODEL_CALL_TIMEOUT_S,
                    help=f"seconds before the model call is killed (default: {MODEL_CALL_TIMEOUT_S})")
    p.add_argument("--notify-telegram", action="store_true",
                    help="after writing the note, send the operator a Telegram summary "
                         "(JOURNAL_TG_BOT_TOKEN/JOURNAL_TG_CHAT_ID, falling back to "
                         "BOT_TOKEN/ALLOWED_USERS in .env)")
    return p


def _process_one_day(day: date, tz_name: str, data_dir: Path, vault_dir: Path,
                      index_path: Path, args: argparse.Namespace, lang: str) -> bool:
    """Returns False when the model call failed. The day still gets a
    digest-only note and its Telegram ping (flagged), so a lapsed login costs
    the prose, not the whole day; the False keeps the cron's exit code red."""
    print(f"{LOG_PREFIX} gathering {day.isoformat()} ({tz_name}) from {data_dir} ...")
    gathered = gather_day(day, tz_name, data_dir)
    # Optional input: the operator's own notes for the day. [] (no file / empty file) leaves
    # every output below exactly as it was before notes existed.
    notes = read_day_notes(vault_dir, day, resolve_notes_suffix(getattr(args, "notes_suffix", None)))
    digest, numbers = render_digest(gathered, tz_name, notes)
    print(
        f"{LOG_PREFIX} {day.isoformat()}: {numbers.projects_count} project(s), "
        f"{numbers.turns} turn(s), digest {len(digest)} chars"
        + (f", {len(notes)} operator note(s)" if notes else "")
    )

    warning = ""
    notes_heading = NOTES_HEADING_DEFAULT
    if args.no_model:
        body = render_fallback_body(gathered, numbers, tz_name)
    else:
        prompt = NOTE_RULES.format(lang=lang)
        if notes:
            prompt += NOTES_ADDENDUM.format(lang=lang)
        prompt += DIGEST_LABEL + digest
        try:
            body = call_model(prompt, args.model, args.model_timeout)
            if notes:  # the model localizes the notes heading; the notes themselves are not its to touch
                heading, body = split_notes_heading(body)
                notes_heading = heading or notes_heading
        except Exception as e:  # noqa: BLE001 — fall back to the digest-only note
            print(f"{LOG_PREFIX} model call FAILED for {day.isoformat()}: {e}", file=sys.stderr)
            warning = f"model call failed ({str(e)[:160]}) — digest-only note"
            body = render_fallback_body(gathered, numbers, tz_name)
        if not body:
            print(f"{LOG_PREFIX} empty model response for {day.isoformat()} — "
                  f"falling back to the digest-only note", file=sys.stderr)
            body = render_fallback_body(gathered, numbers, tz_name)

    notes_section = render_notes_section(notes, notes_heading) if notes else ""
    note = compose_note(day, numbers, body, notes_section)

    if args.dry_run:
        print(f"{LOG_PREFIX} ── digest: {day.isoformat()} ──")
        print(digest)
        print(f"{LOG_PREFIX} ── note: {day.isoformat()} ──")
        print(note)
        return not warning

    vault_dir.mkdir(parents=True, exist_ok=True)
    note_path, used_fallback_path = choose_note_path(vault_dir, day)
    write_note_atomic(note_path, note)

    summary = build_index_summary(gathered["projects"], len(notes))
    if used_fallback_path:
        summary += f" (hand-written note kept at {day.isoformat()}.md — see [[{note_path.stem}]])"
    line = render_index_line(day, summary, numbers.sessions, numbers.projects_count)
    existing_index = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    write_note_atomic(index_path, upsert_index(existing_index, day, line))

    print(f"{LOG_PREFIX} wrote {note_path}")

    if getattr(args, "notify_telegram", False):
        extra = {"notes": notes, "notes_heading": notes_heading} if notes else {}
        notify_telegram(day, numbers, body, note_path, Path(__file__).resolve().parent.parent,
                        warning, **extra)
    return not warning


def main(argv: "list[str] | None" = None) -> int:
    args = build_arg_parser().parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    data_dir = Path(args.data_dir) if args.data_dir else repo_root / "data"
    tz_name = args.tz or os.environ.get("JOURNAL_TZ") or DEFAULT_TZ
    vault_dir = Path(args.vault_dir) if args.vault_dir else Path(
        os.environ.get("JOURNAL_DIR") or str(Path.home() / "vault" / "Journal")
    )
    lang = (os.environ.get("RESPONSE_LANGUAGE") or "").strip() or "en"

    try:
        tz = ZoneInfo(tz_name)
    except Exception as e:
        print(f"{LOG_PREFIX} invalid timezone {tz_name!r}: {e}", file=sys.stderr)
        return 1

    if not data_dir.is_dir():
        print(f"{LOG_PREFIX} data dir not found: {data_dir}", file=sys.stderr)
        return 1

    now_local = datetime.now(tz)
    days = compute_target_days(args.date, args.days, now_local.date() - timedelta(days=1))

    index_path = vault_dir / "_index.md"
    ok = True
    for day in days:
        try:
            if not _process_one_day(day, tz_name, data_dir, vault_dir, index_path, args, lang):
                ok = False
        except Exception as e:  # noqa: BLE001 — one bad day must not sink the whole backfill
            print(f"{LOG_PREFIX} FAILED for {day.isoformat()}: {e}", file=sys.stderr)
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
