# Runbook: worker crash loop / elevated 5xx

**Symptoms:** `http_5xx_rate` jumps, logs show workers crashing or restarting,
availability drops. Latency may look fine for the requests that do succeed.

**Most common cause:** a bad rollout — the new build crashes on startup or on a
code path exercised in production. Check `recent_deploys`; a rollout immediately
before onset is the strongest signal.

**Remediation:**
1. If a deploy correlates with onset, **roll back the deploy** — highest signal,
   reversible.
2. If no deploy correlates, **restart the service** to clear a wedged process.
