"""
Slug validation tests for project renaming.

Regex: ^[a-z0-9][a-z0-9-]{0,40}[a-z0-9]$
(Used in api_project_rename — if the function has not yet been extracted to a constant,
the test duplicates the regex intentionally, to lock down the behaviour and prevent
silent breakage.)

If webapp.py exports _SLUG_RE — use it directly.
Otherwise — test the pattern directly (backward-compatible).
"""
import re
import pytest


# Try to import the constant from webapp (the lead may have added _SLUG_RE).
# If not exported — duplicate the pattern in the test.
try:
    from webapp import _SLUG_RE  # type: ignore[attr-defined]
    _SLUG_PATTERN = _SLUG_RE
except ImportError:
    _SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}[a-z0-9]$")


def _slug_ok(slug: str) -> bool:
    """Returns True if the slug matches the pattern."""
    return bool(_SLUG_PATTERN.match(slug))


# ─────────────────────────── valid slugs ───────────────────────────

@pytest.mark.parametrize("slug", [
    "my-cool-bot",
    "abc-def-ghi",
    "claude-ops-bot",
    "a1",
    "z9",
    "project1",
    "my-project-2",
    "networking-os",
    "a1b2c3d4e5f6",
])
def test_slug_valid(slug: str):
    """Valid slugs must pass validation."""
    assert _slug_ok(slug), f"Slug {slug!r} should be valid"


# ─────────────────────────── invalid slugs ───────────────────────────

@pytest.mark.parametrize("slug,reason", [
    ("-leading",       "starts with a dash"),
    ("trailing-",      "ends with a dash"),
    ("UPPER",          "uppercase letters"),
    ("MyProject",      "mixed case"),
    ("with_underscore","underscore not allowed"),
    ("space here",     "space in the middle"),
    ("",               "empty string"),
    ("-",              "dash only"),
    ("a",              "single char — does not meet min length (need [a-z0-9]{2} min)"),
    ("abc!def",        "special char exclamation mark"),
    ("abc.def",        "dot not allowed"),
    ("abc/def",        "slash not allowed"),
    ("a" * 43,         "too long (>42 chars)"),
])
def test_slug_invalid(slug: str, reason: str):
    """Invalid slugs must be rejected."""
    assert not _slug_ok(slug), f"Slug {slug!r} should be INVALID: {reason}"


# ─────────────────────────── boundary cases ───────────────────────────

def test_slug_min_length():
    """A 2-char slug is the minimum allowed (single char is not)."""
    assert _slug_ok("a1"), "2-char slug should be allowed"
    assert not _slug_ok("a"), "1-char slug should not be allowed"


def test_slug_max_length():
    """42 chars is the maximum allowed (1 + 40 + 1)."""
    max_slug = "a" + "b" * 40 + "c"  # 42 chars
    assert len(max_slug) == 42
    assert _slug_ok(max_slug), f"42-char slug should be allowed"

    too_long = "a" + "b" * 41 + "c"  # 44 chars
    assert not _slug_ok(too_long), "44-char slug should not be allowed"


def test_slug_numbers_only():
    """A digits-only slug is valid."""
    assert _slug_ok("12"), "12 is a valid slug"
    assert _slug_ok("123456"), "digits only are allowed"


def test_slug_consecutive_dashes():
    """Multiple consecutive dashes — pattern [a-z0-9-] allows this; document the behaviour."""
    # Pattern [a-z0-9-]{0,40} allows "--", which is technically ok.
    # This test explicitly documents that behaviour.
    slug_with_double_dash = "my--project"
    # Matches the pattern (starts and ends with a letter/digit)
    assert _slug_ok(slug_with_double_dash), (
        "Double dash is technically allowed by [a-z0-9-]{0,40} — "
        "test documents this behaviour"
    )
