"""Signal-tool tests: summarization logic with injected (offline) fetchers."""

from __future__ import annotations

from incidentpilot.signals import Signals


def _signals(**kw) -> Signals:
    base = {"prometheus_url": "http://p", "target_url": "http://t"}
    base.update(kw)
    return Signals(**base)


def test_query_metrics_summarizes_change() -> None:
    s = _signals(prom_range=lambda _q, _m: [0.35] * 10 + [2.9])
    out = s.query_metrics("p95", minutes=15)
    assert out["first"] == 0.35
    assert out["last"] == 2.9
    assert out["max"] == 2.9
    assert out["change_pct"] > 700  # ~728%


def test_query_metrics_handles_no_data() -> None:
    s = _signals(prom_range=lambda _q, _m: [])
    assert s.query_metrics("p95")["note"] == "no data"


def test_query_logs_counts_levels_and_top_errors() -> None:
    lines = (
        [{"level": "INFO", "msg": "ok"}]
        + [{"level": "ERROR", "msg": "timeout acquiring connection"}] * 4
        + [{"level": "ERROR", "msg": "pool exhausted"}]
    )
    s = _signals(http_get=lambda _p, _params: {"lines": lines})
    out = s.query_logs("payment-service")
    assert out["by_level"]["ERROR"] == 5
    assert out["top_errors"][0]["msg"] == "timeout acquiring connection"
    assert out["top_errors"][0]["count"] == 4


def test_recent_deploys_passes_through() -> None:
    deploys = [{"version": "v412", "bad": True}, {"version": "v411"}]
    s = _signals(http_get=lambda _p, _params: {"deploys": deploys})
    out = s.recent_deploys("payment-service")
    assert out["deploys"][0]["version"] == "v412"


def test_get_traces_localizes_dominant_span() -> None:
    # pool wait is the majority of total latency -> acquire span dominates.
    s = _signals(prom_instant=lambda q: 1.8 if "pool" in q else 2.0)
    out = s.get_traces("payment-service")
    assert out["dominant_span"] == "db.acquire_connection"

    s2 = _signals(prom_instant=lambda q: 0.01 if "pool" in q else 2.0)
    assert s2.get_traces("payment-service")["dominant_span"] == "db.query"


def test_read_runbook_matches_symptom() -> None:
    s = _signals()
    out = s.read_runbook("database connection pool timeout acquiring connection")
    assert out["matched"] == "pool_exhaustion"
    assert "roll back" in out["runbook"].lower()


def test_service_dependencies_returns_topology() -> None:
    s = _signals()
    out = s.service_dependencies("payment-service")
    assert "cache-redis" in out["depends_on"]
    assert "api-gateway" in out["upstream"]
