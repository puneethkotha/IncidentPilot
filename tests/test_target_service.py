"""Target-service tests: the chaos control plane causes *real* behavior changes
(cache outage, crash loop, pool contention) and the deploy/log tools work.

These exercise the actual FastAPI app in-process, so faults are proven by the
metrics the service emits -- the same signals the agent will later read."""

from __future__ import annotations

import asyncio
import re

import httpx
from fastapi.testclient import TestClient

from demo.target_service.app import app

client = TestClient(app)


def _metrics() -> str:
    return client.get("/metrics").text


def _sum(text: str, metric: str, *contains: str) -> float:
    total = 0.0
    for line in text.splitlines():
        if not line.startswith(metric):
            continue
        if all(c in line for c in contains):
            total += float(line.rsplit(" ", 1)[1])
    return total


def setup_function() -> None:
    client.post("/admin/reset")


def test_healthz_and_checkout_ok() -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    r = client.get("/checkout", params={"user": "alice"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_cache_hit_after_first_miss() -> None:
    before = _metrics()
    hits0 = _sum(before, "cache_requests_total", 'result="hit"')
    client.get("/checkout", params={"user": "bob"})  # miss -> populate
    client.get("/checkout", params={"user": "bob"})  # hit
    hits1 = _sum(_metrics(), "cache_requests_total", 'result="hit"')
    assert hits1 > hits0


def test_redis_down_forces_cache_errors() -> None:
    client.post("/admin/chaos", json={"fault": "redis_down"})
    before = _sum(_metrics(), "cache_requests_total", 'result="error"')
    client.get("/checkout", params={"user": "carol"})
    client.get("/checkout", params={"user": "carol"})  # would be a hit normally
    after = _sum(_metrics(), "cache_requests_total", 'result="error"')
    assert after >= before + 2
    logs = client.get("/admin/logs", params={"contains": "cache unavailable"}).json()
    assert len(logs["lines"]) >= 1


def test_crash_loop_returns_5xx() -> None:
    client.post("/admin/chaos", json={"fault": "crash_loop", "magnitude": 1.0})
    before = _sum(_metrics(), "http_requests_total", 'status="503"')
    r = client.get("/checkout", params={"user": "dave"})
    assert r.status_code == 503
    after = _sum(_metrics(), "http_requests_total", 'status="503"')
    assert after >= before + 1


def test_bad_deploy_records_history_and_arms_pool_exhaust() -> None:
    r = client.post("/admin/deploy", json={"version": "v412", "bad": True})
    assert r.json()["chaos"]["pool_exhaust"] is True
    deploys = client.get("/admin/deploys").json()["deploys"]
    assert deploys[0]["version"] == "v412"
    assert deploys[0]["bad"] is True


def test_pool_exhaustion_makes_requests_wait_under_load() -> None:
    """Under pool_exhaust the effective pool collapses to 1, so concurrent
    requests queue on it and db_pool_wait_seconds observations climb."""

    client.post("/admin/chaos", json={"fault": "pool_exhaust"})
    before_count = _sum(_metrics(), "db_pool_wait_seconds_count")
    before_sum = _sum(_metrics(), "db_pool_wait_seconds_sum")

    async def burst() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            await asyncio.gather(
                *[ac.get("/checkout", params={"user": f"load{i}"}) for i in range(12)]
            )

    asyncio.run(burst())
    text = _metrics()
    after_count = _sum(text, "db_pool_wait_seconds_count")
    after_sum = _sum(text, "db_pool_wait_seconds_sum")
    assert after_count >= before_count + 12
    # Contention means the total time spent waiting rose meaningfully.
    assert after_sum - before_sum > 0.05


def test_metrics_endpoint_exposes_expected_series() -> None:
    text = _metrics()
    for name in (
        "http_request_duration_seconds_bucket",
        "http_requests_total",
        "db_pool_wait_seconds_bucket",
        "cache_requests_total",
    ):
        assert re.search(rf"^{re.escape(name)}", text, re.M), name
