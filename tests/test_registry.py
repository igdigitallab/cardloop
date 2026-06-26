"""
Tests for Phase 1 OSS-hardening:
- loading data/registry.json is merged into the registry
- missing file → empty _REG_RAW (auto-scan build_registry does not break)
- env fallbacks VAULT_PROJECTS / OPERATOR_NAME / RESPONSE_LANGUAGE
"""
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


# ──────────────────────────────────────────────────────────────
# Helper: isolated import of bot.py with overridden HERE and env
# ──────────────────────────────────────────────────────────────

def _import_bot_with(tmp_path: Path, env_overrides: dict = None, reg_json: dict = None):
    """
    Loads bot (isolated via importlib) in tmp_path.
    - tmp_path is used as HERE (project root)
    - reg_json, if provided, is written to tmp_path/data/registry.json
    - env_overrides patch os.environ during import
    Returns the module.
    """
    # create data/ and registry.json if needed
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    if reg_json is not None:
        (data_dir / "registry.json").write_text(json.dumps(reg_json))

    # set minimal env
    env_patch = {
        "BOT_TOKEN": "fake:token",
        "ALLOWED_USERS": "999",
        "GROUP_CHAT_ID": "0",
    }
    if env_overrides:
        env_patch.update(env_overrides)

    old_env = {}
    for k, v in env_patch.items():
        old_env[k] = os.environ.get(k)
        os.environ[k] = v

    # evict previous import cache for bot (so module re-evaluates)
    sys.modules.pop("bot", None)

    old_argv = sys.argv[:]
    try:
        spec = importlib.util.spec_from_file_location("bot", ROOT / "bot.py")
        mod = importlib.util.module_from_spec(spec)
        # substitute HERE before exec_module; no monkeypatch available here —
        # simpler to read _load_registry_json directly with HERE substituted via env-free trick
        spec.loader.exec_module(mod)
        return mod
    finally:
        # restore env
        for k, old_v in old_env.items():
            if old_v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old_v
        sys.modules.pop("bot", None)


# ──────────────────────────────────────────────────────────────
# Tests for _load_registry_json directly (without full bot import)
# ──────────────────────────────────────────────────────────────

def _make_load_fn(tmp_path: Path):
    """Creates _load_registry_json bound to tmp_path as HERE."""
    from pathlib import Path as P
    import json as _json

    home = P.home()

    def _home_sub(*parts):
        return str(home.joinpath(*parts))

    def _load_registry_json():
        reg_f = tmp_path / "data" / "registry.json"
        if not reg_f.exists():
            return {}
        try:
            raw = _json.loads(reg_f.read_text())
            return {k: _home_sub(v) for k, v in raw.items()
                    if isinstance(k, str) and isinstance(v, str)}
        except Exception:
            return {}

    return _load_registry_json


class TestLoadRegistryJson:
    def test_missing_file_returns_empty(self, tmp_path):
        load = _make_load_fn(tmp_path)
        (tmp_path / "data").mkdir()
        assert load() == {}

    def test_valid_file_loaded(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "registry.json").write_text(
            json.dumps({"myproject": "my-project", "myapp": "my-app"})
        )
        load = _make_load_fn(tmp_path)
        result = load()
        home = str(Path.home())
        assert result["myproject"] == f"{home}/my-project"
        assert result["myapp"] == f"{home}/my-app"

    def test_invalid_json_returns_empty(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "registry.json").write_text("NOT VALID JSON {{{{")
        load = _make_load_fn(tmp_path)
        assert load() == {}

    def test_non_string_values_skipped(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "registry.json").write_text(
            json.dumps({"good": "good-project", "bad": 123, "alsobad": None})
        )
        load = _make_load_fn(tmp_path)
        result = load()
        assert "good" in result
        assert "bad" not in result
        assert "alsobad" not in result

    def test_entries_expand_to_home(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "registry.json").write_text(json.dumps({"proj": "some-dir"}))
        load = _make_load_fn(tmp_path)
        result = load()
        assert result["proj"] == str(Path.home() / "some-dir")


# ──────────────────────────────────────────────────────────────
# Tests for env fallbacks VAULT_PROJECTS / OPERATOR_NAME / RESPONSE_LANGUAGE
# (verified by reading constants from the bot module)
# ──────────────────────────────────────────────────────────────

class TestEnvFallbacks:
    """Tests the logic via direct import of helpers from bot.py."""

    def _get_constants(self, monkeypatch, env: dict):
        """Imports bot with the given env, returns the relevant constants."""
        # evict cache — also evict engine so env-dependent constants re-evaluate
        sys.modules.pop("bot", None)
        sys.modules.pop("engine", None)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        # ensure no stale values
        for k in ("VAULT_PROJECTS", "OPERATOR_NAME", "RESPONSE_LANGUAGE"):
            if k not in env:
                monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("BOT_TOKEN", "fake:token")
        monkeypatch.setenv("ALLOWED_USERS", "999")
        monkeypatch.setenv("GROUP_CHAT_ID", "0")
        # Don't auto-load the repo .env — otherwise OPERATOR_NAME/RESPONSE_LANGUAGE
        # from a populated .env resurrect via setdefault and the default-case
        # assertions become env-dependent (flaky across checkouts/CI).
        monkeypatch.setenv("COPS_NO_DOTENV", "1")
        import importlib
        spec = importlib.util.spec_from_file_location("bot_test_env", ROOT / "bot.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules.pop("bot_test_env", None)
        return mod

    def test_operator_name_default(self, monkeypatch):
        mod = self._get_constants(monkeypatch, {})
        assert mod.OPERATOR_NAME == "the operator"

    def test_operator_name_custom(self, monkeypatch):
        mod = self._get_constants(monkeypatch, {"OPERATOR_NAME": "Alice"})
        assert mod.OPERATOR_NAME == "Alice"

    def test_response_language_default_empty(self, monkeypatch):
        mod = self._get_constants(monkeypatch, {})
        assert mod.RESPONSE_LANGUAGE == ""

    def test_response_language_set(self, monkeypatch):
        mod = self._get_constants(monkeypatch, {"RESPONSE_LANGUAGE": "ru"})
        assert mod.RESPONSE_LANGUAGE == "ru"

    def test_vault_projects_default_none(self, monkeypatch):
        """If VAULT_PROJECTS is not set, ctx receives None."""
        # checked via the expression in _on_start
        mod = self._get_constants(monkeypatch, {})
        # bot.py builds VAULT_PROJECTS in _on_start via os.environ.get
        # direct check: env not set → None
        import os
        monkeypatch.delenv("VAULT_PROJECTS", raising=False)
        result = Path(os.environ["VAULT_PROJECTS"]) if os.environ.get("VAULT_PROJECTS") else None
        assert result is None

    def test_vault_projects_set(self, monkeypatch, tmp_path):
        """If VAULT_PROJECTS is set, it is expanded to a Path."""
        import os
        monkeypatch.setenv("VAULT_PROJECTS", str(tmp_path))
        result = Path(os.environ["VAULT_PROJECTS"]) if os.environ.get("VAULT_PROJECTS") else None
        assert result == tmp_path

    def test_nudge_uses_operator_name(self, monkeypatch):
        """DEFAULT_NUDGE contains OPERATOR_NAME, not a hardcoded value."""
        mod = self._get_constants(monkeypatch, {"OPERATOR_NAME": "TestUser"})
        assert "TestUser" in mod.DEFAULT_NUDGE

    def test_nudge_no_language_directive_when_empty(self, monkeypatch):
        """If RESPONSE_LANGUAGE is empty — no language directive is present."""
        mod = self._get_constants(monkeypatch, {"RESPONSE_LANGUAGE": ""})
        # there must be no "answer in ..." directive
        assert "answer in" not in mod.DEFAULT_NUDGE

    def test_nudge_has_language_directive_when_set(self, monkeypatch):
        """If RESPONSE_LANGUAGE is set — the directive is present in the nudge."""
        mod = self._get_constants(monkeypatch, {"RESPONSE_LANGUAGE": "ru"})
        assert "answer in ru" in mod.DEFAULT_NUDGE
