"""spec-096 P3a leftovers from P2: the push-subscription file is private, and the DONE.md title
extraction is linear (and cuts the title exactly where the old regex did)."""
import json
import os
import random
import re
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import spec_mirror  # noqa: E402
import webapp  # noqa: E402


# ── push-subscriptions.json ──────────────────────────────────────────────────

def test_push_subscriptions_are_written_private_and_atomically(tmp_path, monkeypatch):
    f = tmp_path / "push-subscriptions.json"
    monkeypatch.setattr(webapp, "_PUSH_SUBS_FILE", f)
    old_umask = os.umask(0o022)                      # the umask that made write_text() 0644
    try:
        subs = [{"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}]
        webapp._save_push_subs(subs)
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
        assert webapp._load_push_subs() == subs
        # an older, world-readable file is replaced by a private one, not widened or kept
        os.chmod(f, 0o644)
        webapp._save_push_subs(subs + [{"endpoint": "https://push.example/def"}])
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
        assert len(json.loads(f.read_text())) == 2
    finally:
        os.umask(old_umask)
    assert [p.name for p in tmp_path.iterdir()] == ["push-subscriptions.json"]   # no temp leftovers


def test_push_subscriptions_save_is_a_noop_before_init(monkeypatch):
    monkeypatch.setattr(webapp, "_PUSH_SUBS_FILE", None)
    webapp._save_push_subs([{"endpoint": "x"}])       # must not raise


# ── spec_mirror._done_line_title ─────────────────────────────────────────────

_OLD_RE = re.compile(r"^\s*[-*]\s*\[.\]\s*(.*?)\s*<!--")


def _old_title(line: str, fallback: str = "") -> str:
    tm = _OLD_RE.match(line)
    return tm.group(1).strip() if tm and tm.group(1).strip() else fallback


@pytest.mark.parametrize("line", [
    "- [x] Ship the thing <!--ops:abc spec=096 rt=1-->",
    "* [ ] star bullet   <!--ops:abc-->",
    "   - [x]   padded   title   <!--ops:abc-->",
    "- [x] <!--ops:abc-->",                       # no title -> falls back to the id upstream
    "- [x] title with <!-- a comment --> and <!--ops:abc-->",   # cut at the FIRST <!--
    "- [x] no marker at all",
    "just text <!--ops:abc-->",
    "- x] broken checkbox <!--ops:abc-->",
    "- [xy] two-char box <!--ops:abc-->",
    "-[x]tight<!--ops:abc-->",
    "- [x]  nbsp  title  <!--ops:abc-->",    # unicode whitespace, as \s and strip() see it
    "- [x] tab\t<!--ops:abc-->",
    "",
])
def test_title_extraction_is_unchanged_on_real_shapes(line):
    assert spec_mirror._done_line_title(line) == _old_title(line)


def test_title_extraction_is_unchanged_on_random_lines():
    rng = random.Random(96)
    alphabet = ["-", "*", "[", "]", "x", " ", " ", "\t", "<!--", "<!-", "-->", "!", "a", "B", " ", "ops:id "]
    for _ in range(20000):
        line = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        assert spec_mirror._done_line_title(line) == _old_title(line), repr(line)


def test_title_extraction_is_linear_on_a_whitespace_flood():
    # The old pattern is quadratic here: a long blank run that is NOT followed by `<!--`.
    flood = "- [x] a" + " " * 100_000 + "b <!--ops:abc spec=096-->"
    start = time.perf_counter()
    title = spec_mirror._done_line_title(flood)
    elapsed = time.perf_counter() - start
    assert title.startswith("a") and title.endswith("b")
    assert elapsed < 0.05, f"{elapsed * 1000:.1f} ms"
    for hostile in ("\t" * 100_000, "- [x]" + " " * 100_000, "-" + " " * 100_000 + "[", "[" * 100_000):
        start = time.perf_counter()
        spec_mirror._done_line_title(hostile)
        assert time.perf_counter() - start < 0.05


def test_done_cards_for_spec_end_to_end_with_a_hostile_line(tmp_path):
    cwd = tmp_path
    flood = "- [x] a" + " " * 100_000 + "b <!--ops:abc123 spec=096-->"
    (cwd / "DONE.md").write_text(
        "# Done\n"
        "- [x] Plain card <!--ops:aaa111 spec=096-->\n"
        f"{flood}\n"
        "- [x] Other spec <!--ops:bbb222 spec=097-->\n"
        "- [x] <!--ops:ccc333 spec=096-->\n",
        encoding="utf-8",
    )
    start = time.perf_counter()
    got = spec_mirror._done_cards_for_spec(str(cwd), "096")
    assert time.perf_counter() - start < 0.2
    assert set(got) == {"aaa111", "abc123", "ccc333"}
    assert got["aaa111"] == "Plain card"
    assert got["ccc333"] == "ccc333"                      # empty title falls back to the id
    assert got["abc123"].startswith("a") and got["abc123"].endswith("b")
