"""Tracing test: the agent emits OTel GenAI spans (invoke_agent / chat /
execute_tool) with the expected operation-name attributes."""

from __future__ import annotations

import json

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from incidentpilot.agent import ChatTurn, DiagnosisAgent, ToolCall
from incidentpilot.models import Incident, Severity
from incidentpilot.signals import Signals


def _incident() -> Incident:
    return Incident(
        id="inc-trace",
        service="payment-service",
        metric="p95",
        value=2.9,
        baseline=0.35,
        z_score=8.4,
        severity=Severity.CRITICAL,
    )


def test_agent_emits_genai_spans() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    signals = Signals(
        prometheus_url="http://p",
        target_url="http://t",
        prom_range=lambda _q, _m: [0.35, 2.9],
        prom_instant=lambda _q: 1.0,
        http_get=lambda _p, _params: {"deploys": [], "lines": []},
    )

    turns = {"n": 0}

    def chat(_messages: list[dict]) -> ChatTurn:
        turns["n"] += 1
        if turns["n"] == 1:
            return ChatTurn(tool_calls=[ToolCall("c1", "query_metrics", '{"promql":"p95"}')])
        return ChatTurn(content=json.dumps({"cause": "x", "confidence": 0.5}))

    DiagnosisAgent(signals=signals, chat=chat).diagnose(_incident())

    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    ops = {s.attributes.get("gen_ai.operation.name") for s in spans}

    assert any(n.startswith("invoke_agent") for n in names)
    assert any(n.startswith("chat") for n in names)
    assert any(n.startswith("execute_tool") for n in names)
    assert {"invoke_agent", "chat", "execute_tool"} <= ops
