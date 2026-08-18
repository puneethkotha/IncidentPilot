# Runbook: database connection pool exhaustion

**Symptoms:** `db_pool_wait_seconds` climbs, request p95 latency rises, logs show
`timeout acquiring connection`. Error rate may rise as waiters time out.

**Most common cause:** a recent deploy changed pool sizing, connection lifetime,
or query cost, so connections are held longer than they are returned. Check
`recent_deploys` first — onset that lines up within a few minutes of a rollout
is the strongest signal.

**Remediation (lowest-risk first):**
1. If a deploy landed just before onset, **roll back the deploy** — reversible
   and addresses the cause.
2. If no deploy correlates, **scale out** replicas to add pool capacity.
3. Restart the affected service only if connections are leaked and unrecoverable.
