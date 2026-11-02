"""The suite must never append to the operator's real audit trail (conftest._isolate_engine_audit)."""
from pathlib import Path

import engine

ROOT = Path(__file__).parent.parent


def test_engine_audit_writes_outside_the_real_data_dir():
    real = (ROOT / "data" / "audit").resolve()
    assert Path(engine.AUDIT_DIR).resolve() != real
    engine.audit("isolation-probe", "TEST", "must land in the throw-away dir")
    logs = list(Path(engine.AUDIT_DIR).glob("audit-*.log"))
    assert any("isolation-probe" in p.read_text(encoding="utf-8") for p in logs)
