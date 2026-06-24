"""
Shared fixtures for claude-ops-bot tests.
"""
import os
import sys
from pathlib import Path

# Add the project root to sys.path so webapp can be imported without installation
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# The aiohttp test client talks plain HTTP, so a Secure-flagged auth cookie set
# by api_login is never echoed back → every authenticated request 401s. Drop the
# flag for tests (CI does not set it either) so suites are deterministic
# regardless of the operator's .env. Must run before webapp is imported anywhere,
# as _WEB_COOKIE_SECURE is read into a module-level bool at import time.
os.environ.pop("WEB_COOKIE_SECURE", None)

import pytest


@pytest.fixture
def tmp_cwd(tmp_path: Path) -> Path:
    """Temporary directory — simulates a project cwd."""
    return tmp_path


@pytest.fixture
def fake_ctx(tmp_path: Path) -> dict:
    """Minimal ctx (dict-injected state) sufficient for most tests.
    Does not start a real PTB/SDK — only file operations."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return {
        "topics": {},
        "sessions": {},
        "running": {},
        "password": "test-password",
        "DATA": data_dir,
        "HERE": ROOT,
        "VAULT_PROJECTS": tmp_path / "vault" / "01-Projects",
        "DEFAULT_MODEL": "sonnet",
        "save_sessions": lambda: None,
        "save_topics": lambda: None,
        "run_engine": None,
        "ptb_app": None,
    }
