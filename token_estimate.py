"""token_estimate.py - the ONE rough chars -> tokens conversion.

No tokenizer is available offline, so every cost figure the cockpit prints about
prompt size (tools/skills-lint.py, the project health check) is an approximation
built from this single constant and labelled as such.  Stdlib only.
"""
from __future__ import annotations

# Rough heuristic: ~4 characters per token for English prose.  Cyrillic and other
# non-Latin text costs more tokens per character, so for such files this UNDER-counts.
CHARS_PER_TOKEN = 4


def approx_tokens(chars: int) -> int:
    """Approximate token count for *chars* characters (0 stays 0)."""
    return chars // CHARS_PER_TOKEN if chars else 0
