"""FastAPI surface + entrypoint -- SKELETON (in-memory store, TODO persistence).

Endpoints:
  GET  /incidents                  -- list known incidents
  POST /incidents/{id}/approve     -- approve a pending high-blast-radius action
  GET  /scoreboard                 -- latest eval scoreboard
  POST /webhook/incident           -- ingest an incident and start the workflow

The webhook is what Alertmanager/Grafana calls. It kicks off the durable DBOS
workflow so the rest survives crashes.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from incidentpilot.models import Incident

app = FastAPI(title="IncidentPilot", version="0.1.0")

# TODO: replace in-memory store with Postgres (or read back from DBOS state).
_INCIDENTS: dict[str, Incident] = {}
_SCOREBOARD: dict[str, Any] = {}


class ApprovalBody(BaseModel):
    approved: bool = True
    approver: str = "unknown"
    note: str = ""


@app.get("/incidents")
def list_incidents() -> list[Incident]:
    return list(_INCIDENTS.values())


@app.post("/incidents/{incident_id}/approve")
def approve(incident_id: str, body: ApprovalBody) -> dict[str, Any]:
    if incident_id not in _INCIDENTS:
        raise HTTPException(status_code=404, detail="unknown incident")
    # TODO: deliver approval to the waiting workflow, e.g.:
    #   from dbos import DBOS
    #   DBOS.send(workflow_id_for(incident_id), body.model_dump(), topic="approval")
    return {"incident_id": incident_id, "delivered": body.model_dump()}


@app.get("/scoreboard")
def scoreboard() -> dict[str, Any]:
    return _SCOREBOARD


@app.post("/webhook/incident")
def webhook_incident(incident: Incident) -> dict[str, Any]:
    _INCIDENTS[incident.id] = incident
    # TODO: start the durable workflow (requires DBOS.launch() in main()):
    #   from dbos import DBOS
    #   from incidentpilot.workflow import handle_incident
    #   handle = DBOS.start_workflow(handle_incident, incident)
    #   return {"incident_id": incident.id, "workflow_id": handle.workflow_id}
    return {"incident_id": incident.id, "status": "accepted", "started": False}


def main() -> None:
    """Console entrypoint: launch DBOS then serve the API."""

    import uvicorn

    # DBOS is launched here in Phase 3 (durable orchestration); for now the API
    # is a thin surface over the in-memory store.
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
