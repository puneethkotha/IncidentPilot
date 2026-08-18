"""Manual smoke for the diagnosis agent against the live stack (`make stack`).

Injects a bad deploy, then:
  * with GROQ_API_KEY set  -> runs the full agent and prints its root-cause hypothesis;
  * without a key          -> prints the raw signal-tool evidence the agent would use.

    make stack
    GROQ_API_KEY=... python scripts/smoke_agent.py     # full agent
    python scripts/smoke_agent.py                       # evidence only (no key)
"""

from __future__ import annotations

import json
import time

import httpx

from incidentpilot.config import get_settings
from incidentpilot.models import Incident, Severity
from incidentpilot.signals import build_signals

TARGET = "http://localhost:8080"


def _incident() -> Incident:
    return Incident(
        id="inc-smoke",
        service="payment-service",
        metric="http_request_duration_seconds:p95",
        value=1.4,
        baseline=0.01,
        z_score=16.0,
        severity=Severity.CRITICAL,
        description="payment-service p95 latency breach",
    )


def main() -> None:
    httpx.post(f"{TARGET}/admin/reset", timeout=5)
    print(">>> injecting bad deploy v412 (pool exhaustion); letting it build ...")
    httpx.post(f"{TARGET}/admin/deploy", json={"version": "v412", "bad": True}, timeout=5)
    time.sleep(12)

    signals = build_signals()
    if not get_settings().groq_api_key:
        print("\nNo GROQ_API_KEY — printing the evidence the agent would reason over:\n")
        print("deploys:", json.dumps(signals.recent_deploys("payment-service")["deploys"][:1]))
        print("logs   :", json.dumps(signals.query_logs("payment-service")["top_errors"]))
        print("traces :", json.dumps(signals.get_traces("payment-service")))
        httpx.post(f"{TARGET}/admin/reset", timeout=5)
        return

    from incidentpilot.agent import DiagnosisAgent
    from incidentpilot.tracing import setup_tracing

    setup_tracing()
    hyp = DiagnosisAgent(signals=signals).diagnose(_incident())
    print("\n=== ROOT-CAUSE HYPOTHESIS ===")
    print(hyp.model_dump_json(indent=2))
    httpx.post(f"{TARGET}/admin/reset", timeout=5)


if __name__ == "__main__":
    main()
