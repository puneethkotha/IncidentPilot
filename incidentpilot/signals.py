"""The diagnosis agent's typed tools -- backed by real signals.

Each tool reads the live system under test (Prometheus + the target service's
log/deploy endpoints) and returns a *summarized* result, not a raw dump: the
agent gets the smallest set of high-signal tokens (change vs baseline, top
repeated error, the correlating deploy), which is both cheaper and more accurate
than pasting whole series or log files into the context.

Everything is injectable (`prom_range`, `prom_instant`, `http_get`) so tests run
without a network, and a default instance wires those to httpx.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from incidentpilot.config import Settings, get_settings

RUNBOOK_DIR = Path(__file__).parent / "runbooks"

# Static service dependency graph -- lets the agent reason about topology
# (an incident in payment-service may originate in a thing it depends on).
DEPENDENCY_GRAPH: dict[str, dict[str, list[str]]] = {
    "payment-service": {"depends_on": ["cache-redis", "ledger-db"], "upstream": ["api-gateway"]},
    "checkout": {"depends_on": ["payment-service"], "upstream": ["api-gateway"]},
    "api-gateway": {"depends_on": ["checkout", "payment-service"], "upstream": []},
    "cache-redis": {"depends_on": [], "upstream": ["payment-service"]},
    "ledger-db": {"depends_on": [], "upstream": ["payment-service"]},
}


def _p95_promql(service: str) -> str:
    return (
        "histogram_quantile(0.95, sum(rate("
        f'http_request_duration_seconds_bucket{{service="{service}"}}[1m])) by (le))'
    )


def _pool_wait_promql(service: str) -> str:
    return (
        "histogram_quantile(0.95, sum(rate("
        f'db_pool_wait_seconds_bucket{{service="{service}"}}[1m])) by (le))'
    )


class Signals:
    """The tool surface. Read-only against the live system."""

    def __init__(
        self,
        *,
        prometheus_url: str,
        target_url: str,
        timeout: float = 5.0,
        prom_range: Callable[[str, int], list[float]] | None = None,
        prom_instant: Callable[[str], float | None] | None = None,
        http_get: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.prometheus_url = prometheus_url.rstrip("/")
        self.target_url = target_url.rstrip("/")
        self.timeout = timeout
        self.prom_range = prom_range or self._default_prom_range
        self.prom_instant = prom_instant or self._default_prom_instant
        self.http_get = http_get or self._default_http_get

    # -- default network impls (lazy httpx) -------------------------------- #
    def _default_prom_range(self, promql: str, minutes: int) -> list[float]:
        import time as _t

        import httpx

        end = _t.time()
        start = end - minutes * 60
        try:
            r = httpx.get(
                f"{self.prometheus_url}/api/v1/query_range",
                params={"query": promql, "start": start, "end": end, "step": "5s"},
                timeout=self.timeout,
            )
            r.raise_for_status()
            series = r.json().get("data", {}).get("result", [])
            if not series:
                return []
            return [float(v) for _, v in series[0]["values"] if v == v]  # drop NaN
        except Exception:  # noqa: BLE001 - a scrape failure returns "no data", not a crash
            return []

    def _default_prom_instant(self, promql: str) -> float | None:
        import httpx

        try:
            r = httpx.get(
                f"{self.prometheus_url}/api/v1/query",
                params={"query": promql},
                timeout=self.timeout,
            )
            r.raise_for_status()
            result = r.json().get("data", {}).get("result", [])
            if not result:
                return None
            value = float(result[0]["value"][1])
            return None if value != value else value
        except Exception:  # noqa: BLE001
            return None

    def _default_http_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        import httpx

        try:
            r = httpx.get(f"{self.target_url}{path}", params=params, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    # -- tools ------------------------------------------------------------- #
    def query_metrics(self, promql: str, minutes: int = 15) -> dict[str, Any]:
        """Run a PromQL range query; return a compact change summary."""

        vals = self.prom_range(promql, minutes)
        if not vals:
            return {"promql": promql, "minutes": minutes, "note": "no data"}
        first, last = vals[0], vals[-1]
        lo, hi = min(vals), max(vals)
        change_pct = ((last - first) / first * 100.0) if first else float("inf")
        return {
            "promql": promql,
            "minutes": minutes,
            "points": len(vals),
            "first": round(first, 4),
            "last": round(last, 4),
            "min": round(lo, 4),
            "max": round(hi, 4),
            "change_pct": round(change_pct, 1),
        }

    def query_logs(
        self, service: str, minutes: int = 15, contains: str | None = None
    ) -> dict[str, Any]:
        """Fetch recent logs; return level counts + the top repeated message."""

        data = self.http_get("/admin/logs", {"contains": contains, "limit": 500})
        lines = data.get("lines", [])
        by_level = Counter(line.get("level", "?") for line in lines)
        msgs = Counter(line.get("msg", "") for line in lines if line.get("level") == "ERROR")
        top = [{"msg": m, "count": c} for m, c in msgs.most_common(3)]
        return {
            "service": service,
            "matched_lines": len(lines),
            "by_level": dict(by_level),
            "top_errors": top,
            "recent": lines[-5:],
        }

    def recent_deploys(self, service: str) -> dict[str, Any]:
        """List recent deploys (a top RCA signal: correlate onset with a rollout)."""

        data = self.http_get("/admin/deploys", {"service": service})
        return {"service": service, "deploys": data.get("deploys", [])[:5]}

    def get_traces(self, service: str) -> dict[str, Any]:
        """Localize the slow span from real latency breakdown (total vs pool-wait)."""

        p95 = self.prom_instant(_p95_promql(service))
        pool = self.prom_instant(_pool_wait_promql(service))
        dominant = "unknown"
        if p95 is not None and pool is not None:
            dominant = "db.acquire_connection" if pool >= 0.5 * p95 else "db.query"
        return {
            "service": service,
            "p95_total_s": None if p95 is None else round(p95, 4),
            "db_acquire_p95_s": None if pool is None else round(pool, 4),
            "dominant_span": dominant,
        }

    def read_runbook(self, symptom: str) -> dict[str, Any]:
        """Return the runbook whose keywords best match the symptom."""

        if not RUNBOOK_DIR.exists():
            return {"symptom": symptom, "runbook": None, "note": "no runbook store"}
        words = {w for w in symptom.lower().replace("/", " ").split() if len(w) > 3}
        best, best_score, best_name = None, 0, None
        for path in sorted(RUNBOOK_DIR.glob("*.md")):
            text = path.read_text()
            score = sum(text.lower().count(w) for w in words)
            if score > best_score:
                best, best_score, best_name = text, score, path.stem
        if best is None:
            return {"symptom": symptom, "runbook": None, "note": "no match"}
        return {"symptom": symptom, "matched": best_name, "runbook": best.strip()}

    def service_dependencies(self, service: str) -> dict[str, Any]:
        """Return the service's dependencies and upstreams (topology for RCA)."""

        edges = DEPENDENCY_GRAPH.get(service, {"depends_on": [], "upstream": []})
        return {"service": service, **edges}

    # -- wiring for the agent --------------------------------------------- #
    def tools(self) -> dict[str, Callable[..., dict[str, Any]]]:
        return {
            "query_metrics": self.query_metrics,
            "query_logs": self.query_logs,
            "recent_deploys": self.recent_deploys,
            "get_traces": self.get_traces,
            "read_runbook": self.read_runbook,
            "service_dependencies": self.service_dependencies,
        }


def build_signals(settings: Settings | None = None) -> Signals:
    s = settings or get_settings()
    return Signals(prometheus_url=s.prometheus_url, target_url=s.target_url)


# OpenAI-compatible tool schemas advertised to the LLM.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_metrics",
            "description": "Run a PromQL range query; returns a compact change summary",
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
            "description": "Recent logs for a service: level counts and top repeated errors",
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
            "name": "recent_deploys",
            "description": "Recent deploys for a service (correlate incident onset with a rollout)",
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
            "name": "get_traces",
            "description": "Latency breakdown for a service; localizes the dominant slow span",
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
            "description": "Return the operator runbook whose keywords best match a symptom",
            "parameters": {
                "type": "object",
                "properties": {"symptom": {"type": "string"}},
                "required": ["symptom"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "service_dependencies",
            "description": "Service dependencies and upstreams (topology for RCA)",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string"}},
                "required": ["service"],
            },
        },
    },
]
