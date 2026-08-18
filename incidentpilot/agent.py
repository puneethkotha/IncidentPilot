"""DiagnosisAgent -- ReAct-style tool-calling loop over an OpenAI-compatible LLM.

The loop structure is implemented: build messages, call the model, dispatch any
tool calls to `incidentpilot.signals`, feed results back, repeat until the model emits
a final JSON diagnosis. The actual model call is isolated behind `_chat()` so it
is trivial to swap Groq <-> Claude <-> a mock in tests.

Importing this module performs NO network I/O (the OpenAI client is created
lazily inside `_chat`).
"""

from __future__ import annotations

import json
from typing import Any

from incidentpilot.config import Settings, get_settings
from incidentpilot.models import Evidence, Incident, RootCauseHypothesis
from incidentpilot.signals import TOOL_FUNCTIONS, TOOL_SCHEMAS

SYSTEM_PROMPT = """You are IncidentPilot, a precise Site Reliability Engineer.

You are given an incident. Investigate it by calling the provided tools. Rules:
- Be evidence-driven: prefer recent_deploys and metrics before guessing.
- Do not speculate beyond what the tool outputs support.
- Call tools one or a few at a time; stop as soon as you can justify a root cause.
- When confident, return a FINAL answer as strict JSON with keys:
  {"cause": str, "confidence": 0..1, "evidence": [{"tool": str, "summary": str}],
   "recommended_action": one of
     [no_op, restart_service, rollback_deploy, scale_out, scale_in,
      clear_cache, failover, throttle_traffic]}
Return ONLY that JSON object for your final message, with no prose around it.
"""


class DiagnosisAgent:
    def __init__(self, settings: Settings | None = None, max_iters: int = 6) -> None:
        self.settings = settings or get_settings()
        self.max_iters = max_iters

    # --- model call boundary -------------------------------------------- #
    def _chat(self, messages: list[dict[str, Any]]) -> Any:
        """Single chat-completion turn with tools enabled.

        Returns the raw `message` object (OpenAI-compatible), which may contain
        `tool_calls` and/or `content`.
        """

        # TODO: wire real client + error handling / retries / token budget.
        from openai import OpenAI  # lazy import

        client = OpenAI(
            api_key=self.settings.groq_api_key,
            base_url=self.settings.groq_base_url,
        )
        resp = client.chat.completions.create(
            model=self.settings.llm_model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=0.0,
        )
        return resp.choices[0].message

    # --- tool dispatch -------------------------------------------------- #
    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        fn = TOOL_FUNCTIONS.get(name)
        if fn is None:
            return {"error": f"unknown tool: {name}"}
        try:
            return fn(**args)
        except Exception as exc:  # noqa: BLE001 - surface any tool error to the model, keep the loop alive
            return {"error": f"{type(exc).__name__}: {exc}"}

    # --- main loop ------------------------------------------------------ #
    def diagnose(self, incident: Incident) -> RootCauseHypothesis:
        """Run the ReAct loop and return a structured root-cause hypothesis."""

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": incident.model_dump_json()},
        ]

        for _ in range(self.max_iters):
            msg = self._chat(messages)
            tool_calls = getattr(msg, "tool_calls", None)

            if not tool_calls:
                # Final answer expected: parse the JSON diagnosis.
                return self._parse_final(getattr(msg, "content", "") or "")

            # Echo the assistant turn, then answer each tool call.
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [tc.model_dump() for tc in tool_calls],
                }
            )
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self._dispatch(name, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result),
                    }
                )

        # Ran out of iterations without a final answer.
        return RootCauseHypothesis(
            cause="inconclusive: iteration budget exhausted",
            confidence=0.0,
            evidence=[],
        )

    def _parse_final(self, content: str) -> RootCauseHypothesis:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return RootCauseHypothesis(
                cause=content.strip()[:200] or "unparseable model output",
                confidence=0.2,
            )
        evidence = [
            Evidence(tool=e.get("tool", "?"), summary=e.get("summary", ""))
            for e in data.get("evidence", [])
        ]
        return RootCauseHypothesis(
            cause=data.get("cause", "unspecified"),
            confidence=float(data.get("confidence", 0.5)),
            evidence=evidence,
            recommended_action=data.get("recommended_action"),
        )
