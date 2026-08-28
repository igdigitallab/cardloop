"""
tools/precommit-secret-scan.sh — the pre-commit barrier for repos without gitleaks installed.

Every scenario runs against a throwaway git repo under pytest's tmp_path, never against this
repo's own working tree or index (the project's own .git/hooks install is exercised manually,
see the task report — a live commit attempt is not something a test suite should do here).
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

# Fixtures are ASSEMBLED at runtime, never written out as a literal key shape.
# A test file containing a real-looking Stripe or Slack token trips GitHub's own
# push protection and blocks the push — the fixture must exercise the scanner
# without ever existing as a matchable string on disk.
_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
_STRIPE = "sk_" + "live_" + "abcdefghijklmnopqrstuvwx"
_GITHUB = "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"
_TELEGRAM = "123456789" + ":AA" + "Hf3x9pQzR7lY2mN8vK1sT4uW6xZ0aB3cD"
_SLACK = "xoxb" + "-1234567890-abcdefghijklmnop"
_PEM_HEAD = "BEGIN " + "RSA " + "PRIVATE" + " KEY"
_PEM_TAIL = "END " + "RSA " + "PRIVATE" + " KEY"
_PRIVATE_KEY = f"-----{_PEM_HEAD}-----\nMIIEpAIBAAKCAQEA...\n-----{_PEM_TAIL}-----\n"
_GENERIC = "api" + "_key" + ' = "abcdefghijklmnopqrstuvwxyz123456"\n'

SCRIPT = ROOT / "tools" / "precommit-secret-scan.sh"


def _run(cwd: Path, *args: str, env: "dict | None" = None) -> subprocess.CompletedProcess:
    import os
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(list(args), cwd=cwd, env=full_env,
                           capture_output=True, text=True, timeout=30)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A fresh git repo with the scanner script staged and user identity configured."""
    r = tmp_path / "repo"
    r.mkdir()
    _run(r, "git", "init", "-q")
    _run(r, "git", "config", "user.email", "test@example.com")
    _run(r, "git", "config", "user.name", "Test")
    return r


def _stage_and_scan(repo: Path, filename: str, content: str, env: "dict | None" = None):
    (repo / filename).write_text(content)
    _run(repo, "git", "add", filename)
    return _run(repo, "bash", str(SCRIPT), env=env)


def test_script_exists_and_is_executable():
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & 0o111, "script must be executable"


def test_blocks_aws_key(repo):
    result = _stage_and_scan(repo, "leaked.py", f'AWS_ACCESS_KEY = "{_AWS}"\n')
    assert result.returncode == 1
    assert "AWS access key" in result.stderr
    assert "leaked.py:1" in result.stderr


def test_blocks_private_key(repo):
    result = _stage_and_scan(
        repo, "id_rsa.txt",
        _PRIVATE_KEY,
    )
    assert result.returncode == 1
    assert "private key" in result.stderr


def test_blocks_stripe_live_key(repo):
    result = _stage_and_scan(repo, "cfg.py", f'STRIPE_KEY = "{_STRIPE}"\n')
    assert result.returncode == 1
    assert "Stripe" in result.stderr


def test_blocks_github_token(repo):
    result = _stage_and_scan(
        repo, "cfg.py", f'GITHUB_TOKEN = "{_GITHUB}"\n')
    assert result.returncode == 1
    assert "GitHub token" in result.stderr


def test_blocks_telegram_bot_token(repo):
    result = _stage_and_scan(
        repo, "cfg.py", f'TG = "{_TELEGRAM}"\n')
    assert result.returncode == 1
    assert "Telegram" in result.stderr


def test_blocks_slack_token(repo):
    result = _stage_and_scan(
        repo, "cfg.py", f'SLACK = "{_SLACK}"\n')
    assert result.returncode == 1
    assert "Slack" in result.stderr


def test_blocks_generic_api_key(repo):
    result = _stage_and_scan(
        repo, "cfg.py", _GENERIC)
    assert result.returncode == 1
    assert "api key" in result.stderr


def test_blocks_dotenv_filename_even_if_content_looks_harmless(repo):
    result = _stage_and_scan(repo, ".env", "SOME_VAR=harmless\n")
    assert result.returncode == 1
    assert ".env" in result.stderr


def test_blocks_pem_filename(repo):
    result = _stage_and_scan(repo, "server.pem", "not even key-shaped content\n")
    assert result.returncode == 1
    assert "server.pem" in result.stderr


def test_blocks_credentials_json_filename(repo):
    result = _stage_and_scan(repo, "credentials.json", "{}\n")
    assert result.returncode == 1


def test_allows_clean_commit(repo):
    result = _stage_and_scan(repo, "clean.py", 'def hello():\n    return "world"\n')
    assert result.returncode == 0, result.stderr


def test_allows_ops_commands_mentioned_in_prose(repo):
    # false-positive guard: talking ABOUT git push/rm -rf/docker in a doc must not trip the scan
    result = _stage_and_scan(
        repo, "README.md",
        "Push with `git push origin master` and clean with `docker compose down`"
        " or `rm -rf ./build`.\n",
    )
    assert result.returncode == 0, result.stderr


def test_skip_env_var_bypasses_scan(repo):
    result = _stage_and_scan(
        repo, "cfg.py", _GENERIC,
        env={"SKIP_SECRET_SCAN": "1"},
    )
    assert result.returncode == 0
    assert "SKIPPED" in result.stderr


def test_ignore_file_excludes_a_path(repo):
    (repo / ".secretscanignore").write_text("fixtures/*\n")
    _run(repo, "git", "add", ".secretscanignore")
    (repo / "fixtures").mkdir()
    result = _stage_and_scan(
        repo, "fixtures/sample_key.py", f'AWS_ACCESS_KEY = "{_AWS}"\n')
    assert result.returncode == 0, result.stderr


def test_real_commit_is_actually_rejected_by_the_hook(repo):
    """End-to-end: install the hook for real and attempt `git commit`, not just run the
    script directly — proves the hook wiring, not only the classifier."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-commit"
    hook_path.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f'exec "{SCRIPT}"\n'
    )
    hook_path.chmod(0o755)
    (repo / "leaked.py").write_text(f'AWS_ACCESS_KEY = "{_AWS}"\n')
    _run(repo, "git", "add", "leaked.py")
    result = _run(repo, "git", "commit", "-m", "should be rejected")
    assert result.returncode != 0
    assert "AWS access key" in result.stdout + result.stderr
    log = _run(repo, "git", "log", "--oneline")
    assert log.stdout.strip() == ""  # no commit was created
