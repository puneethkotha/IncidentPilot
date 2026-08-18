"""FastAPI surface + entrypoint.

Endpoints:
  GET  /healthz                    -- liveness
  GET  /incidents                  -- known incidents
  GET  /incidents/{id}             -- an incident + its latest workflow status
  POST /incidents/{id}/approve     -- deliver a human approval to the waiting workflow
  GET  /scoreboard                 -- latest eval scoreboard (Phase 4)
  POST /webhook/incident           -- ingest an incident and start the durable workflow

The webhook is what Alertmanager/Grafana (or the detector) calls; it kicks off
the DBOS workflow so the remediation loop is durable and auditable. DBOS is
launched in main() before serving.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from incidentpilot.config import get_settings
from incidentpilot.models import Incident

app = FastAPI(title="IncidentPilot", version="0.3.0")

# In-memory indices (the durable state of record lives in DBOS).
_INCIDENTS: dict[str, Incident] = {}
_WORKFLOWS: dict[str, str] = {}  # incident_id -> workflow_id
_SCOREBOARD: dict[str, Any] = {}


class ApprovalBody(BaseModel):
    approved: bool = True
    approver: str = "unknown"
    note: str = ""


def _status_for(incident_id: str) -> dict[str, Any] | None:
    workflow_id = _WORKFLOWS.get(incident_id)
    if not workflow_id:
        return None
    from dbos import DBOS

    from incidentpilot.workflow import STATUS_EVENT

    try:
        return DBOS.get_event(workflow_id, STATUS_EVENT, timeout_seconds=0)
    except Exception:  # noqa: BLE001
        return None


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/incidents")
def list_incidents() -> list[Incident]:
    return list(_INCIDENTS.values())


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str) -> dict[str, Any]:
    if incident_id not in _INCIDENTS:
        raise HTTPException(status_code=404, detail="unknown incident")
    return {
        "incident": _INCIDENTS[incident_id].model_dump(mode="json"),
        "workflow_id": _WORKFLOWS.get(incident_id),
        "status": _status_for(incident_id),
    }


@app.post("/incidents/{incident_id}/approve")
def approve(incident_id: str, body: ApprovalBody) -> dict[str, Any]:
    workflow_id = _WORKFLOWS.get(incident_id)
    if workflow_id is None:
        raise HTTPException(status_code=404, detail="unknown incident / no workflow")
    from dbos import DBOS

    from incidentpilot.workflow import APPROVAL_TOPIC

    DBOS.send(workflow_id, body.model_dump(), topic=APPROVAL_TOPIC)
    return {"incident_id": incident_id, "workflow_id": workflow_id, "delivered": body.model_dump()}


@app.get("/incidents/{incident_id}/postmortem")
def postmortem(incident_id: str) -> dict[str, Any]:
    if incident_id not in _INCIDENTS:
        raise HTTPException(status_code=404, detail="unknown incident")
    workflow_id = _WORKFLOWS.get(incident_id)
    report = None
    if workflow_id:
        from dbos import DBOS

        from incidentpilot.workflow import REPORT_EVENT

        try:
            report = DBOS.get_event(workflow_id, REPORT_EVENT, timeout_seconds=0)
        except Exception:  # noqa: BLE001
            report = None
    if report is None:
        raise HTTPException(status_code=409, detail="incident not yet resolved")

    from incidentpilot.postmortem import render_postmortem

    md = render_postmortem(_INCIDENTS[incident_id], report)
    return {"incident_id": incident_id, "markdown": md}


@app.get("/scoreboard")
def scoreboard() -> dict[str, Any]:
    return _SCOREBOARD


@app.post("/webhook/incident")
def webhook_incident(incident: Incident) -> dict[str, Any]:
    from dbos import DBOS

    from incidentpilot.workflow import handle_incident

    _INCIDENTS[incident.id] = incident
    handle = DBOS.start_workflow(handle_incident, incident)
    _WORKFLOWS[incident.id] = handle.workflow_id
    return {"incident_id": incident.id, "workflow_id": handle.workflow_id, "started": True}


def _dbos_config() -> dict[str, Any]:
    settings = get_settings()
    return {
        "name": "incidentpilot",
        "application_version": app.version,
        "system_database_url": settings.dbos_system_database_url,
    }


def main() -> None:
    """Console entrypoint: launch DBOS (durable execution) then serve the API."""

    import uvicorn
    from dbos import DBOS

    # Register decorated workflows/steps before launch.
    import incidentpilot.workflow  # noqa: F401
    from incidentpilot.tracing import setup_tracing

    setup_tracing()
    DBOS(fastapi=app, config=_dbos_config())
    DBOS.launch()
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
