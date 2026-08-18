# Runbook: latency spike (no error increase)

**Symptoms:** request p95 latency rises with little or no change in error rate,
cache hit ratio, or pool wait. Often a downstream dependency slowed down or the
service is capacity-constrained under load.

**Most common cause:** load growth or a slow dependency. Check `recent_deploys`
to rule out a change; if none correlates, treat it as capacity/dependency.

**Remediation:**
1. **Scale out** replicas to add capacity.
2. If a specific dependency is the bottleneck, **throttle traffic** to protect it
   while it recovers.
3. Roll back only if a deploy correlates with onset.
