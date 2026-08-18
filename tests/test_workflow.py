"""Durable-workflow tests: propose-only safety, gated auto-remediation, the
human-approval path, post-action verification, and exactly-once execution.

Runs a real DBOS workflow against a temporary SQLite system database, with an
injected deterministic agent and offline signals (no LLM, no network)."""

from __future__ import annotations

import tempfile

import httpx
import pytest
from dbos import DBOS, SetWorkflowID

import incidentpilot.workflow as wf
from incidentpilot.config import Settings
from incidentpilot.models import ActionType, Incident, RootCauseHypothesis, Severity


@pytest.fixture(scope="module", autouse=True)
def dbos_runtime():
    d = tempfile.mkdtemp()
    DBOS(config={"name": "iptest", "system_database_url": f"sqlite:///{d}/sys.sqlite"})
    DBOS.launch()
    yield
    DBOS.destroy()


def _settings(**overrides) -> Settings:
    base = {
        "mode": "auto",
        "target_env": "dev",
        "allowed_envs": ["dev", "staging", "prod"],
        "blast_radius_auto_threshold": 0.30,
    }
    base.update(overrides)
    return Settings(**base)


class _FakeAgent:
    def __init__(self, action: ActionType) -> None:
        self.action = action

    def diagnose(self, incident: Incident) -> RootCauseHypothesis:
        return RootCauseHypothesis(
            cause="fake cause", confidence=0.9, recommended_action=self.action
        )


class _FakeSignals:
    def __init__(self, last: float) -> None:
        self.last = last

    def query_metrics(self, promql: str, minutes: int = 1) -> dict:
        return {"last": self.last}


def _incident() -> Incident:
    return Incident(
        id="inc-wf",
        service="payment-service",
        metric="http_request_duration_seconds:p95",
        value=1.4,
        baseline=0.01,
        z_score=16.0,
        severity=Severity.CRITICAL,
    )


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Stub the actuator's HTTP calls so tests never hit a real target."""

    calls = {"n": 0}

    def fake_post(*_a, **_k):
        calls["n"] += 1
        return type("R", (), {"raise_for_status": lambda self: None})()

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def _wire(monkeypatch, *, action: ActionType, recovered_last: float, settings: Settings) -> None:
    monkeypatch.setattr(wf, "AGENT_FACTORY", lambda: _FakeAgent(action))
    monkeypatch.setattr(wf, "SIGNALS_FACTORY", lambda: _FakeSignals(recovered_last))
    monkeypatch.setattr(wf, "get_settings", lambda: settings)


def test_propose_only_never_actuates(monkeypatch, _no_network):
    _wire(monkeypatch, action=ActionType.SCALE_OUT, recovered_last=0.02,
          settings=_settings(mode="propose_only"))
    result = wf.handle_incident(_incident())
    assert result.resolved is False
    assert "propose-only" in result.notes or "refused" in result.notes
    assert _no_network["n"] == 0  # executor never ran


def test_auto_low_blast_acts_and_verifies_recovery(monkeypatch, _no_network):
    # scale_out has blast 0.20 < 0.30 threshold -> no approval needed.
    _wire(monkeypatch, action=ActionType.SCALE_OUT, recovered_last=0.02, settings=_settings())
    result = wf.handle_incident(_incident())
    assert result.resolved is True
    assert _no_network["n"] >= 1  # executor ran


def test_high_blast_requires_and_receives_approval(monkeypatch, _no_network):
    # rollback_deploy has blast 0.70 > 0.30 -> workflow parks on approval.
    _wire(monkeypatch, action=ActionType.ROLLBACK_DEPLOY, recovered_last=0.02, settings=_settings())
    with SetWorkflowID("wf-approval"):
        handle = DBOS.start_workflow(wf.handle_incident, _incident())
    DBOS.send("wf-approval", {"approved": True, "approver": "puneeth"}, topic=wf.APPROVAL_TOPIC)
    result = handle.get_result()
    assert result.resolved is True


def test_unresolved_action_triggers_rollback(monkeypatch, _no_network):
    # Verifier never sees recovery (and a tiny timeout), so rollback runs.
    _wire(monkeypatch, action=ActionType.SCALE_OUT, recovered_last=99.0,
          settings=_settings(verify_timeout_seconds=0.1, verify_poll_interval_seconds=0.05))
    result = wf.handle_incident(_incident())
    assert result.resolved is False
    assert "did not recover" in result.notes


def test_exactly_once_on_replay(monkeypatch, _no_network):
    _wire(monkeypatch, action=ActionType.SCALE_OUT, recovered_last=0.02, settings=_settings())
    with SetWorkflowID("wf-idem"):
        r1 = DBOS.start_workflow(wf.handle_incident, _incident()).get_result()
    after_first = _no_network["n"]
    # Same workflow id -> DBOS returns the recorded result without re-executing.
    with SetWorkflowID("wf-idem"):
        r2 = DBOS.start_workflow(wf.handle_incident, _incident()).get_result()
    assert r1.resolved == r2.resolved is True
    assert _no_network["n"] == after_first  # no second actuation
