"""Manual end-to-end of the full durable loop against the live stack (`make stack`).

Stubs only the LLM (so no key is needed); everything else is real: a durable
DBOS workflow diagnoses, proposes a rollback, waits for human approval, executes
a real remediation against the target, and verifies the metric recovered.

    make stack
    python scripts/smoke_loop.py
"""

from __future__ import annotations

import tempfile
import time

import httpx
from dbos import DBOS, SetWorkflowID

import incidentpilot.workflow as wf
from incidentpilot.config import Settings
from incidentpilot.models import ActionType, Incident, RootCauseHypothesis, Severity

TARGET = "http://localhost:8080"


class _StubAgent:
    def diagnose(self, _incident: Incident) -> RootCauseHypothesis:
        return RootCauseHypothesis(
            cause="DB connection pool exhausted after deploy v412",
            confidence=0.9,
            recommended_action=ActionType.ROLLBACK_DEPLOY,
        )


def main() -> None:
    wf.AGENT_FACTORY = lambda: _StubAgent()
    wf.get_settings = lambda: Settings(
        mode="auto",
        target_env="dev",
        allowed_envs=["dev", "staging", "prod"],
        blast_radius_auto_threshold=0.30,
        verify_timeout_seconds=120.0,
    )

    d = tempfile.mkdtemp()
    DBOS(config={"name": "ipsmoke", "system_database_url": f"sqlite:///{d}/s.sqlite"})
    DBOS.launch()
    try:
        httpx.post(f"{TARGET}/admin/reset", timeout=5)
        time.sleep(3)
        print(">>> inject bad deploy v412; waiting for p95 to break ...")
        httpx.post(f"{TARGET}/admin/deploy", json={"version": "v412", "bad": True}, timeout=5)
        time.sleep(18)

        incident = Incident(
            id="inc-smoke-loop",
            service="payment-service",
            metric="http_request_duration_seconds:p95",
            value=1.4,
            baseline=0.01,
            z_score=16.0,
            severity=Severity.CRITICAL,
        )
        with SetWorkflowID("smoke-loop"):
            handle = DBOS.start_workflow(wf.handle_incident, incident)
        time.sleep(2)
        print("status:", DBOS.get_event("smoke-loop", wf.STATUS_EVENT, timeout_seconds=2))
        print(">>> approving the rollback ...")
        DBOS.send("smoke-loop", {"approved": True, "approver": "you"}, topic=wf.APPROVAL_TOPIC)
        result = handle.get_result()
        print(f"\nresolved={result.resolved}  notes={result.notes!r}  after={result.metrics_after}")
    finally:
        DBOS.destroy()


if __name__ == "__main__":
    main()
