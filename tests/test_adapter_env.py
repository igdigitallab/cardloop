"""adapter_env: what Codex and Grok get from the cockpit besides the prompt (tools/ on PATH + the
image/file hint), exactly when a Claude turn gets it."""
from __future__ import annotations

import os

import adapter_env
import engine


def test_without_the_media_vars_the_env_is_an_unchanged_copy():
    src = {"PATH": "/usr/bin", "TOKEN": "t"}
    out = adapter_env.with_tools_on_path(src)
    assert out == src and out is not src
    assert adapter_env.with_tools_on_path(None) == {}
    assert adapter_env.media_hint({"TOKEN": "t"}) == "" and adapter_env.media_hint(None) == ""


def test_the_media_vars_put_tools_first_on_path_once():
    tools = adapter_env.TOOLS_DIR
    out = adapter_env.with_tools_on_path({"COPS_MEDIA_DIR": "/m", "PATH": f"/usr/bin{os.pathsep}{tools}"})
    assert out["PATH"].split(os.pathsep) == [tools, "/usr/bin"]


def test_without_a_path_in_the_env_the_process_path_is_extended(monkeypatch):
    monkeypatch.setenv("PATH", "/bin")
    assert adapter_env.with_tools_on_path({"COPS_MEDIA_DIR": "/m"})["PATH"] == f"{adapter_env.TOOLS_DIR}{os.pathsep}/bin"


def test_the_hint_is_the_claude_engines_own_text():
    hint = adapter_env.media_hint({"COPS_MEDIA_DIR": "/m"})
    assert engine.IMAGES_PROMPT in hint and engine.FILES_PROMPT in hint
