"""Policy + actuator safety tests -- FULLY IMPLEMENTED.

These pin the safety invariants that make IncidentPilot trustworthy:
  * high blast radius in prod  -> requires approval (or denied),
  * low-risk action in dev     -> allowed outright,
  * per-action rate limit      -> blocks after N,
  * propose_only mode          -> never executes.
"""

from __future__ import annotations

from incidentpilot.actions import Actuator, PolicyEngine, RemediationRegistry
from incidentpilot.config import Settings
from incidentpilot.models import ActionType, AuthorizationDecision, RemediationProposal


def _settings(**overrides) -> Settings:
    base = dict(
        mode="auto",
        target_env="dev",
        allowed_envs=["dev", "staging", "prod"],
        blast_radius_auto_threshold=0.30,
        rate_limit_window_seconds=3600,
        default_action_rate_limit=3,
    )
    base.update(overrides)
    return Settings(**base)


def test_high_blast_radius_in_prod_requires_approval_or_denied() -> None:
    policy = PolicyEngine()
    settings = _settings(target_env="prod")
    proposal = RemediationProposal(
        incident_id="inc-1",
        action=ActionType.FAILOVER,
        blast_radius=0.90,
        rationale="cache down",
    )
    decision = policy.authorize(proposal, settings)
    # Either it's gated behind a human, or it's outright denied -- never a silent yes.
    assert decision.requires_approval or not decision.allowed
    assert not (decision.allowed and not decision.requires_approval)


def test_high_blast_radius_denied_when_env_not_allowed() -> None:
    policy = PolicyEngine()
    settings = _settings(target_env="prod", allowed_envs=["dev"])
    proposal = RemediationProposal(
        incident_id="inc-1b",
        action=ActionType.FAILOVER,
        blast_radius=0.90,
    )
    decision = policy.authorize(proposal, settings)
    assert decision.allowed is False


def test_low_risk_action_in_dev_is_allowed() -> None:
    policy = PolicyEngine()
    settings = _settings(target_env="dev")
    proposal = RemediationProposal(
        incident_id="inc-2",
        action=ActionType.CLEAR_CACHE,
        blast_radius=0.10,
        rationale="stale cache",
    )
    decision = policy.authorize(proposal, settings)
    assert decision.allowed is True
    assert decision.requires_approval is False


def test_rate_limit_blocks_after_n() -> None:
    policy = PolicyEngine()
    settings = _settings(
        target_env="dev",
        default_action_rate_limit=2,
        action_rate_limits={"restart_service": 2},
    )

    def restart() -> AuthorizationDecision:
        return policy.authorize(
            RemediationProposal(
                incident_id="inc-3",
                action=ActionType.RESTART_SERVICE,
                blast_radius=0.10,  # below threshold: isolate the rate-limit effect
            ),
            settings,
        )

    assert restart().allowed is True   # 1st
    assert restart().allowed is True   # 2nd
    third = restart()                  # 3rd exceeds limit of 2
    assert third.allowed is False
    assert any("rate limit" in r for r in third.reasons)


def test_propose_only_mode_never_executes() -> None:
    settings = _settings(mode="propose_only", target_env="dev")
    actuator = Actuator(RemediationRegistry())
    proposal = RemediationProposal(
        incident_id="inc-4",
        action=ActionType.CLEAR_CACHE,
        blast_radius=0.05,
    )
    # Even a fully-approved, fully-authorized decision must not execute.
    decision = AuthorizationDecision(allowed=True, requires_approval=False, reasons=["ok"])
    result = actuator.execute(proposal, decision, settings, approved=True)
    assert result.executed is False
    assert "propose_only" in result.message
