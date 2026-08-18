"""Monitor tests: the detection loop turns a scripted Prometheus signal into an
Incident, using an injected query function (no network)."""

from __future__ import annotations

from incidentpilot.detection import DriftAdaptiveDetector
from incidentpilot.monitor import MetricSpec, PrometheusMonitor


def test_poll_opens_incident_on_p95_break() -> None:
    spec = MetricSpec(
        key="payment-service:p95_latency",
        service="payment-service",
        metric="http_request_duration_seconds:p95",
        promql="q",
    )
    # A scripted signal: flat baseline, then a break.
    samples = [0.35] * 20 + [2.9]
    it = iter(samples)
    monitor = PrometheusMonitor(
        query_fn=lambda _q: next(it),
        specs=[spec],
        detector=DriftAdaptiveDetector(min_samples=10, z_threshold=3.5),
    )

    incidents = []
    for _ in range(len(samples)):
        incidents.extend(monitor.poll_once())

    assert len(incidents) == 1
    inc = incidents[0]
    assert inc.service == "payment-service"
    assert inc.metric == "http_request_duration_seconds:p95"
    assert inc.value == 2.9


def test_missing_data_is_skipped() -> None:
    spec = MetricSpec(key="k", service="s", metric="m", promql="q")
    monitor = PrometheusMonitor(
        query_fn=lambda _q: None, specs=[spec], detector=DriftAdaptiveDetector(min_samples=3)
    )
    # None samples must never crash and never open an incident.
    assert monitor.poll_once() == []
    assert monitor.poll_once() == []


def test_inverted_signal_restores_sign() -> None:
    # cache_hit_ratio collapse: invert so the detector sees "worse = larger",
    # but the reported incident value should be the real (positive) ratio.
    spec = MetricSpec(key="s:cache", service="s", metric="cache_hit_ratio", promql="q", invert=True)
    samples = [0.95] * 20 + [0.10]
    it = iter(samples)
    monitor = PrometheusMonitor(
        query_fn=lambda _q: next(it),
        specs=[spec],
        detector=DriftAdaptiveDetector(min_samples=10, z_threshold=3.5),
    )
    incidents = []
    for _ in range(len(samples)):
        incidents.extend(monitor.poll_once())
    assert len(incidents) == 1
    assert abs(incidents[0].value - 0.10) < 1e-9
