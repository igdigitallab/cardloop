"""Every GitHub Actions `uses:` must be pinned to a full commit SHA (Scorecard
Pinned-Dependencies). A tag or branch ref can be re-pointed by the action's owner;
a commit SHA cannot. Dependabot (github-actions ecosystem) keeps the SHAs current
and rewrites the trailing version comment with them."""
import re
from pathlib import Path

WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"
USES = re.compile(r"^\s*-?\s*uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _uses_lines():
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        for n, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), 1):
            m = USES.match(line)
            if m:
                yield wf.name, n, m.group("ref"), m.group("rest")


def test_workflows_exist():
    assert list(_uses_lines()), "no `uses:` found - did the workflows move?"


def test_every_action_is_pinned_to_a_full_sha_with_a_version_comment():
    bad = []
    for name, n, ref, rest in _uses_lines():
        if ref.startswith("./"):
            continue  # a local action lives in this repo
        if not PINNED.match(ref):
            bad.append(f"{name}:{n} {ref} is not pinned to a 40-hex commit SHA")
        elif not re.match(r"^\s*#\s*v?\d+(\.\d+)*", rest):
            bad.append(f"{name}:{n} {ref} has no trailing `# vX.Y.Z` version comment")
    assert not bad, "\n".join(bad)


def test_codeql_runs_the_extended_security_suite():
    text = (WORKFLOWS / "codeql.yml").read_text(encoding="utf-8")
    assert re.search(r"^\s*queries:\s*security-extended\b", text, re.M)
