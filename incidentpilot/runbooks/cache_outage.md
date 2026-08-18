# Runbook: cache / Redis outage

**Symptoms:** `cache_hit_ratio` collapses, logs show `cache unavailable`, latency
rises as every request falls through to the database.

**Most common cause:** the cache node is unreachable (crash, network, eviction
storm). This is usually *not* deploy-related — check `recent_deploys` to rule it
out, but expect no correlation.

**Remediation:**
1. **Restart the cache service** to restore the node.
2. If restart does not recover it, **fail over** to a replica.
3. Do not roll back the application deploy — the cause is the dependency, not the app.
