"""Demo system-under-test for IncidentPilot.

A small, self-contained distributed system the agent can watch and break:
an instrumented target service (`demo.target_service`) plus a load generator
(`demo.loadgen`). It emits real Prometheus metrics and exposes a chaos control
plane so the fault-injection harness can cause *real*, observable incidents
(latency spikes, pool exhaustion after a bad deploy, cache outage, crash loop).

Nothing here imports `incidentpilot` -- it is the system under test, kept
deliberately separate from the agent that operates it.
"""
