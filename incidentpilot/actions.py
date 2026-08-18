"""Remediation registry + policy engine + actuator -- THE CRUX, FULLY IMPLEMENTED.

This is the module that makes IncidentPilot "not slop": it separates *what the agent
wants to do* (RemediationProposal) from *what is allowed to happen*
(AuthorizationDecision) from *what actually happens* (Actuator). Every guard is
enforced here, in code, independent of anything the LLM says.

Safety invariants enforced:
  1. Env allowlist       -- refuse to act in an environment not on the allowlist.
  2. Per-action rate limit -- cap executions per action per rolling window.
  3. Blast-radius gate   -- proposals above the threshold require human approval.
  4. propose_only mode   -- never actuate, regardless of authorization.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from incidentpilot.config import Settings
from incidentpilot.models import (
    ActionResult,
    ActionType,
    AuthorizationDecision,
    RemediationProposal,
)

# --------------------------------------------------------------------------- #
# Registry: ActionType -> (executor, default blast radius)
# Executors are STUBS (they don't touch real infra yet) but the wiring is real.
# --------------------------------------------------------------------------- #

Executor = Callable[[RemediationProposal, Settings], dict]

# Which fault each remediation actually heals on the demo control plane. Only the
# *correct* action for an incident clears its fault, so post-action verification
# reflects remediation correctness rather than rubber-stamping any action.
_HEALS: dict[ActionType, str] = {
    ActionType.ROLLBACK_DEPLOY: "pool_exhaust",  # roll back the bad deploy
    ActionType.RESTART_SERVICE: "crash_loop",  # restart a crash-looping worker
    ActionType.FAILOVER: "redis_down",  # fail over to a healthy cache
    ActionType.SCALE_OUT: "latency",  # add capacity to relieve latency
    ActionType.THROTTLE_TRAFFIC: "latency",  # shed load to relieve latency
}


def _executor(action: ActionType) -> Executor:
    """Real executor: acts against the target's control plane over HTTP.

    In a production deploy these would call k8s / a deploy tool / a load
    balancer; here they drive the demo system under test, which is what makes
    the closed loop (act -> verify -> resolved) genuinely observable.
    """

    def _run(proposal: RemediationProposal, settings: Settings) -> dict:
        fault = _HEALS.get(action)
        if fault is None:
            return {"ok": True, "action": action.value, "effect": "no-op (nothing to heal)"}
        import httpx  # lazy

        try:
            httpx.post(f"{settings.target_url}/admin/clear", json={"fault": fault}, timeout=5.0)
            if action is ActionType.ROLLBACK_DEPLOY:
                to_version = proposal.params.get("to_version", "v411")
                httpx.post(
                    f"{settings.target_url}/admin/deploy",
                    json={"version": to_version, "bad": False, "note": "rollback"},
                    timeout=5.0,
                )
            return {"ok": True, "action": action.value, "cleared": fault}
        except Exception as exc:  # noqa: BLE001 - a failed actuation is a result, not a crash
            return {"ok": False, "action": action.value, "error": f"{type(exc).__name__}: {exc}"}

    return _run


class RemediationRegistry:
    """Maps each ActionType to its executor and a default blast radius.

    `default_blast_radius` is what the proposer uses when the agent doesn't
    supply one; the policy engine keys on the proposal's blast_radius.
    """

    def __init__(self) -> None:
        self._registry: dict[ActionType, tuple[Executor, float]] = {
            ActionType.NO_OP: (_executor(ActionType.NO_OP), 0.0),
            ActionType.CLEAR_CACHE: (_executor(ActionType.CLEAR_CACHE), 0.10),
            ActionType.SCALE_OUT: (_executor(ActionType.SCALE_OUT), 0.20),
            ActionType.SCALE_IN: (_executor(ActionType.SCALE_IN), 0.25),
            ActionType.THROTTLE_TRAFFIC: (_executor(ActionType.THROTTLE_TRAFFIC), 0.30),
            ActionType.RESTART_SERVICE: (_executor(ActionType.RESTART_SERVICE), 0.40),
            ActionType.ROLLBACK_DEPLOY: (_executor(ActionType.ROLLBACK_DEPLOY), 0.70),
            ActionType.FAILOVER: (_executor(ActionType.FAILOVER), 0.90),
        }

    def default_blast_radius(self, action: ActionType) -> float:
        return self._registry[action][1]

    def executor(self, action: ActionType) -> Executor:
        return self._registry[action][0]

    def known(self, action: ActionType) -> bool:
        return action in self._registry


class PolicyEngine:
    """Authorizes proposals. Holds rate-limit state (in-memory for the MVP).

    The engine is intentionally conservative: unknown actions and disallowed
    environments are hard-denied; anything above the blast-radius threshold is
    allowed to proceed *only* behind human approval.
    """

    def __init__(self, registry: RemediationRegistry | None = None) -> None:
        self.registry = registry or RemediationRegistry()
        # action.value -> list[monotonic timestamps of authorized attempts]
        self._attempts: dict[str, list[float]] = {}

    def _rate_limited(self, action: ActionType, limit: int, window: float) -> bool:
        """Record this attempt and report whether it exceeds the limit."""

        now = time.monotonic()
        bucket = self._attempts.setdefault(action.value, [])
        cutoff = now - window
        bucket[:] = [t for t in bucket if t >= cutoff]
        bucket.append(now)
        return len(bucket) > limit

    def authorize(
        self, proposal: RemediationProposal, settings: Settings
    ) -> AuthorizationDecision:
        reasons: list[str] = []
        allowed = True
        requires_approval = False

        # (0) Only actions we know how to execute.
        if not self.registry.known(proposal.action):
            return AuthorizationDecision(
                allowed=False,
                requires_approval=False,
                reasons=[f"unknown action: {proposal.action}"],
            )

        # (1) Environment allowlist -- hard deny.
        if settings.target_env not in settings.allowed_envs:
            allowed = False
            reasons.append(
                f"env '{settings.target_env}' not in allowed_envs {settings.allowed_envs}"
            )

        # (2) Per-action rate limit -- hard deny when exceeded.
        limit = settings.action_rate_limits.get(
            proposal.action.value, settings.default_action_rate_limit
        )
        if self._rate_limited(proposal.action, limit, settings.rate_limit_window_seconds):
            allowed = False
            reasons.append(
                f"rate limit exceeded for {proposal.action.value} "
                f"(>{limit}/{settings.rate_limit_window_seconds}s)"
            )

        # (3) Blast-radius gate -- above threshold => needs a human.
        if proposal.blast_radius > settings.blast_radius_auto_threshold:
            requires_approval = True
            reasons.append(
                f"blast_radius {proposal.blast_radius:.2f} > threshold "
                f"{settings.blast_radius_auto_threshold:.2f}; human approval required"
            )

        if allowed and not reasons:
            reasons.append("authorized: within all policy limits")

        return AuthorizationDecision(
            allowed=allowed, requires_approval=requires_approval, reasons=reasons
        )


class Actuator:
    """Executes an authorized proposal -- or refuses, with a reason.

    Refuses when:
      * mode == propose_only (never actuates),
      * the decision is not allowed,
      * the decision requires approval and none was granted.
    """

    def __init__(self, registry: RemediationRegistry | None = None) -> None:
        self.registry = registry or RemediationRegistry()

    def execute(
        self,
        proposal: RemediationProposal,
        decision: AuthorizationDecision,
        settings: Settings,
        approved: bool = False,
    ) -> ActionResult:
        # Guard 1: propose_only never touches anything.
        if settings.mode == "propose_only":
            return ActionResult(
                action=proposal.action,
                executed=False,
                success=False,
                message="mode=propose_only: proposal recorded, not executed",
            )

        # Guard 2: policy said no.
        if not decision.allowed:
            return ActionResult(
                action=proposal.action,
                executed=False,
                success=False,
                message="not authorized: " + "; ".join(decision.reasons),
            )

        # Guard 3: needs a human and didn't get one.
        if decision.requires_approval and not approved:
            return ActionResult(
                action=proposal.action,
                executed=False,
                success=False,
                message="awaiting human approval (high blast radius)",
            )

        # All guards passed -- run the executor against the target control plane.
        executor = self.registry.executor(proposal.action)
        output = executor(proposal, settings)
        ok = bool(output.get("ok", True))
        return ActionResult(
            action=proposal.action,
            executed=True,
            success=ok,
            message=(
                f"executed {proposal.action.value} in {settings.target_env}"
                if ok
                else f"actuation failed: {output.get('error', 'unknown')}"
            ),
            output=output,
        )
