"""Diagnosis tools + OpenAI tool schemas.

These are the *typed tools* the DiagnosisAgent is allowed to call. `query_metrics`
carries a real prometheus-api-client call sketch (reuses the owner's existing
Prometheus/Grafana stack). The rest are typed stubs returning placeholder data
so the agent loop is exercisable end-to-end without live infra.

`TOOL_SCHEMAS` is the OpenAI-compatible function/tool spec list handed to the LLM.
Importing this module performs NO network I/O (Prometheus client is imported and
connected lazily, inside the call).
"""

from __future__ import annotations

from typing import Any


def query_metrics(promql: str, minutes: int = 15) -> dict[str, Any]:
    """Run a PromQL range query against Prometheus.

    Real call sketch using prometheus-api-client. Import + connect are lazy so
    that merely importing this module never hits the network.
    """

    from datetime import datetime, timedelta, timezone

    from prometheus_api_client import PrometheusConnect  # lazy import

    from incidentpilot.config import get_settings

    settings = get_settings()
    prom = PrometheusConnect(url=settings.prometheus_url, disable_ssl=True)
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    series = prom.custom_query_range(
        query=promql,
        start_time=start,
        end_time=end,
        step="30s",
    )
    return {"promql": promql, "minutes": minutes, "series": series}


def query_logs(service: str, minutes: int = 15, contains: str | None = None) -> dict[str, Any]:
    """Fetch recent log lines for a service, optionally filtered."""

    # TODO: wire to Loki / CloudWatch / the owner's log backend.
    return {
        "service": service,
        "minutes": minutes,
        "contains": contains,
        "lines": [
            {"ts": "2026-08-17T00:00:00Z", "level": "ERROR", "msg": "placeholder log line"},
        ],
    }


def get_traces(service: str) -> dict[str, Any]:
    """Fetch recent slow/error traces for a service."""

    # TODO: wire to Tempo / Jaeger / OTel collector.
    return {
        "service": service,
        "traces": [
            {"trace_id": "0000", "duration_ms": 1234, "status": "error", "span": "db.query"},
        ],
    }


def recent_deploys(service: str) -> dict[str, Any]:
    """List recent deploys/config changes for a service (a top RCA signal)."""

    # TODO: wire to the deploy system / GitHub deployments / Argo.
    return {
        "service": service,
        "deploys": [
            {"version": "v0.0.0", "at": "2026-08-17T00:00:00Z", "by": "ci", "note": "placeholder"},
        ],
    }


def read_runbook(symptom: str) -> dict[str, Any]:
    """Return the operator runbook snippet matching a symptom."""

    # TODO: wire to the runbook store (markdown repo / Notion / wiki).
    return {
        "symptom": symptom,
        "runbook": "placeholder runbook: check deploys, then dependencies, then capacity",
    }


# Dispatch table so the agent can map a tool name -> callable.
TOOL_FUNCTIONS = {
    "query_metrics": query_metrics,
    "query_logs": query_logs,
    "get_traces": get_traces,
    "recent_deploys": recent_deploys,
    "read_runbook": read_runbook,
}


# OpenAI-compatible tool schemas advertised to the LLM.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_metrics",
            "description": "Run a PromQL query over the last N minutes and return time series.",
            "parameters": {
                "type": "object",
                "properties": {
                    "promql": {"type": "string", "description": "PromQL expression"},
                    "minutes": {"type": "integer", "default": 15},
                },
                "required": ["promql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_logs",
            "description": "Fetch recent log lines for a service, optionally filtered by substring.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "minutes": {"type": "integer", "default": 15},
                    "contains": {"type": "string"},
                },
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_traces",
            "description": "Fetch recent slow or error traces for a service.",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string"}},
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_deploys",
            "description": "List recent deploys/config changes for a service.",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string"}},
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_runbook",
            "description": "Return the operator runbook snippet matching a symptom.",
            "parameters": {
                "type": "object",
                "properties": {"symptom": {"type": "string"}},
                "required": ["symptom"],
            },
        },
    },
]
