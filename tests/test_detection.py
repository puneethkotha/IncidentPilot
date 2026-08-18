"""Detector tests: it opens an incident on a genuine break, stays quiet on
noise, and follows slow drift instead of alerting on the trend."""

from __future__ import annotations

from incidentpilot.detection import DriftAdaptiveDetector
from incidentpilot.models import Severity


def _warm(det: DriftAdaptiveDetector, metric: str, value: float, n: int) -> None:
    for _ in range(n):
        assert det.observe(metric, value, service="payment-service") is None


def test_no_alert_during_warmup() -> None:
    det = DriftAdaptiveDetector(min_samples=10)
    # Even a wild value before warm-up must not alert (no baseline yet).
    for _ in range(5):
        assert det.observe("m", 100.0, service="s") is None


def test_spike_opens_incident_with_fields() -> None:
    det = DriftAdaptiveDetector(window=50, z_threshold=3.5, min_samples=10)
    _warm(det, "p95", 0.35, 30)
    incident = det.observe("p95", 2.9, service="payment-service")
    assert incident is not None
    assert incident.service == "payment-service"
    assert incident.metric == "p95"
    assert incident.value == 2.9
    assert abs(incident.baseline - 0.35) < 0.05
    assert incident.z_score > 3.5
    assert incident.severity in (Severity.WARNING, Severity.CRITICAL)


def test_steady_noise_does_not_alert() -> None:
    det = DriftAdaptiveDetector(z_threshold=3.5, min_samples=10)
    seq = [0.34, 0.35, 0.36, 0.35, 0.34, 0.36, 0.35, 0.34, 0.35, 0.36] * 3
    alerts = [det.observe("m", v, service="s") for v in seq]
    assert all(a is None for a in alerts)


def test_slow_drift_is_absorbed() -> None:
    det = DriftAdaptiveDetector(window=20, z_threshold=3.5, min_samples=10)
    # A slow linear ramp should be tracked by the rolling baseline, not alerted.
    fired = False
    for i in range(60):
        inc = det.observe("m", 1.0 + i * 0.02, service="s")
        fired = fired or inc is not None
    assert not fired


def test_big_break_is_critical() -> None:
    det = DriftAdaptiveDetector(z_threshold=3.5, min_samples=10)
    _warm(det, "m", 1.0, 30)
    inc = det.observe("m", 50.0, service="s")
    assert inc is not None
    assert inc.severity == Severity.CRITICAL
