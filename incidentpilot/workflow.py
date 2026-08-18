"""Durable incident-handling workflow (DBOS) -- orchestration skeleton.

Why DBOS: it gives us durable, exactly-once execution on top of Postgres alone
(no new infra -- reuses the DB we already run). Each @DBOS.step() checkpoints its
result to Postgres; if the process crashes mid-incident, DBOS re-runs the
workflow from the *last completed step* rather than re-doing side effects. That
is exactly what you want when a "step" is "restart the payments service": you
must never double-execute it, and you must survive a redeploy while waiting on a
human approval.

Flow: diagnose -> propose -> authorize -> (await approval if required) -> act
      -> verify -> rollback-if-unresolved.

Importing this module registers the decorated functions but does NOT connect to
Postgres. Connection happens in api.main() via DBOS.launch().
"""

from __future__ import annotations

from dbos import DBOS

from incidentpilot.actions import Actuator, PolicyEngine, RemediationRegistry
from incidentpilot.agent import DiagnosisAgent
from incidentpilot.config import get_settings
from incidentpilot.models import (
    ActionResult,
    AuthorizationDecision,
    Incident,
    RemediationProposal,
    RootCauseHypothesis,
    VerificationResult,
)

_registry = RemediationRegistry()
_policy = PolicyEngine(_registry)


# --------------------------------------------------------------------------- #
# Steps: each is a durable, checkpointed unit. Keep them deterministic given
# their inputs so DBOS can safely resume. Side-effecting steps (act/rollback)
# are guarded upstream by the PolicyEngine + Actuator.
# --------------------------------------------------------------------------- #


@DBOS.step()
def diagnose_step(incident: Incident) -> RootCauseHypothesis:
    return DiagnosisAgent().diagnose(incident)


@DBOS.step()
def propose_step(incident: Incident, hypothesis: RootCauseHypothesis) -> RemediationProposal:
    action = hypothesis.recommended_action
    if action is None:
        from incidentpilot.models import ActionType

        action = ActionType.NO_OP
    return RemediationProposal(
        incident_id=incident.id,
        action=action,
        params={},
        blast_radius=_registry.default_blast_radius(action),
        rationale=hypothesis.cause,
    )


@DBOS.step()
def authorize_step(proposal: RemediationProposal) -> AuthorizationDecision:
    return _policy.authorize(proposal, get_settings())


@DBOS.step()
def act_step(
    proposal: RemediationProposal,
    decision: AuthorizationDecision,
    approved: bool,
) -> ActionResult:
    # The Actuator itself refuses in propose_only / unauthorized / unapproved
    # cases, so this step is safe to (re-)run.
    return Actuator(_registry).execute(proposal, decision, get_settings(), approved=approved)


@DBOS.step()
def verify_step(incident: Incident, result: ActionResult) -> VerificationResult:
    # TODO: re-query the incident's metric via signals.query_metrics and compare
    # against baseline to decide `resolved`. Stubbed as unresolved-if-not-executed.
    return VerificationResult(
        resolved=result.executed and result.success,
        metrics_after={},
        notes="TODO: real post-action metric verification",
    )


@DBOS.step()
def rollback_step(proposal: RemediationProposal) -> ActionResult:
    # TODO: map each action to its inverse (scale_out -> scale_in, etc.).
    from incidentpilot.models import ActionType

    return Actuator(_registry).execute(
        RemediationProposal(
            incident_id=proposal.incident_id,
            action=ActionType.NO_OP,
            blast_radius=0.0,
            rationale=f"rollback of {proposal.action.value}",
        ),
        AuthorizationDecision(allowed=True, requires_approval=False, reasons=["rollback"]),
        get_settings(),
        approved=True,
    )


# --------------------------------------------------------------------------- #
# Workflow: the durable orchestrator. The whole function is replayable; DBOS
# skips already-completed steps on resume, giving exactly-once side effects.
# --------------------------------------------------------------------------- #


@DBOS.workflow()
def handle_incident(incident: Incident) -> VerificationResult:
    hypothesis = diagnose_step(incident)
    proposal = propose_step(incident, hypothesis)
    decision = authorize_step(proposal)

    approved = False
    if decision.allowed and decision.requires_approval:
        # Durable wait for a human. DBOS.recv() blocks the workflow but the
        # process can crash/redeploy meanwhile -- on resume we're still waiting
        # here, and api.approve() delivers the decision via DBOS.send().
        approval = DBOS.recv("approval", timeout_seconds=24 * 3600)
        approved = bool(approval and approval.get("approved"))

    result = act_step(proposal, decision, approved)
    verification = verify_step(incident, result)

    # Close the loop: if we acted but the metric didn't recover, roll back.
    if result.executed and not verification.resolved:
        rollback_step(proposal)

    return verification
