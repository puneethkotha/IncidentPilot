"""Closed-loop load against the target service.

Runs a fixed number of concurrent virtual users, each looping request→request.
The concurrency matters: it is what lets a shrunken connection pool actually
*contend*, so pool exhaustion produces a real db_pool_wait / p95 spike rather
than just slower isolated requests. Not a benchmark -- a steady heartbeat.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random

import httpx

TARGET = os.getenv("TARGET_URL", "http://payment-service:8080")
CONCURRENCY = int(os.getenv("CONCURRENCY", "15"))
USERS = int(os.getenv("USERS", "40"))
THINK_S = float(os.getenv("THINK_S", "0.02"))


async def worker(client: httpx.AsyncClient) -> None:
    while True:
        user = f"u{random.randint(1, USERS)}"
        with contextlib.suppress(Exception):
            await client.get("/checkout", params={"user": user})
        await asyncio.sleep(THINK_S)


async def main() -> None:
    print(f"loadgen -> {TARGET} · {CONCURRENCY} concurrent users", flush=True)
    async with httpx.AsyncClient(base_url=TARGET, timeout=5.0) as client:
        await asyncio.gather(*[worker(client) for _ in range(CONCURRENCY)])


if __name__ == "__main__":
    asyncio.run(main())
