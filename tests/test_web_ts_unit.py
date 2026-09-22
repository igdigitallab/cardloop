"""Runs the frontend's pure-logic unit tests (web/src/**/*.test.ts) inside the pytest suite.

They are written against Node's built-in runner (no vitest/jest in web/package.json), which
left them manual-only: nobody ran them, and a mis-named bundle made `node --test` report
"# tests 0 / # pass 0" — green with nothing executed. Here each file is bundled with the
esbuild that ships with vite (the modules import React and the api client), and the run must
report at least one passing test and zero failures.

Skipped when node or web/node_modules is missing (a checkout that never ran `npm install`).
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
WEB = ROOT / "web"
ESBUILD = WEB / "node_modules" / ".bin" / "esbuild"
TEST_FILES = sorted(p.relative_to(WEB) for p in (WEB / "src").rglob("*.test.ts"))


@pytest.mark.skipif(shutil.which("node") is None or not ESBUILD.exists(),
                    reason="node or web/node_modules (esbuild) not installed")
@pytest.mark.parametrize("rel", TEST_FILES, ids=[str(p) for p in TEST_FILES])
def test_web_ts_unit(rel, tmp_path):
    # The bundle name must end in .test.cjs — node --test only discovers *.test.* files.
    out = tmp_path / (rel.stem + ".cjs")
    build = subprocess.run(
        [str(ESBUILD), str(rel), "--bundle", "--platform=node", "--format=cjs",
         f"--outfile={out}", "--log-level=warning"],
        cwd=WEB, capture_output=True, text=True, timeout=120,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(["node", "--test", str(tmp_path)], capture_output=True, text=True, timeout=120)
    summary = run.stdout + run.stderr
    passed = int((re.search(r"^# pass (\d+)", summary, re.M) or [0, 0])[1])
    failed = int((re.search(r"^# fail (\d+)", summary, re.M) or [0, 1])[1])
    assert run.returncode == 0 and failed == 0, summary[-4000:]
    assert passed > 0, f"no tests ran for {rel} — bundle not discovered?\n{summary[-2000:]}"
