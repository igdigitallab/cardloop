"""
Shared fixtures for Cardloop tests.
"""
import os
import sys
from pathlib import Path

# Add the project root to sys.path so webapp can be imported without installation
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# The aiohttp test client talks plain HTTP, so a Secure-flagged auth cookie set
# by api_login is never echoed back → every authenticated request 401s. Unset the
# override for tests (CI does not set it either) so the cookie follows the request
# scheme and suites stay deterministic regardless of the operator's .env. Must run
# before webapp is imported anywhere, as the mode is read into a module-level string
# at import time.
os.environ.pop("WEB_COOKIE_SECURE", None)

# Same class of leak: engine reads CROSS_SESSION_INBOUND into a module-level constant
# at import time, and several suites assert the pre-feature invariant "options.settings
# is None". An operator who set it in .env would turn those red on their machine only.
# The three tests that exercise the feature monkeypatch the constant directly.
os.environ.pop("CROSS_SESSION_INBOUND", None)

# The SDK release watch is the one component that would reach the network from a test.
# Force it off for the whole suite; the tests that exercise it flip _SDK_CHECK_ENABLED
# back on and monkeypatch the fetch, so no test can ever actually call PyPI.
os.environ["SDK_UPDATE_CHECK"] = "0"

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
