"""payment-service -- the instrumented target the agent watches and breaks.

A deliberately small but *real* service: every request checks a cache, acquires
a bounded connection pool, and does simulated work, emitting Prometheus metrics
the whole time. A chaos control plane (`/admin/*`) injects genuine faults so the
fault-injection harness produces observable incidents rather than fake numbers.

Faults (each is a real behavior change, not a metric fudge):
  * latency        -- extra per-request work time (dependency slowdown).
  * pool_exhaust   -- shrink the DB connection pool; requests queue on it, so
                      db_pool_wait_seconds and p95 latency both climb.
  * redis_down     -- cache reads fail -> every request falls through to the DB.
  * crash_loop     -- a fraction of requests 5xx (a bad rollout flapping).

A "deploy" endpoint records rollout history (so the agent can correlate an
incident with a recent deploy -- the single highest-signal RCA heuristic) and a
"bad" deploy can arm a fault automatically, modelling a rollout that breaks prod.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel

SERVICE = os.getenv("SERVICE_NAME", "payment-service")
DEFAULT_POOL = int(os.getenv("POOL_SIZE", "10"))

# --------------------------------------------------------------------------- #
# Metrics (names/labels chosen to match the incident examples in the README)
# --------------------------------------------------------------------------- #
REQ_LATENCY = Histogram(
    "http_request_duration_seconds",
    "Request duration in seconds.",
    ["service", "endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0),
)
REQ_TOTAL = Counter(
    "http_requests_total", "Total HTTP requests.", ["service", "endpoint", "status"]
)
POOL_WAIT = Histogram(
    "db_pool_wait_seconds",
    "Time spent waiting to acquire a DB connection.",
    ["service"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)
POOL_IN_USE = Gauge("db_pool_in_use", "Connections currently checked out.", ["service"])
POOL_SIZE_G = Gauge("db_pool_size", "Configured connection-pool size.", ["service"])
CACHE_REQ = Counter("cache_requests_total", "Cache lookups.", ["service", "result"])
DEPLOY_G = Gauge("deploy_info", "Currently running deploy (1=active).", ["service", "version"])


class Chaos:
    """Mutable fault state. Every field maps to a real behavior change above."""

    def __init__(self) -> None:
        self.extra_latency_s: float = 0.0
        self.pool_exhaust: bool = False
        self.redis_down: bool = False
        self.crash_rate: float = 0.0
        self.pool_size: int = DEFAULT_POOL

    def reset(self) -> None:
        self.__init__()

    def snapshot(self) -> dict[str, Any]:
        return {
            "extra_latency_s": self.extra_latency_s,
            "pool_exhaust": self.pool_exhaust,
            "redis_down": self.redis_down,
            "crash_rate": self.crash_rate,
            "pool_size": self.pool_size,
        }


class State:
    """Process-wide runtime state: chaos, pool, deploy log, in-memory cache, logs."""

    def __init__(self) -> None:
        self.chaos = Chaos()
        self._sema = asyncio.Semaphore(DEFAULT_POOL)
        self._in_use = 0
        self.cache: dict[str, float] = {}
        self.deploys: deque[dict[str, Any]] = deque(maxlen=50)
        self.logs: deque[dict[str, Any]] = deque(maxlen=2000)
        self._version = "v411"
        self.log("INFO", f"{SERVICE} started", version=self._version)
        DEPLOY_G.labels(SERVICE, self._version).set(1)
        POOL_SIZE_G.labels(SERVICE).set(DEFAULT_POOL)

    # -- logging ring buffer (backs the agent's query_logs tool) ------------ #
    def log(self, level: str, msg: str, **fields: Any) -> None:
        self.logs.append(
            {"ts": datetime.now(UTC).isoformat(), "level": level, "msg": msg, **fields}
        )

    # -- bounded pool: acquire cost is what pool_exhaust inflates ----------- #
    @asynccontextmanager
    async def acquire(self):
        # Effective capacity collapses to 1 under pool_exhaust, so concurrent
        # load queues up and the wait time (recorded below) spikes.
        effective = 1 if self.chaos.pool_exhaust else self.chaos.pool_size
        start = time.perf_counter()
        acquired = False
        try:
            # Model a small pool: fail fast if we've been waiting too long.
            timeout = 2.0
            while True:
                if self._in_use < effective:
                    self._in_use += 1
                    acquired = True
                    break
                if time.perf_counter() - start > timeout:
                    raise TimeoutError("timeout acquiring connection")
                await asyncio.sleep(0.005)
        finally:
            waited = time.perf_counter() - start
            POOL_WAIT.labels(SERVICE).observe(waited)
        POOL_IN_USE.labels(SERVICE).set(self._in_use)
        try:
            yield waited
        finally:
            if acquired:
                self._in_use -= 1
                POOL_IN_USE.labels(SERVICE).set(self._in_use)

    def cache_get(self, key: str) -> bool:
        if self.chaos.redis_down:
            self.log("ERROR", "cache unavailable: connection refused (redis)")
            CACHE_REQ.labels(SERVICE, "error").inc()
            return False
        hit = key in self.cache
        CACHE_REQ.labels(SERVICE, "hit" if hit else "miss").inc()
        return hit

    def cache_set(self, key: str) -> None:
        if not self.chaos.redis_down:
            self.cache[key] = time.time()


STATE = State()


@asynccontextmanager
async def lifespan(_: FastAPI):
    STATE.log("INFO", "lifespan start")
    yield


app = FastAPI(title=f"{SERVICE} (IncidentPilot target)", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Business endpoint: the thing that gets slow / errors under fault
# --------------------------------------------------------------------------- #
async def _handle(endpoint: str, key: str) -> tuple[int, dict[str, Any]]:
    import random

    start = time.perf_counter()
    status = 200
    body: dict[str, Any] = {}
    try:
        if STATE.chaos.crash_rate and random.random() < STATE.chaos.crash_rate:
            STATE.log("ERROR", "worker crashed handling request", endpoint=endpoint)
            status = 503
            body = {"error": "worker unavailable"}
        else:
            hit = STATE.cache_get(key)
            # A checkout always takes a DB connection (it writes an order); the
            # cache only saves the heavier read query. So pool exhaustion slows
            # *every* request, not just cache misses.
            try:
                async with STATE.acquire():
                    read = 0.002 if hit else (0.01 + STATE.chaos.extra_latency_s)
                    # While the pool is thrashing, held connections are slow.
                    penalty = 0.03 if STATE.chaos.pool_exhaust else 0.0
                    await asyncio.sleep(read + penalty)
                if not hit:
                    STATE.cache_set(key)
                body = {"ok": True, "cache": "hit" if hit else "miss"}
            except TimeoutError:
                STATE.log("ERROR", "timeout acquiring connection", endpoint=endpoint)
                status = 503
                body = {"error": "db pool timeout"}
    finally:
        dur = time.perf_counter() - start
        REQ_LATENCY.labels(SERVICE, endpoint).observe(dur)
        REQ_TOTAL.labels(SERVICE, endpoint, str(status)).inc()
    return status, body


@app.get("/checkout")
async def checkout(user: str = "u1") -> Response:
    status, body = await _handle("/checkout", f"cart:{user}")
    import json

    return Response(content=json.dumps(body), status_code=status, media_type="application/json")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE}


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --------------------------------------------------------------------------- #
# Chaos control plane + deploy registry + log tail (the agent's read tools)
# --------------------------------------------------------------------------- #
class DeployBody(BaseModel):
    version: str
    bad: bool = False  # a "bad" deploy arms pool exhaustion, modelling a broken rollout
    by: str = "ci-bot"
    note: str = ""


class ChaosBody(BaseModel):
    fault: str  # latency | pool_exhaust | redis_down | crash_loop
    magnitude: float = 1.0  # seconds (latency) or rate 0..1 (crash_loop)


@app.post("/admin/deploy")
async def deploy(body: DeployBody) -> dict[str, Any]:
    DEPLOY_G.labels(SERVICE, STATE._version).set(0)
    STATE._version = body.version
    DEPLOY_G.labels(SERVICE, body.version).set(1)
    rec = {
        "version": body.version,
        "at": datetime.now(UTC).isoformat(),
        "by": body.by,
        "note": body.note or ("bump connection-pool defaults" if body.bad else "routine deploy"),
        "bad": body.bad,
    }
    STATE.deploys.appendleft(rec)
    STATE.log("INFO", f"deploy {body.version} applied", version=body.version)
    if body.bad:
        STATE.chaos.pool_exhaust = True
        STATE.log("WARN", "post-deploy: connection pool saturating", version=body.version)
    return {"deployed": rec, "chaos": STATE.chaos.snapshot()}


@app.get("/admin/deploys")
async def deploys(service: str = SERVICE) -> dict[str, Any]:
    return {"service": service, "deploys": list(STATE.deploys)}


@app.get("/admin/logs")
async def logs(contains: str | None = None, limit: int = 200) -> dict[str, Any]:
    lines = list(STATE.logs)
    if contains:
        lines = [line for line in lines if contains.lower() in line["msg"].lower()]
    return {"service": SERVICE, "lines": lines[-limit:]}


@app.post("/admin/chaos")
async def chaos(body: ChaosBody) -> dict[str, Any]:
    f = body.fault
    if f == "latency":
        STATE.chaos.extra_latency_s = max(0.0, body.magnitude)
    elif f == "pool_exhaust":
        STATE.chaos.pool_exhaust = True
    elif f == "redis_down":
        STATE.chaos.redis_down = True
    elif f == "crash_loop":
        STATE.chaos.crash_rate = min(1.0, max(0.0, body.magnitude))
    else:
        return {"error": f"unknown fault: {f}"}
    STATE.log("WARN", f"chaos injected: {f}", magnitude=body.magnitude)
    return {"injected": f, "chaos": STATE.chaos.snapshot()}


@app.post("/admin/reset")
async def reset() -> dict[str, Any]:
    STATE.chaos.reset()
    STATE.cache.clear()
    STATE.log("INFO", "chaos reset; recovered")
    return {"chaos": STATE.chaos.snapshot()}


@app.get("/admin/state")
async def admin_state() -> dict[str, Any]:
    return {"service": SERVICE, "version": STATE._version, "chaos": STATE.chaos.snapshot()}
