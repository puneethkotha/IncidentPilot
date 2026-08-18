"""Manual end-to-end smoke: prove an injected fault becomes a detected incident.

Requires the demo stack to be running (`make stack`). It baselines the live
Prometheus signals, injects a bad deploy (which exhausts the connection pool),
and watches IncidentPilot's detector open an incident from the real metrics.

    make stack
    python scripts/smoke_incident.py
"""

from __future__ import annotations

import time

import httpx

from incidentpilot.detection import DriftAdaptiveDetector
from incidentpilot.monitor import PrometheusClient, PrometheusMonitor, default_specs

PROMETHEUS = "http://localhost:9090"
SERVICE = "http://localhost:8080"


def main() -> None:
    client = PrometheusClient(PROMETHEUS)
    monitor = PrometheusMonitor(
        client.query,
        specs=default_specs(),
        detector=DriftAdaptiveDetector(min_samples=8, z_threshold=3.5),
    )
    p95_query = default_specs()[0].promql

    print("warming a baseline from live Prometheus ...")
    for _ in range(12):
        monitor.poll_once()
        time.sleep(1)
    print(f"  baseline p95 ≈ {client.query(p95_query):.4f}s")

    print("\n>>> injecting a bad deploy (v412) that exhausts the connection pool")
    httpx.post(f"{SERVICE}/admin/deploy", json={"version": "v412", "bad": True}, timeout=5)

    print("watching for an incident from real signals ...")
    opened = None
    for _ in range(40):
        found = monitor.poll_once()
        if found:
            opened = found[0]
            break
        time.sleep(1)

    print(f"  spiked p95 ≈ {client.query(p95_query):.4f}s")
    if opened is None:
        print("\nNo incident opened — try increasing load or the fault magnitude.")
        raise SystemExit(1)

    print("\n=== INCIDENT (detected from live Prometheus) ===")
    print(opened.model_dump_json(indent=2))
    print("\nreset the target with:  curl -XPOST http://localhost:8080/admin/reset")


if __name__ == "__main__":
    main()
