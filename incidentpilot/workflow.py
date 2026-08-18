"""Durable incident-handling workflow (DBOS).

The whole loop -- diagnose -> propose -> authorize -> (await approval) -> act ->
verify -> rollback-if-unresolved -- runs as a DBOS workflow. Each @DBOS.step()
checkpoints to durable storage, so a crash mid-remediation resumes from the last
completed step (exactly-once side effects), and the human-approval wait survives
a process restart. Live progress is published with DBOS.set_event so a dashboard
can follow along; the approval is delivered with DBOS.send from the API.

Importing this module registers the decorated functions but connects to nothing;
DBOS is launched in incidentpilot.api.main().
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable

from dbos import DBOS

from incidentpilot.actions import Actuator, PolicyEngine, RemediationRegistry
from incidentpilot.agent import DiagnosisAgent
from incidentpilot.config import get_settings
from incidentpilot.models import (
    ActionResult,
    ActionType,
    AuthorizationDecision,
    Incident,
    RemediationProposal,
    RootCauseHypothesis,
    VerificationResult,
)
from incidentpilot.monitor import is_recovered, promql_for
from incidentpilot.signals import Signals, build_signals

STATUS_EVENT = "status"  # DBOS event key a dashboard reads for live progress
APPROVAL_TOPIC = "approval"

_registry = RemediationRegistry()
_policy = PolicyEngine(_registry)

# Injection seams so tests can supply a deterministic agent / offline signals.
AgentFactory = Callable[[], DiagnosisAgent]
SignalsFactory = Callable[[], Signals]
AGENT_FACTORY: AgentFactory = DiagnosisAgent
SIGNALS_FACTORY: SignalsFactory = build_signals


def _set_status(phase: str, **extra: object) -> None:
    # Status is best-effort telemetry for the dashboard; never let it fail a step.
    with contextlib.suppress(Exception):
        DBOS.set_event(STATUS_EVENT, {"phase": phase, **extra})


# --------------------------------------------------------------------------- #
# Steps: durable, checkpointed units. Side-effecting steps (act/rollback) are
# guarded upstream by the PolicyEngine + Actuator.
# --------------------------------------------------------------------------- #


@DBOS.step()
def diagnose_step(incident: Incident) -> RootCauseHypothesis:
    _set_status("diagnosing", incident_id=incident.id)
    return AGENT_FACTORY().diagnose(incident)


@DBOS.step()
def propose_step(incident: Incident, hypothesis: RootCauseHypothesis) -> RemediationProposal:
    action = hypothesis.recommended_action or ActionType.NO_OP
    proposal = RemediationProposal(
        incident_id=incident.id,
        action=action,
        params={},
        blast_radius=_registry.default_blast_radius(action),
        rationale=hypothesis.cause,
    )
    _set_status("proposed", action=action.value, blast_radius=proposal.blast_radius)
    return proposal


@DBOS.step()
def authorize_step(proposal: RemediationProposal) -> AuthorizationDecision:
    decision = _policy.authorize(proposal, get_settings())
    _set_status(
        "authorized",
        allowed=decision.allowed,
        requires_approval=decision.requires_approval,
        reasons=decision.reasons,
    )
    return decision


@DBOS.step()
def act_step(
    proposal: RemediationProposal, decision: AuthorizationDecision, approved: bool
) -> ActionResult:
    # The Actuator refuses in propose_only / unauthorized / unapproved cases, so
    # this step is safe to (re-)run on resume.
    result = Actuator(_registry).execute(proposal, decision, get_settings(), approved=approved)
    _set_status("acted", executed=result.executed, success=result.success, message=result.message)
    return result


@DBOS.step()
def verify_step(incident: Incident, result: ActionResult) -> VerificationResult:
    """Poll the incident's own metric until it recovers (or times out)."""

    if not result.executed:
        return VerificationResult(
            resolved=False, notes="no action executed (propose-only or refused)"
        )

    settings = get_settings()
    signals = SIGNALS_FACTORY()
    promql = promql_for(incident.service, incident.metric)
    if promql is None:
        return VerificationResult(resolved=False, notes=f"no promql for metric {incident.metric}")

    deadline = time.monotonic() + settings.verify_timeout_seconds
    last: float | None = None
    while True:
        summary = signals.query_metrics(promql, minutes=1)
        last = summary.get("last")
        if is_recovered(incident.metric, last, incident.baseline, settings.recovery_factor):
            _set_status("verified", resolved=True, value=last)
            return VerificationResult(
                resolved=True, metrics_after={incident.metric: last}, notes="signal recovered"
            )
        if time.monotonic() >= deadline:
            _set_status("verified", resolved=False, value=last)
            return VerificationResult(
                resolved=False,
                metrics_after={incident.metric: last} if last is not None else {},
                notes="signal did not recover within timeout",
            )
        time.sleep(settings.verify_poll_interval_seconds)


@DBOS.step()
def rollback_step(proposal: RemediationProposal, result: ActionResult) -> ActionResult:
    """Revert the (unresolved) remediation. The demo's remediations only clear
    faults, so a rollback is a recorded no-op; the structure is what matters."""

    _set_status("rolling_back", of=proposal.action.value)
    return ActionResult(
        action=ActionType.NO_OP,
        executed=True,
        success=True,
        message=f"rolled back {proposal.action.value} (unresolved)",
    )


# --------------------------------------------------------------------------- #
# Workflow: replayable; DBOS skips completed steps on resume.
# --------------------------------------------------------------------------- #


@DBOS.workflow()
def handle_incident(incident: Incident) -> VerificationResult:
    _set_status("opened", incident_id=incident.id, service=incident.service)
    hypothesis = diagnose_step(incident)
    proposal = propose_step(incident, hypothesis)
    decision = authorize_step(proposal)

    approved = False
    if decision.allowed and decision.requires_approval:
        _set_status("awaiting_approval", action=proposal.action.value)
        # Durable wait for a human. The process can crash/redeploy while parked
        # here; on resume we are still waiting, and api.approve() delivers via send.
        approval = DBOS.recv(APPROVAL_TOPIC, timeout_seconds=24 * 3600)
        approved = bool(approval and approval.get("approved"))
        _set_status("approval_received", approved=approved)

    result = act_step(proposal, decision, approved)
    verification = verify_step(incident, result)

    if result.executed and not verification.resolved:
        rollback_step(proposal, result)

    _set_status("resolved" if verification.resolved else "closed", resolved=verification.resolved)
    return verification
