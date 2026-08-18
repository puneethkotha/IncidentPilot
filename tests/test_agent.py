"""Agent tests: the tool-calling loop reaches a correct, structured diagnosis
using a deterministic mock LLM and injected (offline) signals."""

from __future__ import annotations

import json

from incidentpilot.agent import ChatTurn, DiagnosisAgent, ToolCall
from incidentpilot.models import ActionType, Incident, Severity
from incidentpilot.signals import Signals


def _incident() -> Incident:
    return Incident(
        id="inc-test",
        service="payment-service",
        metric="http_request_duration_seconds:p95",
        value=2.9,
        baseline=0.35,
        z_score=8.4,
        severity=Severity.CRITICAL,
    )


def _signals() -> Signals:
    def http_get(path: str, _params: dict) -> dict:
        if "deploys" in path:
            return {"deploys": [{"version": "v412", "bad": True, "at": "14:00Z", "by": "ci"}]}
        return {"lines": [{"level": "ERROR", "msg": "timeout acquiring connection"}] * 3}

    return Signals(
        prometheus_url="http://p",
        target_url="http://t",
        prom_range=lambda _q, _m: [0.35] * 10 + [2.9],
        prom_instant=lambda q: 1.8 if "pool" in q else 2.0,
        http_get=http_get,
    )


class _MockLLM:
    """Turn 1: call two tools. Turn 2: emit the final JSON diagnosis."""

    def __init__(self) -> None:
        self.turns = 0
        self.saw_tool_results = False

    def __call__(self, messages: list[dict]) -> ChatTurn:
        self.turns += 1
        if self.turns == 1:
            return ChatTurn(
                tool_calls=[
                    ToolCall("c1", "recent_deploys", '{"service":"payment-service"}'),
                    ToolCall("c2", "query_metrics", '{"promql":"p95","minutes":15}'),
                ]
            )
        # confirm the tool results were fed back before the final answer
        self.saw_tool_results = any(m.get("role") == "tool" for m in messages)
        return ChatTurn(
            content=json.dumps(
                {
                    "cause": "payment-service DB connection pool exhausted after deploy v412",
                    "confidence": 0.82,
                    "evidence": [{"tool": "recent_deploys", "summary": "v412 ~90s before onset"}],
                    "recommended_action": "rollback_deploy",
                }
            )
        )


def test_agent_reaches_structured_diagnosis() -> None:
    llm = _MockLLM()
    agent = DiagnosisAgent(signals=_signals(), chat=llm, max_iters=6)
    hyp = agent.diagnose(_incident())

    assert llm.saw_tool_results is True
    assert "pool" in hyp.cause.lower()
    assert hyp.confidence == 0.82
    assert hyp.recommended_action == ActionType.ROLLBACK_DEPLOY
    assert hyp.evidence and hyp.evidence[0].tool == "recent_deploys"


def test_agent_parses_fenced_json() -> None:
    def chat(_messages: list[dict]) -> ChatTurn:
        payload = '{"cause":"x","confidence":1.5,"recommended_action":"scale_out"}'
        return ChatTurn(content=f"```json\n{payload}\n```")

    agent = DiagnosisAgent(signals=_signals(), chat=chat)
    hyp = agent.diagnose(_incident())
    assert hyp.cause == "x"
    assert hyp.confidence == 1.0  # clamped
    assert hyp.recommended_action == ActionType.SCALE_OUT


def test_agent_rejects_unknown_action() -> None:
    def chat(_messages: list[dict]) -> ChatTurn:
        return ChatTurn(content='{"cause":"x","confidence":0.5,"recommended_action":"nuke_prod"}')

    hyp = DiagnosisAgent(signals=_signals(), chat=chat).diagnose(_incident())
    assert hyp.recommended_action is None  # not in the closed enum -> dropped


def test_agent_inconclusive_when_budget_exhausted() -> None:
    def chat(_messages: list[dict]) -> ChatTurn:
        return ChatTurn(tool_calls=[ToolCall("c", "query_metrics", '{"promql":"p95"}')])

    hyp = DiagnosisAgent(signals=_signals(), chat=chat, max_iters=3).diagnose(_incident())
    assert hyp.confidence == 0.0
    assert "inconclusive" in hyp.cause
