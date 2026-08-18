"""DiagnosisAgent -- a tool-calling loop that root-causes an incident.

The loop is provider-agnostic and fully testable: the model call is isolated
behind `_chat`, which either delegates to an injected callable (a deterministic
mock in tests) or to Groq with a model fallback ladder + retries. Every turn and
every tool call is wrapped in OpenTelemetry GenAI spans, so the agent's own
reasoning is traceable.

The agent can only ever *return a structured hypothesis*; it never acts.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from incidentpilot.config import Settings, get_settings
from incidentpilot.models import ActionType, Evidence, Incident, RootCauseHypothesis
from incidentpilot.signals import TOOL_SCHEMAS, Signals, build_signals
from incidentpilot.tracing import agent_span, chat_span, record_usage, tool_span

SYSTEM_PROMPT = """You are IncidentPilot, a precise Site Reliability Engineer.

You are given an incident on a service. Investigate it by calling tools, then
return a root-cause hypothesis. Work like an SRE, not a log-guesser:

1. Confirm the break with query_metrics.
2. Call recent_deploys FIRST -- an onset that lines up within a few minutes of a
   rollout is the single strongest root-cause signal.
3. Use query_logs for the dominant error, get_traces to localize the slow span,
   and service_dependencies to reason about topology.
4. Consult read_runbook for the matching symptom before deciding a remediation.

Rules:
- Be evidence-driven; every claim must rest on a tool result you actually saw.
- Prefer the lowest-risk reversible remediation that addresses the *cause*.
- Stop calling tools as soon as you can justify a cause.

When done, return ONLY a strict JSON object (no prose, no code fences) with keys:
  {"cause": str,
   "confidence": number 0..1,
   "evidence": [{"tool": str, "summary": str}],
   "recommended_action": one of
     [no_op, restart_service, rollback_deploy, scale_out, scale_in,
      clear_cache, failover, throttle_traffic]}
"""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string, as the model emitted it


@dataclass
class ChatTurn:
    """Provider-agnostic model turn (what `_chat` returns)."""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


ChatFn = Callable[[list[dict[str, Any]]], ChatTurn]


class DiagnosisAgent:
    def __init__(
        self,
        settings: Settings | None = None,
        signals: Signals | None = None,
        chat: ChatFn | None = None,
        max_iters: int = 8,
    ) -> None:
        self.settings = settings or get_settings()
        self.signals = signals or build_signals(self.settings)
        self.tools = self.signals.tools()
        self._chat_fn = chat
        self.max_iters = max_iters

    # --- model call boundary -------------------------------------------- #
    def _chat(self, messages: list[dict[str, Any]]) -> ChatTurn:
        if self._chat_fn is not None:
            with chat_span("mock"):
                return self._chat_fn(messages)
        return self._chat_groq(messages)

    def _chat_groq(self, messages: list[dict[str, Any]]) -> ChatTurn:
        """Real call: walk the model fallback ladder, retry transient errors."""

        from openai import OpenAI  # lazy import

        client = OpenAI(
            api_key=self.settings.groq_api_key,
            base_url=self.settings.groq_base_url,
            timeout=self.settings.llm_timeout_seconds,
        )
        models = [self.settings.llm_model, *self.settings.llm_fallback_models]
        last_err: Exception | None = None
        for model in models:
            for attempt in range(self.settings.llm_max_retries):
                try:
                    with chat_span(model) as span:
                        resp = client.chat.completions.create(
                            model=model,
                            messages=messages,
                            tools=TOOL_SCHEMAS,
                            tool_choice="auto",
                            temperature=0.0,
                        )
                        choice = resp.choices[0]
                        record_usage(span, resp.usage, choice.finish_reason)
                        return _normalize(choice.message)
                except Exception as exc:  # noqa: BLE001 - retry, then fall to next model
                    last_err = exc
                    time.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"all LLM models failed: {last_err}")

    # --- tool dispatch -------------------------------------------------- #
    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        fn = self.tools.get(name)
        if fn is None:
            return {"error": f"unknown tool: {name}"}
        try:
            return fn(**args)
        except Exception as exc:  # noqa: BLE001 - surface to the model, keep the loop alive
            return {"error": f"{type(exc).__name__}: {exc}"}

    # --- main loop ------------------------------------------------------ #
    def diagnose(self, incident: Incident) -> RootCauseHypothesis:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": incident.model_dump_json()},
        ]

        with agent_span("incidentpilot", incident.id):
            for _ in range(self.max_iters):
                turn = self._chat(messages)
                if not turn.tool_calls:
                    return self._parse_final(turn.content or "")

                messages.append(
                    {
                        "role": "assistant",
                        "content": turn.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": tc.name, "arguments": tc.arguments},
                            }
                            for tc in turn.tool_calls
                        ],
                    }
                )
                for tc in turn.tool_calls:
                    try:
                        args = json.loads(tc.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    with tool_span(tc.name, tc.id):
                        result = self._dispatch(tc.name, args)
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)}
                    )

        return RootCauseHypothesis(
            cause="inconclusive: iteration budget exhausted", confidence=0.0, evidence=[]
        )

    def _parse_final(self, content: str) -> RootCauseHypothesis:
        data = _extract_json(content)
        if data is None:
            return RootCauseHypothesis(
                cause=content.strip()[:200] or "unparseable model output", confidence=0.2
            )
        evidence = [
            Evidence(tool=e.get("tool", "?"), summary=e.get("summary", ""))
            for e in data.get("evidence", [])
            if isinstance(e, dict)
        ]
        return RootCauseHypothesis(
            cause=data.get("cause", "unspecified"),
            confidence=_clamp(data.get("confidence", 0.5)),
            evidence=evidence,
            recommended_action=_coerce_action(data.get("recommended_action")),
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _normalize(message: Any) -> ChatTurn:
    """Normalize an OpenAI-compatible message into a provider-agnostic ChatTurn."""

    calls = []
    for tc in getattr(message, "tool_calls", None) or []:
        calls.append(
            ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}")
        )
    return ChatTurn(content=getattr(message, "content", None), tool_calls=calls)


def _extract_json(content: str) -> dict[str, Any] | None:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") : text.rfind("}") + 1] if "{" in text else text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _coerce_action(value: Any) -> ActionType | None:
    if not value:
        return None
    try:
        return ActionType(str(value))
    except ValueError:
        return None
