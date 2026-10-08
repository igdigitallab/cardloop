"""spec-096 P2b: the board regexes are linear AND parse exactly what they used to parse.

CodeQL py/polynomial-redos #75-#78. `_MARKER_RE` began with `\\s*` and was used UNANCHORED
(`finditer` / `sub`), so a card text of 40 000 spaces cost seconds; `_CARD_RE` / `_PLAIN_CARD_RE`
have overlapping `\\s*` / `(.*)` and go quadratic once the string holds a newline.

The fix must not change what the board parses, so this file keeps the OLD patterns and the OLD
helper bodies verbatim and compares them to the shipped ones — on a committed synthetic fixture,
on a seeded random corpus (odd whitespace, newlines inside the string, ids of dashes, ...) — and
then asserts the timing. The comparison against every real TASKS.md/DONE.md on a host is a
one-off run, not a test (it would read $HOME).
"""
import random
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import board
import search
import spec_mirror

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "board_regex"

# ── the patterns and helpers exactly as they were before spec-096 P2b ──────────────────────────
OLD_CARD_RE = re.compile(r"^\s*[-*]\s*\[(.)\]\s*(.*)$")
OLD_PLAIN_CARD_RE = re.compile(r"^\s*[-*]\s+(?!\[)(.+)$")
OLD_MARKER_RE = re.compile(r"\s*<!--\s*ops:([\w-]+)(\s[^>]*)?\s*-->")


def old_extract_id_and_text(rest):
    matches = list(OLD_MARKER_RE.finditer(rest))
    if not matches:
        return board._new_card_id(), rest.strip()
    cid = matches[0].group(1)
    clean = OLD_MARKER_RE.sub("", rest).strip()
    return cid, clean


def old_extract_id_text_and_meta(rest):
    matches = list(OLD_MARKER_RE.finditer(rest))
    if not matches:
        return board._new_card_id(), rest.strip(), {}
    m0 = matches[0]
    cid = m0.group(1)
    meta = board._parse_marker_meta(m0.group(2))
    clean = OLD_MARKER_RE.sub("", rest).strip()
    return cid, clean, meta


def old_iter_markers(text):
    return (board.Marker(m.group(1), m.group(2), m.start(), m.end()) for m in OLD_MARKER_RE.finditer(text))


@pytest.fixture
def fixed_card_ids(monkeypatch):
    monkeypatch.setattr(board, "_new_card_id", lambda: "NEWID")


# ── what "the same" means ──────────────────────────────────────────────────────────────────────

def snapshot(text: str) -> dict:
    """Everything that consumes these regexes, for one file's text."""
    lines = text.splitlines()
    return {
        "parse": board._parse_tasks(text),
        "search_cards": list(search._iter_card_lines(text)),
        # webapp/board `done_count` counts these
        "card_line_count": sum(1 for ln in lines if board._CARD_RE.match(ln)),
        "mirror_ids": {m.id for m in spec_mirror._iter_markers(text)},
        "mirror_lines": [(m.id, board._parse_marker_meta(m.meta))
                         for m in (next(spec_mirror._iter_markers(ln), None) for ln in lines) if m],
        "extract": [(board._extract_id_and_text(ln), board._extract_id_text_and_meta(ln)) for ln in lines],
    }


def snapshots_old_and_new(text, monkeypatch):
    new = snapshot(text)
    with monkeypatch.context() as mp:
        mp.setattr(board, "_CARD_RE", OLD_CARD_RE)
        mp.setattr(board, "_PLAIN_CARD_RE", OLD_PLAIN_CARD_RE)
        mp.setattr(board, "_MARKER_RE", OLD_MARKER_RE)
        mp.setattr(board, "_extract_id_and_text", old_extract_id_and_text)
        mp.setattr(board, "_extract_id_text_and_meta", old_extract_id_text_and_meta)
        mp.setattr(board, "_iter_markers", old_iter_markers)
        mp.setattr(board, "_strip_markers", lambda t: OLD_MARKER_RE.sub("", t))
        mp.setattr(spec_mirror, "_iter_markers", old_iter_markers)
        old = snapshot(text)
    return old, new


def assert_regexes_agree(s: str):
    """Regex level: the same match / the same groups on one string (it may contain newlines)."""
    o, n = OLD_CARD_RE.match(s), board._CARD_RE.match(s)
    assert (o is None) == (n is None), f"_CARD_RE presence differs on {s!r}"
    if o:
        assert o.groups() == n.groups(), f"_CARD_RE groups differ on {s!r}"

    # A plain card whose text is only whitespace is no card at all (the parser drops empty text),
    # and the old pattern handed a leading whitespace char to group 1 that every caller strips.
    o, n = OLD_PLAIN_CARD_RE.match(s), board._PLAIN_CARD_RE.match(s)
    o_text = o.group(1).strip() if o else ""
    n_text = n.group(1).strip() if n else ""
    assert o_text == n_text, f"_PLAIN_CARD_RE card text differs on {s!r}"
    assert not (n and not n_text), f"_PLAIN_CARD_RE matched an empty card on {s!r}"

    om = [(m.group(1), m.group(2)) for m in OLD_MARKER_RE.finditer(s)]
    nm = [(m.group(1), m.group(2)) for m in board._MARKER_RE.finditer(s)]
    assert om == nm, f"_MARKER_RE markers differ on {s!r}"
    # the linear iterator yields the same markers as the reference pattern's finditer
    fm = [(m.group(0), m.group(1), m.group(2)) for m in board._MARKER_RE.finditer(s)]
    im = [(s[m.start:m.end], m.id, m.meta) for m in board._iter_markers(s)]
    assert fm == im, f"_iter_markers differs from finditer on {s!r}"
    assert OLD_MARKER_RE.sub("", s) == board._strip_markers(s), f"marker stripping differs on {s!r}"


# ── fixture + corpus ───────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["board_tasks.md", "board_done.md"])
def test_synthetic_fixture_parses_identically(name, monkeypatch, fixed_card_ids):
    text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    old, new = snapshots_old_and_new(text, monkeypatch)
    assert new == old
    for line in text.splitlines():
        assert_regexes_agree(line)
    assert_regexes_agree(text)          # the whole file as ONE string: newlines inside


def test_synthetic_fixture_is_not_vacuous(fixed_card_ids):
    """The fixture must actually reach the paths it claims to (cards, meta, plain, star, markers)."""
    _, cols = board._parse_tasks((FIXTURE_DIR / "board_tasks.md").read_text(encoding="utf-8"))
    cards = [c for col in cols.values() for c in col]
    assert len(cards) >= 35
    assert any(c.get("model") == "haiku" and c.get("spec") == "096" and c.get("rt") for c in cards)
    assert any(c.get("provider") == "codex" for c in cards)
    texts = [c["text"] for c in cards]
    assert "marker mid text   and text after it" in texts        # the marker and the blanks BEFORE it go
    assert any(c["id"] == "ffff6666" or c["id"] == "eeee5555" for c in cards)
    assert any(c["text"] == "plain bullet card" and c["id"] == "NEWID" for c in cards)
    assert any(c["text"] == "star plain card" for c in cards)
    assert all(c["id"] != "unk11111" and c["id"] != "pre0" for c in cards)   # unknown section / preamble


_TOKENS = ["-", "*", "[", "]", " ", "  ", "\t", "\n", "x", "a", "?", "!", "~", "<!--", "-->", "->", ">",
           "ops:", "ops:abc", "ops:-", "id-9", " model=haiku", " spec=096", " rt=1790000000", "=",
           "\u00a0", "\u2003", "\u3000", "\x0b", "\x1c", "\x85", "\u2028", "\r", "é", "<", "!"]


def _random_strings(n: int, seed: int):
    rng = random.Random(seed)
    for _ in range(n):
        yield "".join(rng.choice(_TOKENS) for _ in range(rng.randint(0, 16)))


def test_random_corpus_agrees_at_regex_level():
    n = 0
    for s in _random_strings(60_000, seed=96):
        assert_regexes_agree(s)
        n += 1
    assert n == 60_000


_WS = ["", " ", "  ", "\t", "\n", "\u00a0", "\u3000", " \n", "\n ", "\x0b", "   "]
_TAIL = ["", "x", "[", "[x", "[ ] y", " y ", "y\n", "y\nz", "\n", "<!--ops:ab-->", "- [ ] z", "\u00a0q"]


def _structured_lines(n: int, seed: int):
    """`ws bullet ws [status] ws tail` with whitespace and newlines everywhere: the shapes that
    actually reach the two card patterns (a flat random token soup almost never does)."""
    rng = random.Random(seed)
    for _ in range(n):
        bullet = rng.choice(["-", "*", "-", "*", "+", ""])
        status = rng.choice(["[ ]", "[x]", "[?]", "[ ", "[\n]", "[\t]", "[]", "[xy]", "", "[ ] ["])
        yield (rng.choice(_WS) + bullet + rng.choice(_WS) + status + rng.choice(_WS)
               + rng.choice(_WS) + rng.choice(_TAIL) + rng.choice(["", "", " ", "\n"]))


def test_structured_corpus_agrees_and_reaches_the_card_patterns():
    n = cards = plains = 0
    for s in _structured_lines(120_000, seed=962):
        assert_regexes_agree(s)
        n += 1
        cards += bool(OLD_CARD_RE.match(s))
        o = OLD_PLAIN_CARD_RE.match(s)
        plains += bool(o and o.group(1).strip())
    assert cards > 5_000 and plains > 5_000, (cards, plains)   # the corpus is not vacuous
    assert n == 120_000


def test_random_corpus_marker_shapes_agree(fixed_card_ids):
    """Stress the part the fix rewrote: markers glued to arbitrary whitespace and punctuation."""
    rng = random.Random(1096)
    ws = [" ", "  ", "\t", "\n", "\u00a0", "\u2003", "\r\n", ""]
    bodies = ["<!--ops:abc-->", "<!--ops:abc k=v-->", "<!--  ops:a-b   x=1  -->", "<!--ops:-->",
              "<!--ops:- -->", "<!--ops:a- -->", "<!--ops:a--->", "<!--ops:a b>c-->", "<!--ops:a",
              "<!-- ops: -->", "<!--ops:a\t-->"]
    for _ in range(30_000):
        s = "".join(rng.choice(ws) + rng.choice(bodies + ["text", "- [ ] "]) for _ in range(rng.randint(1, 6)))
        s += rng.choice(ws)
        assert_regexes_agree(s)
        assert old_extract_id_text_and_meta(s) == board._extract_id_text_and_meta(s), repr(s)
        assert old_extract_id_and_text(s) == board._extract_id_and_text(s), repr(s)


def test_iter_markers_equals_finditer_on_marker_soup():
    """The skip-ahead scan must never lose or invent a marker: compare spans and groups on a soup
    of marker fragments (unterminated starts, stray `>`, `--`, dashes in ids, nested starts)."""
    rng = random.Random(4242)
    frags = ["<!--", "<!--", "ops:", "ops:a", "ops:-", "ops:a-b", " ", " ", "\t", "\n", ">", "->", "-->",
             "-->", "--", "-", "x", "k=v", "<", "!", "<!-- ", "<!--ops:a-->", "<!--ops:b k=1-->",
             "\x85", "\u2028", "\u00a0", "_", "é", "9", "#"]
    hits = 0
    for _ in range(120_000):
        text = "".join(rng.choice(frags) for _ in range(rng.randint(0, 14)))
        want = [(m.span(), m.groups()) for m in board._MARKER_RE.finditer(text)]
        got = [((m.start, m.end), (m.id, m.meta)) for m in board._iter_markers(text)]
        assert got == want, repr(text)
        hits += bool(want)
    assert hits > 10_000, hits                       # the soup reaches real markers


def test_random_whole_files_parse_identically(monkeypatch, fixed_card_ids):
    rng = random.Random(7)
    heads = ["## Backlog", "## In Progress", "## Review", "## Failed", "## Other", "# Title", ""]
    for _ in range(300):
        lines = []
        for _ in range(rng.randint(1, 25)):
            lines.append(rng.choice(heads) if rng.random() < 0.15 else "".join(
                rng.choice(_TOKENS) for _ in range(rng.randint(0, 14))).replace("\n", ""))
        text = "\n".join(lines)
        old, new = snapshots_old_and_new(text, monkeypatch)
        assert new == old, text


def test_marker_stripping_keeps_the_text_around_it(fixed_card_ids):
    assert board._extract_id_and_text("a   <!--ops:abcd-->   b") == ("abcd", "a   b")
    assert board._extract_id_and_text("a<!--ops:abcd-->b <!--ops:efgh-->") == ("abcd", "ab")
    assert board._extract_id_text_and_meta("x <!--ops:abcd model=haiku rt=1790000000-->  ") == (
        "abcd", "x", {"model": "haiku", "rt": 1790000000})
    assert board._extract_id_and_text("no marker  ") == ("NEWID", "no marker")


# ── timing: 100 000 spaces and the CodeQL-reported prefixes ────────────────────────────────────

N = 100_000
LIMIT_SEC = 0.05

_SP = " " * N
_ATTACKS = {
    "spaces": _SP,
    "spaces+newline": _SP + "\nx",
    "tabs": "\t" * N,
    "marker prefix + spaces (#75/#76)": "<!--ops:-" + _SP,
    "spaces + marker prefix (#75/#76)": _SP + "<!--ops:-",
    "marker prefix + dash + spaces": "<!--ops:- " + _SP,
    "marker meta + spaces": "<!--ops:abc k=v" + _SP,
    "card prefix + spaces (#78)": "*[a]" + _SP,
    "card prefix + spaces + newline (#78)": "*[a]" + _SP + "\nx",
    "card prefix + spaces + x + newline": "*[a]" + _SP + "x\ny",
    "spaces + card prefix": _SP + "*[a]",
    "plain prefix + spaces (#77)": "* " + _SP,
    "plain prefix + spaces + newline (#77)": "* " + _SP + "\nx",
    "plain prefix + double spaces (#77)": "* " + "  " * (N // 2),
    "plain prefix + spaces + bracket": "*" + _SP + "[x",
    "plain prefix + spaces + x + newline": "* " + _SP + "x\ny",
    "dash then spaces": "-" + _SP,
    # not reported by CodeQL, measured: every `<!--` start used to rescan `[^>]*` to the end
    "repeated marker starts + gt": "<!--ops:a " * (N // 10) + ">",
    "repeated marker starts, no gt": "<!--ops:a " * (N // 10),
    "repeated marker starts + dashes": "<!--ops:a --" * (N // 12) + ">",
    "repeated bare comment starts + gt": "<!--" * (N // 4) + ">",
    "repeated long-id starts": "<!--ops:" + "a" * 50 + "#" + (" <!--ops:" + "a" * 50 + "#") * (N // 60),
}


def _paths():
    """Every regex call path the board code uses."""
    return {
        "_MARKER_RE.finditer": lambda s: list(board._MARKER_RE.finditer(s)),
        "_MARKER_RE.search": lambda s: board._MARKER_RE.search(s),
        "_iter_markers": lambda s: list(board._iter_markers(s)),
        "_strip_markers": board._strip_markers,
        "_extract_id_and_text": board._extract_id_and_text,
        "_extract_id_text_and_meta": board._extract_id_text_and_meta,
        "_CARD_RE.match": board._CARD_RE.match,
        "_PLAIN_CARD_RE.match": board._PLAIN_CARD_RE.match,
        "_parse_tasks": lambda s: board._parse_tasks("## Backlog\n" + s.replace("\n", " ")),
        "search._iter_card_lines": lambda s: list(search._iter_card_lines("## Backlog\n" + s)),
        "spec_mirror._iter_markers": lambda s: list(spec_mirror._iter_markers(s)),
    }


# The raw pattern is linear on whitespace, but a bare finditer/search still retries `[^>]*` from every
# `<!--` start; that is exactly what `_iter_markers` exists for, so the raw pattern is not timed on
# the "repeated starts" attacks (no board code path calls it unanchored any more).
_RAW_PATTERN_PATHS = ("_MARKER_RE.finditer", "_MARKER_RE.search")


@pytest.mark.parametrize("attack", list(_ATTACKS))
def test_no_quadratic_blowup(attack):
    s = _ATTACKS[attack]
    slow = {}
    for name, fn in _paths().items():
        if name in _RAW_PATTERN_PATHS and attack.startswith("repeated"):
            continue
        best = _time(fn, s)
        if best < 10 * LIMIT_SEC:              # only re-measure what looks fast: noise, not blow-ups
            best = min(best, _time(fn, s), _time(fn, s))
        if best >= LIMIT_SEC:
            slow[name] = round(best * 1000, 1)
    assert not slow, f"{attack!r} ({len(s)} chars) is slow on: {slow} (ms, limit {LIMIT_SEC * 1000:.0f})"


def _time(fn, arg) -> float:
    t = time.perf_counter()
    fn(arg)
    return time.perf_counter() - t
