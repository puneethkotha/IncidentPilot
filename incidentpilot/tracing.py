"""OpenTelemetry GenAI tracing for the agent's own reasoning.

Emits spans following the OTel GenAI semantic conventions -- an `invoke_agent`
span for the whole investigation, a `chat` span per model turn (with token usage
and finish reason), and an `execute_tool` span per tool call. Point it at any
OTLP backend (Arize Phoenix, SigNoz, Grafana Tempo) to *see* how the agent
reasoned, which is what makes it measurable rather than a black box.

Tracing is a no-op until `setup_tracing()` is called (or an OTLP endpoint is set
in the environment), so importing this module -- and running the test suite --
never needs a collector.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import Span

_CONFIGURED = False


def setup_tracing(service_name: str = "incidentpilot", force: bool = False) -> None:
    """Wire an OTLP exporter if an endpoint is configured. Idempotent."""

    global _CONFIGURED
    if _CONFIGURED and not force:
        return
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        _CONFIGURED = True  # leave the default (no-op) provider in place
        return

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _CONFIGURED = True


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("incidentpilot")


@contextmanager
def agent_span(agent_name: str, incident_id: str) -> Iterator[Span]:
    with get_tracer().start_as_current_span(f"invoke_agent {agent_name}") as span:
        span.set_attribute("gen_ai.operation.name", "invoke_agent")
        span.set_attribute("gen_ai.agent.name", agent_name)
        span.set_attribute("gen_ai.conversation.id", incident_id)
        yield span


@contextmanager
def chat_span(model: str) -> Iterator[Span]:
    with get_tracer().start_as_current_span(f"chat {model}") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.provider.name", "openai")  # Groq is OpenAI-compatible
        span.set_attribute("gen_ai.request.model", model)
        yield span


def record_usage(span: Span, usage: object, finish_reason: str | None) -> None:
    """Attach token usage + finish reason to a chat span, if available."""

    for attr, key in (("prompt_tokens", "gen_ai.usage.input_tokens"),
                      ("completion_tokens", "gen_ai.usage.output_tokens")):
        value = getattr(usage, attr, None)
        if value is not None:
            span.set_attribute(key, int(value))
    if finish_reason:
        span.set_attribute("gen_ai.response.finish_reasons", [finish_reason])


@contextmanager
def tool_span(tool_name: str, call_id: str | None) -> Iterator[Span]:
    with get_tracer().start_as_current_span(f"execute_tool {tool_name}") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", tool_name)
        if call_id:
            span.set_attribute("gen_ai.tool.call.id", call_id)
        yield span
