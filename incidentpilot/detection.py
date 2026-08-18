"""Drift-adaptive anomaly detection -- FULLY IMPLEMENTED.

Idea borrowed from the owner's StockStream project: keep a rolling window per
metric and score new samples with a *robust* z-score (median + MAD) instead of
mean/stddev. MAD tolerates outliers, and the fixed-length rolling window lets
the baseline follow slow drift (diurnal load, gradual traffic growth) so we
alert on genuine breaks, not on the trend itself.

Runnable with only numpy + incidentpilot.models.
"""

from __future__ import annotations

import uuid
from collections import deque

import numpy as np

from incidentpilot.models import Incident, Severity, Signal

# 1.4826 makes MAD a consistent estimator of the standard deviation for
# normally distributed data, so the resulting score is comparable to a z-score.
_MAD_TO_STD = 1.4826


class DriftAdaptiveDetector:
    """Per-metric robust anomaly detector.

    Parameters
    ----------
    window:
        Number of recent samples kept per metric. Older samples fall off, which
        is what makes the baseline *drift-adaptive*.
    z_threshold:
        Absolute robust z-score above which a sample is flagged.
    min_samples:
        Warm-up count before we score anything (avoids alerting on cold start).
    """

    def __init__(
        self,
        window: int = 50,
        z_threshold: float = 3.5,
        min_samples: int = 10,
    ) -> None:
        self.window = window
        self.z_threshold = z_threshold
        self.min_samples = min_samples
        self._buffers: dict[str, deque[float]] = {}

    def _severity(self, z: float) -> Severity:
        az = abs(z)
        if az >= 2 * self.z_threshold:
            return Severity.CRITICAL
        if az >= 1.5 * self.z_threshold:
            return Severity.WARNING
        return Severity.INFO

    def observe(
        self,
        metric: str,
        value: float,
        service: str = "unknown",
    ) -> Incident | None:
        """Feed one sample; return an Incident iff it breaks from baseline.

        The sample is always folded into the window afterwards so the baseline
        keeps tracking drift even across an incident.
        """

        buf = self._buffers.setdefault(metric, deque(maxlen=self.window))

        incident: Incident | None = None
        if len(buf) >= self.min_samples:
            arr = np.fromiter(buf, dtype=float)
            median = float(np.median(arr))
            mad = float(np.median(np.abs(arr - median)))
            robust_std = _MAD_TO_STD * mad

            if robust_std == 0.0:
                # Degenerate flat window: fall back to a simple non-zero delta.
                z = 0.0 if value == median else float("inf")
            else:
                z = (value - median) / robust_std

            if abs(z) >= self.z_threshold:
                severity = self._severity(z)
                incident = Incident(
                    id=f"inc-{uuid.uuid4().hex[:8]}",
                    service=service,
                    metric=metric,
                    value=value,
                    baseline=median,
                    z_score=(z if np.isfinite(z) else self.z_threshold * 2),
                    severity=severity,
                    description=(
                        f"{metric} on {service} = {value:.3f} vs baseline "
                        f"{median:.3f} (robust z={z:.2f})"
                    ),
                    signals=[Signal(name=metric, value=value, labels={"service": service})],
                )

        buf.append(value)
        return incident
