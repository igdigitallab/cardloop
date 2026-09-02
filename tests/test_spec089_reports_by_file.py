"""spec-089 §3 — sub-agent reports by file, at most 5 lines in context.

The CLI pastes an agent's whole final text into the orchestrator's context via
<task-notification>, and it rides in every later request (12 agents × 2–8K tokens per wave).
Every roster prompt therefore ends with the FINAL ANSWER contract, and the ultracode complement
makes the orchestrator demand it in every brief it writes.
"""
import engine


def test_every_roster_prompt_carries_the_final_answer_contract():
    for name, agent in engine.DEFAULT_AGENTS.items():
        p = agent.prompt
        assert "FINAL ANSWER" in p, name
        assert "5 lines" in p, name
        assert "Never paste" in p, name


def test_heavy_roles_report_by_file_path():
    for name in ("executor", "researcher", "skeptic"):
        p = engine.DEFAULT_AGENTS[name].prompt
        assert "path of your report file on disk" in p, name
        # PROGRESS ON DISK still names the file the final answer points at.
        assert "PROGRESS ON DISK" in p, name


def test_quick_role_falls_back_to_a_file_only_when_long():
    p = engine.DEFAULT_AGENTS["quick"].prompt
    assert "If the result is longer" in p and "/tmp/cardloop-scratch/" in p


def test_ultracode_complement_demands_reports_on_disk_in_briefs():
    low = engine.ULTRACODE_PROMPT.lower()
    assert "reports live on disk" in low
    assert "final answer = report file path" in low
    assert "schema" in low and "exempt" in low
    # The operator-facing synthesis rule survives.
    assert "synthesis" in low
