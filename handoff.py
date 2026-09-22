"""handoff.py — the block handed to the OTHER engine when a chat crosses runtimes (spec-092 P2).

Why a crossing needs one at all: Claude and Codex keep their conversations in different
stores (`session_id` vs `codex_thread_id`). Flip the picker and the new engine starts cold —
and answers as if the chat began now, which reads like a healthy continuation. That is worse
than an obvious reset: it will happily "fix back" work the other engine just did.

⚠️ Deliberately NOT a model call. The spec's own review found three defects that block
reusing the `/rotate` summariser: it parses Claude JSONL and cannot read Codex history, its
injector keys pending summaries by the PROJECT session_key (another chat can consume one) and
deletes the summary BEFORE delivery is confirmed, and its helper hardwires `claude-sonnet-5`
with no account or backend — so building a handoff for an all-local chat would ship that
chat's transcript to the cloud. A deterministic extractor has none of those failure modes: no
network, no provider, no account, nothing to leak, and no way to hallucinate a constraint
that was never stated.

It is also the right shape. A prose summary is lossy exactly where agentic work lives: exact
paths, error text, and NEGATIVE constraints ("do not touch module X"). So this carries
verbatim operator constraints and the last N raw messages, not a retelling.
"""
from __future__ import annotations

import re

# Phrases that mark a standing instruction rather than a passing remark. Matched on the
# operator's OWN messages only — an assistant restating a rule is an echo, and echoing it
# back into the next engine doubles the noise without adding authority.
_CONSTRAINT_PATTERNS = (
    r"\bdo not\b", r"\bdon't\b", r"\bnever\b", r"\bmust not\b", r"\bavoid\b",
    r"\bstop\b", r"\bonly\b", r"\balways\b", r"\bdo NOT\b",
    # The operator writes in Russian; a constraint stated there is exactly as binding.
    r"\bне\s+\w+", r"\bникогда\b", r"\bнельзя\b", r"\bтолько\b", r"\bвсегда\b",
)
_CONSTRAINT_RE = re.compile("|".join(_CONSTRAINT_PATTERNS), re.IGNORECASE)

MAX_CONSTRAINTS = 12
MAX_RAW_MESSAGES = 6
MAX_MESSAGE_CHARS = 1200
MAX_FILES = 15


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def extract_constraints(messages: list[dict]) -> list[str]:
    """Verbatim operator lines that read as standing instructions, newest LAST.

    Line-level, not message-level: a constraint usually lives in one sentence of a long
    message, and carrying the whole message would blow the budget on prose the other engine
    does not need. Deduplicated on the cleaned text so a rule the operator repeated three
    times does not eat three slots.
    """
    out: list[str] = []
    seen: set[str] = set()
    for msg in messages:
        if (msg.get("role") or "") != "user":
            continue
        for raw_line in (msg.get("text") or "").splitlines():
            line = _clean(raw_line)
            # Too short to be a rule, too long to be quoted verbatim without cost.
            if len(line) < 8 or len(line) > 300:
                continue
            if not _CONSTRAINT_RE.search(line):
                continue
            key = line.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(line)
    return out[-MAX_CONSTRAINTS:]


def extract_files(messages: list[dict]) -> list[str]:
    """Paths this session actually touched, from the tool calls already on each message.

    The single most expensive thing to lose across a crossing: the new engine otherwise
    re-derives which files the work lives in, usually by grepping, usually wrongly.
    """
    out: list[str] = []
    seen: set[str] = set()
    for msg in messages:
        for tool in msg.get("tools") or []:
            path = tool.get("file") if isinstance(tool, dict) else None
            if not path or not isinstance(path, str):
                continue
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
    return out[-MAX_FILES:]


def recent_messages(messages: list[dict], limit: int = MAX_RAW_MESSAGES) -> list[dict]:
    """The tail, raw. Only user/assistant turns — board strips, runtime markers and model
    fallbacks are cockpit chrome, not conversation, and mean nothing to the other engine."""
    convo = [m for m in messages if (m.get("role") or "") in ("user", "assistant")]
    out = []
    for m in convo[-limit:]:
        text = _clean(m.get("text") or "")
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + " […truncated]"
        if not text:
            continue
        out.append({"role": m.get("role"), "text": text})
    return out


def build_handoff(
    messages: list[dict],
    *,
    from_label: str,
    to_label: str,
) -> dict:
    """`{text, constraints, files, recent, unreplayed}` — the editable handoff block.

    `text` is what actually gets prefixed onto the first prompt on the new engine; the other
    fields are returned so the UI can show WHAT was extracted rather than a wall of markdown
    the operator has to re-read to trust.
    """
    constraints = extract_constraints(messages)
    files = extract_files(messages)
    recent = recent_messages(messages)
    unreplayed = max(0, len([m for m in messages
                             if (m.get("role") or "") in ("user", "assistant")]) - len(recent))

    lines: list[str] = []
    lines.append(f"# Handoff: {from_label} → {to_label}")
    lines.append("")
    lines.append(
        f"This conversation was running on {from_label} and continues here. You do NOT have "
        f"its transcript: {unreplayed} earlier message(s) were not replayed. Everything you "
        f"can rely on is below. Do not assume work you cannot see is absent — ask before "
        f"redoing or reverting anything."
    )
    if constraints:
        lines.append("")
        lines.append("## Standing constraints (verbatim, from the operator)")
        lines.extend(f"- {c}" for c in constraints)
    if files:
        lines.append("")
        lines.append("## Files this session touched")
        lines.extend(f"- {f}" for f in files)
    if recent:
        lines.append("")
        lines.append("## Last messages (raw)")
        for m in recent:
            who = "operator" if m["role"] == "user" else "previous engine"
            lines.append(f"[{who}] {m['text']}")
    lines.append("")
    lines.append("---")
    return {
        "text": "\n".join(lines),
        "constraints": constraints,
        "files": files,
        "recent": recent,
        "unreplayed": unreplayed,
    }
