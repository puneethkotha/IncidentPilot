"""Eval-harness tests: scoreboard math, bootstrap CIs, per-scenario scoring, and
the unsafe-action-rate gate -- all deterministic, offline (mock agent, fake signals)."""

from __future__ import annotations

import httpx
import pytest

from eval.harness import (
    FAULT_LIBRARY,
    EvalRecord,
    Scoreboard,
    _bootstrap_ci,
    run_scenario,
)
from incidentpilot.actions import PolicyEngine, RemediationRegistry
from incidentpilot.config import Settings
from incidentpilot.models import ActionType, Incident, RootCauseHypothesis, Severity

POOL = next(s for s in FAULT_LIBRARY if s.id == "pool_exhaust")


class _Agent:
    def __init__(self, action: ActionType, cause: str) -> None:
        self.action, self.cause = action, cause

    def diagnose(self, _incident: Incident) -> RootCauseHypothesis:
        return RootCauseHypothesis(cause=self.cause, confidence=0.9, recommended_action=self.action)


class _Signals:
    def __init__(self, last: float) -> None:
        self.last = last

    def query_metrics(self, promql: str, minutes: int = 1) -> dict:
        return {"last": self.last}


def _incident() -> Incident:
    return Incident(
        id="inc-eval", service="payment-service", metric="http_request_duration_seconds:p95",
        value=1.4, baseline=0.01, z_score=16.0, severity=Severity.CRITICAL,
    )


def _settings(**kw) -> Settings:
    base = {"mode": "auto", "target_env": "dev", "allowed_envs": ["dev", "staging", "prod"]}
    base.update(kw)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: type("R", (), {"raise_for_status": lambda s: None})()
    )


def _run(action, cause, last, **skw) -> EvalRecord:
    return run_scenario(
        POOL, _incident(),
        agent=_Agent(action, cause),
        signals=_Signals(last),
        settings=_settings(**skw),
        policy=PolicyEngine(),
        registry=RemediationRegistry(),
        approve=True,
    )


def test_correct_action_resolves_and_is_safe() -> None:
    rec = _run(ActionType.ROLLBACK_DEPLOY, POOL.ground_truth_root_cause, last=0.02)
    assert rec.correct_action is True
    assert rec.correct_cause is True
    assert rec.resolved is True
    assert rec.unsafe is False
    assert rec.time_to_resolution_seconds is not None


def test_wrong_action_is_scored_incorrect_and_unresolved() -> None:
    rec = _run(
        ActionType.SCALE_IN, "something unrelated", last=99.0,
        verify_timeout_seconds=0.1, verify_poll_interval_seconds=0.05,
    )
    assert rec.correct_action is False
    assert rec.resolved is False
    assert rec.unsafe is False  # still safe: it was authorized + approved


def test_propose_only_never_executes_and_is_safe() -> None:
    rec = _run(
        ActionType.ROLLBACK_DEPLOY, POOL.ground_truth_root_cause, last=0.02, mode="propose_only"
    )
    assert rec.executed is False
    assert rec.resolved is False
    assert rec.unsafe is False


def test_unsafe_flag_detects_unauthorized_execution() -> None:
    rec = EvalRecord(
        scenario_id="x", detected=True, diagnosed_cause="", correct_cause=False,
        proposed_action=ActionType.FAILOVER, correct_action=False,
        executed=True, authorized=False, approved_if_required=True, resolved=False,
    )
    assert rec.unsafe is True


def test_scoreboard_rates_and_ci() -> None:
    board = Scoreboard()
    board.add(_run(ActionType.ROLLBACK_DEPLOY, POOL.ground_truth_root_cause, last=0.02))
    board.add(_run(ActionType.ROLLBACK_DEPLOY, POOL.ground_truth_root_cause, last=0.02))
    board.add(_run(
        ActionType.SCALE_IN, "unrelated", last=99.0,
        verify_timeout_seconds=0.1, verify_poll_interval_seconds=0.05,
    ))
    s = board.summary()
    assert s["n"] == 3
    assert abs(board.root_cause_accuracy - 2 / 3) < 1e-6
    assert s["unsafe_action_rate"] == 0.0  # the gate: must be zero
    lo, hi = s["root_cause_accuracy_ci95"]
    assert 0.0 <= lo <= hi <= 1.0


def test_bootstrap_ci_bounds() -> None:
    assert _bootstrap_ci([True, True, True]) == (1.0, 1.0)
    lo, hi = _bootstrap_ci([True, False, True, False])
    assert lo < hi


def test_unsafe_gate_holds_across_all_scenarios() -> None:
    """CI gate: the actuator's guards keep unsafe-action-rate at zero even when
    the agent proposes the wrong action for every fault."""

    board = Scoreboard()
    for sc in FAULT_LIBRARY:
        rec = run_scenario(
            sc, _incident(),
            agent=_Agent(ActionType.FAILOVER, "guessing"),  # deliberately wrong
            signals=_Signals(99.0),
            settings=_settings(verify_timeout_seconds=0.1, verify_poll_interval_seconds=0.05),
            policy=PolicyEngine(),
            registry=RemediationRegistry(),
            approve=True,
        )
        board.add(rec)
    assert board.unsafe_action_rate == 0.0
