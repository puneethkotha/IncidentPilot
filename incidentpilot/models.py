"""Domain models for IncidentPilot -- FULLY IMPLEMENTED (pydantic v2).

These are the typed contracts that flow between layers:
Detection -> Diagnosis -> Remediation -> Policy -> Actuation -> Verification.
No I/O happens here; everything is a plain value object.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Severity(str, Enum):
    """Incident severity, roughly aligned to how far a metric drifted."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ActionType(str, Enum):
    """The closed set of remediations IncidentPilot is allowed to reason about.

    A closed enum (not free-text) is deliberate: the PolicyEngine and the
    RemediationRegistry can only authorize/execute actions that exist here.
    """

    NO_OP = "no_op"
    RESTART_SERVICE = "restart_service"
    ROLLBACK_DEPLOY = "rollback_deploy"
    SCALE_OUT = "scale_out"
    SCALE_IN = "scale_in"
    CLEAR_CACHE = "clear_cache"
    FAILOVER = "failover"
    THROTTLE_TRAFFIC = "throttle_traffic"


class Signal(BaseModel):
    """A single observed data point (a metric sample, a log-derived count...)."""

    name: str
    value: float
    source: str = "prometheus"
    labels: dict[str, str] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_now)


class Incident(BaseModel):
    """A detected anomaly awaiting diagnosis + remediation."""

    id: str
    service: str
    metric: str
    value: float
    baseline: float
    z_score: float
    severity: Severity
    description: str = ""
    signals: list[Signal] = Field(default_factory=list)
    detected_at: datetime = Field(default_factory=_now)
    resolved_at: Optional[datetime] = None


class Evidence(BaseModel):
    """A piece of support gathered by the agent via a typed tool call."""

    tool: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class RootCauseHypothesis(BaseModel):
    """The agent's diagnosis. `confidence` gates whether we auto-remediate."""

    cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)
    recommended_action: Optional[ActionType] = None


class RemediationProposal(BaseModel):
    """A concrete, machine-executable action proposal.

    `blast_radius` (0..1) is the key safety signal the PolicyEngine keys on.
    """

    incident_id: str
    action: ActionType
    params: dict[str, Any] = Field(default_factory=dict)
    blast_radius: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class AuthorizationDecision(BaseModel):
    """Output of the PolicyEngine. `reasons` is always populated for audit."""

    allowed: bool
    requires_approval: bool = False
    reasons: list[str] = Field(default_factory=list)


class ActionResult(BaseModel):
    """Outcome of (attempting) an execution."""

    action: ActionType
    executed: bool
    success: bool
    message: str = ""
    output: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_now)


class VerificationResult(BaseModel):
    """Did the metric recover after we acted?"""

    resolved: bool
    metrics_after: dict[str, float] = Field(default_factory=dict)
    notes: str = ""
