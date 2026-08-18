"""Postmortem generator: renders the incident's audit trail into markdown."""

from __future__ import annotations

from incidentpilot.models import Incident, Severity
from incidentpilot.postmortem import render_postmortem


def _incident() -> Incident:
    return Incident(
        id="inc-7f3a2b", service="payment-service",
        metric="http_request_duration_seconds:p95", value=2.9, baseline=0.35,
        z_score=8.4, severity=Severity.CRITICAL,
    )


def test_resolved_postmortem_has_all_sections() -> None:
    report = {
        "cause": "DB connection pool exhausted after deploy v412",
        "confidence": 0.82,
        "evidence": [{"tool": "recent_deploys", "summary": "v412 ~90s before onset"}],
        "action": "rollback_deploy", "blast_radius": 0.6,
        "allowed": True, "requires_approval": True, "approved": True,
        "executed": True, "resolved": True,
        "metrics_after": {"http_request_duration_seconds:p95": 0.33},
    }
    md = render_postmortem(_incident(), report)
    assert "# Incident postmortem — inc-7f3a2b" in md
    assert "## Summary" in md and "## Root cause" in md
    assert "## Remediation & authorization" in md and "## Verification" in md
    assert "rollback_deploy" in md
    assert "Status:** Resolved" in md
    assert "granted" in md  # approval granted line
    assert "recent_deploys" in md  # evidence rendered
    assert "confirmed recovery" in md


def test_unresolved_postmortem_notes_rollback() -> None:
    report = {
        "cause": "undetermined", "action": "scale_in", "allowed": True,
        "requires_approval": False, "executed": True, "resolved": False, "metrics_after": {},
    }
    md = render_postmortem(_incident(), report)
    assert "Status:** Unresolved" in md
    assert "rolled back" in md
    assert "not required" in md
