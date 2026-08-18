"""Detection loop -- polls Prometheus and turns metric breaks into Incidents.

This wires the `DriftAdaptiveDetector` to real signals: on each tick it runs a
small set of PromQL queries (p95 latency, pool-wait, error rate, cache-hit
ratio), feeds each result to the per-metric detector, and yields an `Incident`
whenever a sample breaks from its rolling baseline.

The Prometheus client is a thin httpx wrapper behind a tiny interface, so tests
inject a scripted query function and never touch the network.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from incidentpilot.config import get_settings
from incidentpilot.detection import DriftAdaptiveDetector
from incidentpilot.models import Incident

# A query function maps PromQL -> a scalar sample (or None if no data yet).
QueryFn = Callable[[str], float | None]


@dataclass(frozen=True)
class MetricSpec:
    """One watched signal: how to fetch it and which service it belongs to."""

    key: str  # stable id used as the detector's per-metric buffer key
    service: str
    metric: str  # human-facing metric name (shown on the incident)
    promql: str
    # Some signals are anomalies when they DROP (cache hit ratio); flip the sign
    # so the detector's magnitude logic still points "worse = larger".
    invert: bool = False


def default_specs(service: str = "payment-service") -> list[MetricSpec]:
    s = service
    return [
        MetricSpec(
            key=f"{s}:p95_latency",
            service=s,
            metric="http_request_duration_seconds:p95",
            promql=(
                "histogram_quantile(0.95, sum(rate("
                f'http_request_duration_seconds_bucket{{service="{s}"}}[1m])) by (le))'
            ),
        ),
        MetricSpec(
            key=f"{s}:pool_wait_p95",
            service=s,
            metric="db_pool_wait_seconds:p95",
            promql=(
                "histogram_quantile(0.95, sum(rate("
                f'db_pool_wait_seconds_bucket{{service="{s}"}}[1m])) by (le))'
            ),
        ),
        MetricSpec(
            key=f"{s}:error_rate",
            service=s,
            metric="http_5xx_rate",
            promql=(
                f'sum(rate(http_requests_total{{service="{s}",status=~"5.."}}[1m]))'
                f' / clamp_min(sum(rate(http_requests_total{{service="{s}"}}[1m])), 0.001)'
            ),
        ),
        MetricSpec(
            # cache_hit_ratio COLLAPSES when the cache is down -> invert so the
            # detector still reads "worse = larger"; the incident reports the
            # true (positive) ratio.
            key=f"{s}:cache_hit_ratio",
            service=s,
            metric="cache_hit_ratio",
            promql=(
                f'sum(rate(cache_requests_total{{service="{s}",result="hit"}}[1m]))'
                f' / clamp_min(sum(rate(cache_requests_total{{service="{s}"}}[1m])), 0.001)'
            ),
            invert=True,
        ),
    ]


def promql_for(service: str, metric: str) -> str | None:
    """Recover the PromQL for a watched metric name (used by verification)."""

    for spec in default_specs(service):
        if spec.metric == metric:
            return spec.promql
    return None


# Metrics where recovery means the value goes back UP (not down).
HIGHER_IS_BETTER = {"cache_hit_ratio"}


def is_recovered(metric: str, last: float | None, baseline: float, recovery_factor: float) -> bool:
    """Direction-aware recovery check for post-action verification."""

    if last is None:
        return False
    if metric in HIGHER_IS_BETTER:
        return last >= baseline * 0.8
    return last <= max(baseline * recovery_factor, 0.05)


class PrometheusClient:
    """Minimal Prometheus instant-query client (httpx, lazy import)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def query(self, promql: str) -> float | None:
        import httpx  # lazy so importing this module never needs the dep

        try:
            resp = httpx.get(
                f"{self.base_url}/api/v1/query", params={"query": promql}, timeout=5.0
            )
            resp.raise_for_status()
            result = resp.json().get("data", {}).get("result", [])
            if not result:
                return None
            value = float(result[0]["value"][1])
            # Prometheus returns NaN as the string "NaN".
            return None if value != value else value
        except Exception:  # noqa: BLE001 - a scrape hiccup should not crash the loop
            return None


class PrometheusMonitor:
    """Drives a detector across a set of watched Prometheus signals."""

    def __init__(
        self,
        query_fn: QueryFn,
        specs: list[MetricSpec] | None = None,
        detector: DriftAdaptiveDetector | None = None,
    ) -> None:
        self.query_fn = query_fn
        self.specs = specs or default_specs()
        self.detector = detector or DriftAdaptiveDetector()

    def poll_once(self) -> list[Incident]:
        """Fetch every watched signal once; return any incidents that opened."""

        incidents: list[Incident] = []
        for spec in self.specs:
            value = self.query_fn(spec.promql)
            if value is None:
                continue
            observed = -value if spec.invert else value
            incident = self.detector.observe(spec.key, observed, service=spec.service)
            if incident is not None:
                # Restore human-facing fields (buffer key -> real metric/value).
                incident.metric = spec.metric
                if spec.invert:
                    incident.value = -incident.value
                    incident.baseline = -incident.baseline
                incidents.append(incident)
        return incidents

    def run(
        self,
        interval_seconds: float = 5.0,
        on_incident: Callable[[Incident], None] | None = None,
    ) -> None:
        """Poll forever (used by the CLI / live demo)."""

        while True:
            for incident in self.poll_once():
                if on_incident:
                    on_incident(incident)
                else:
                    print(incident.model_dump_json())
            time.sleep(interval_seconds)


def main() -> None:
    settings = get_settings()
    client = PrometheusClient(settings.prometheus_url)
    monitor = PrometheusMonitor(client.query)
    print(
        f"IncidentPilot monitor -> {settings.prometheus_url} "
        f"(watching {len(monitor.specs)} signals)"
    )
    monitor.run()


if __name__ == "__main__":
    main()
