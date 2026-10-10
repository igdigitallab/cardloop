"""Lifecycle status of a curated memory article (frontmatter `status:` / `superseded_by:`).

An article marked `status: superseded` or `status: rejected` stays on disk and in the Memory
tab, but recall (the search index, the context pack) must not serve it: the memory source
carries the highest rank weight in the corpus, so a stale article would outrank live code and
fresh chat. Marking is the soft alternative to deleting — the history stays readable.

Stdlib only: imported by search.py, context_pack.py and the standalone tools/memory-lint.py.
"""
from __future__ import annotations

import re

INACTIVE_STATUSES = frozenset({"superseded", "rejected"})

# Only the head of the file is parsed: frontmatter sits at the top, and memory articles can be
# large. The fields may be top-level or nested under `metadata:` (native auto-memory nests).
_HEAD_CHARS = 4096
_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---", re.S)
_FIELD = re.compile(r"^[ \t]*(status|superseded_by)[ \t]*:[ \t]*(.*?)[ \t]*$", re.M)


def frontmatter_fields(text: str) -> dict[str, str]:
    """`{"status": ..., "superseded_by": ...}` from the article's frontmatter (absent → missing key)."""
    m = _FRONTMATTER.match(text[:_HEAD_CHARS])
    if not m:
        return {}
    out: dict[str, str] = {}
    for key, value in _FIELD.findall(m.group(1)):
        value = value.strip().strip("\"'").strip()
        if value and key not in out:
            out[key] = value.lower() if key == "status" else value
    return out


def is_inactive(text: str) -> bool:
    """True when the article is marked superseded or rejected and must not be recalled."""
    return frontmatter_fields(text).get("status") in INACTIVE_STATUSES
