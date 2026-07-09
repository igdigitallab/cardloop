"""Handoff digests are inherited by the next session as fact — so they must not lie.

Regression cover for the 2026-07-09 incident: haiku, handed ~35k chars of dialog whose last
turn had been interrupted mid-work, continued the transcript instead of summarizing it. It
wrote the `git commit` calls the agent was about to make and reported two hashes (7d8ae9e,
4e9f1a7) as "✅ shipped". Neither existed. The next session would have skipped that work.

Three independent guards, tested here:
  1. _handoff_narrative_ok   — reject an echo / a digest missing its headers.
  2. _annotate_unknown_hashes — git, not the model, decides which hashes are real.
  3. _git_ground_truth        — the model is shown what is actually committed and what is not.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import webapp as _webapp


# ─────────────────────────── Fixtures ─────────────────────────────────────────


@pytest.fixture
def git_repo(tmp_path):
    """A throwaway repo with exactly one real commit."""
    def _git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args],
                              capture_output=True, text=True, check=True)

    _git("init", "-q")
    _git("config", "user.email", "t@example.com")
    _git("config", "user.name", "t")
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    _git("add", "a.txt")
    _git("commit", "-q", "-m", "real commit")
    sha = _git("rev-parse", "--short", "HEAD").stdout.strip()
    return tmp_path, sha


# ─────────────────────────── _handoff_narrative_ok ────────────────────────────


def test_valid_digest_is_accepted():
    narrative = "## Where we stopped\nMid-refactor of engine.py; next step is the test run."
    assert _webapp._handoff_narrative_ok(narrative) is True


def test_echoed_transcript_is_rejected():
    """The exact failure shape: the model replays the rendered dialog back at us."""
    narrative = (
        "## Where we stopped\nAll good.\n"
        "[tool]: {'name': 'Bash', 'kind': 'bash', 'cmd': 'git commit -q -m \"ship it\"'}\n"
        "Коммит `7d8ae9e`."
    )
    assert _webapp._handoff_narrative_ok(narrative) is False


def test_oversized_digest_is_rejected():
    """A ≤500-word digest that comes back at 16k chars is a transcript, not a digest."""
    narrative = "## Where we stopped\n" + ("x" * (_webapp._HANDOFF_MAX_NARRATIVE_CHARS + 1))
    assert _webapp._handoff_narrative_ok(narrative) is False


def test_digest_without_required_header_is_rejected():
    assert _webapp._handoff_narrative_ok("Some prose with no headers at all.") is False


@pytest.mark.parametrize("header", ["## Where we stopped", "## Where We Stopped", "## WHERE WE STOPPED"])
def test_header_check_is_case_insensitive(header):
    """A title-cased header is still a real digest — do not throw it away over cosmetics."""
    assert _webapp._handoff_narrative_ok(f"{header}\nMid-refactor of engine.py.") is True


def test_empty_narrative_is_rejected():
    assert _webapp._handoff_narrative_ok("") is False
    assert _webapp._handoff_narrative_ok("   \n ") is False


# ─────────────────────────── _annotate_unknown_hashes ─────────────────────────


def test_fabricated_hash_is_flagged(git_repo):
    repo, _ = git_repo
    text = "### ✅ Phase 2 (commit `7d8ae9e`): per-project brains"

    out = _webapp._annotate_unknown_hashes(text, str(repo))

    assert "NO SUCH COMMIT" in out
    assert "7d8ae9e" in out  # annotated, not deleted — the reader sees the false claim


def test_real_hash_is_left_alone(git_repo):
    repo, sha = git_repo
    text = f"Shipped in commit {sha}, tests green."

    out = _webapp._annotate_unknown_hashes(text, str(repo))

    assert out == text
    assert "NO SUCH COMMIT" not in out


def test_both_hashes_from_the_real_incident_are_flagged(git_repo):
    repo, _ = git_repo
    text = "Phase 2 -> 7d8ae9e, Phase 3a -> 4e9f1a7. Both shipped."

    out = _webapp._annotate_unknown_hashes(text, str(repo))

    assert out.count("NO SUCH COMMIT") == 2


def test_hash_free_text_is_untouched(git_repo):
    repo, _ = git_repo
    text = "No hashes here, just prose about engine.py and webapp.py."
    assert _webapp._annotate_unknown_hashes(text, str(repo)) == text


@pytest.mark.parametrize("text", [
    "Session started at unix time 1783616753.",       # digits only — a timestamp
    "Context grew to 3145728 tokens before rotation.",  # digits only — a token count
    "The plan was defaced by a bad merge.",            # letters only — an English word
    "Cache hit 99, fresh 287, duration 9495 ms.",      # short numbers
])
def test_hexish_prose_is_not_branded_a_fabrication(git_repo, text):
    """`[0-9a-f]{7,40}` also matches timestamps, token counts and words like "defaced".
    Branding those as fabricated commits is worse than the bug being guarded against."""
    repo, _ = git_repo
    assert "NO SUCH COMMIT" not in _webapp._annotate_unknown_hashes(text, str(repo))


def test_non_git_dir_leaves_text_intact(tmp_path):
    """rev-parse cannot run → leave the text alone rather than smear a possibly-real hash."""
    text = "Shipped in commit 7d8ae9e."
    assert "NO SUCH COMMIT" not in _webapp._annotate_unknown_hashes(text, str(tmp_path))


# ─────────────────────────── _git_ground_truth ────────────────────────────────


def test_ground_truth_lists_real_commits_and_clean_tree(git_repo):
    repo, sha = git_repo
    gt = _webapp._git_ground_truth(str(repo))

    assert "GROUND TRUTH" in gt
    assert sha in gt
    assert "Working tree is clean" in gt


def test_ground_truth_surfaces_uncommitted_work(git_repo):
    """The fact the summarizer most needs, and most often gets wrong."""
    repo, _ = git_repo
    (repo / "b.txt").write_text("work in progress", encoding="utf-8")

    gt = _webapp._git_ground_truth(str(repo))

    assert "NOT committed" in gt
    assert "b.txt" in gt


def test_ground_truth_is_empty_outside_a_repo(tmp_path):
    assert _webapp._git_ground_truth(str(tmp_path)) == ""


# ─────────────────────────── prompt contract ──────────────────────────────────


def test_prompt_forbids_inventing_hashes_and_continuing_the_transcript():
    p = _webapp.ROTATION_SUMMARY_PROMPT
    assert "Never invent" in p
    assert "Never continue it" in p
    assert "not committed is NOT done" in p or "not committed is not done" in p.lower()


def test_digest_model_defaults_to_sonnet_in_source():
    """haiku at effort=low drifted into continuing a long transcript; sonnet-5 is the default.

    Assert the SOURCE default, not os.environ — reading the env with a sonnet-5 fallback would
    pass even if the code had been reverted to haiku, which is exactly the regression to catch.
    """
    import inspect

    src = inspect.getsource(_webapp._build_handoff_inner)
    assert 'os.environ.get("HANDOFF_MODEL", "claude-sonnet-5")' in src


def test_session_title_does_not_read_the_digest_model_var():
    """HANDOFF_MODEL now means "the model that decides what is true". The cosmetic title model
    must not share it — overriding one should not silently change the other."""
    import inspect

    src = inspect.getsource(_webapp._build_session_title)
    assert 'os.environ.get("HANDOFF_MODEL"' not in src, "must not READ the digest model var"
    assert 'os.environ.get("SESSION_TITLE_MODEL", "haiku")' in src
