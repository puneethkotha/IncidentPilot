"""Runtime configuration -- IMPLEMENTED (pydantic-settings).

Loaded from environment / .env. No network happens here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All knobs that govern how autonomously IncidentPilot is allowed to act."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- LLM (OpenAI-compatible; default Groq free tier, Ollama offline alt) ---
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    groq_base_url: str = Field(default="https://api.groq.com/openai/v1", alias="GROQ_BASE_URL")
    # Primary model + fallback ladder. On a rate-limit / error we walk down the
    # ladder (strong reasoner -> proven tool-caller -> cheap high-RPD model).
    llm_model: str = Field(default="openai/gpt-oss-120b", alias="LLM_MODEL")
    llm_fallback_models: list[str] = Field(
        default_factory=lambda: ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
    )
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 3

    # --- Infra ---
    database_url: str = Field(
        default="postgresql://incidentpilot:incidentpilot@localhost:5432/incidentpilot",
        alias="DATABASE_URL",
    )
    prometheus_url: str = Field(default="http://localhost:9090", alias="PROMETHEUS_URL")
    # The system under test -- backs query_logs / recent_deploys / get_traces
    # and the remediation control plane.
    target_url: str = Field(default="http://localhost:8080", alias="TARGET_URL")
    # DBOS durable-execution store. SQLite by default (zero setup); point at a
    # Postgres URL to demonstrate recovery across a process restart.
    dbos_system_database_url: str = Field(
        default="sqlite:///incidentpilot_dbos.sqlite", alias="DBOS_SYSTEM_DATABASE_URL"
    )

    # --- Autonomy / safety ---
    # propose_only: never actuate, only surface proposals (safe default).
    # auto: allowed to actuate when policy authorizes and no approval needed.
    mode: Literal["propose_only", "auto"] = Field(
        default="propose_only", alias="INCIDENTPILOT_MODE"
    )
    target_env: str = Field(default="dev", alias="TARGET_ENV")
    allowed_envs: list[str] = Field(default_factory=lambda: ["dev", "staging"])

    # Any proposal with blast_radius strictly above this needs a human.
    blast_radius_auto_threshold: float = 0.30

    # --- Post-action verification ---
    # After acting, poll the incident's own metric until it recovers (or times
    # out). "Recovered" = current value back within recovery_factor x baseline.
    verify_timeout_seconds: float = 60.0
    verify_poll_interval_seconds: float = 3.0
    recovery_factor: float = 3.0

    # Per-action rate limiting (max executions per window). Missing keys fall
    # back to default_action_rate_limit. Values are counts within the window.
    rate_limit_window_seconds: int = 3600
    default_action_rate_limit: int = 3
    action_rate_limits: dict[str, int] = Field(
        default_factory=lambda: {
            "restart_service": 3,
            "rollback_deploy": 2,
            "scale_out": 5,
            "scale_in": 5,
            "clear_cache": 5,
            "failover": 1,
            "throttle_traffic": 3,
            "no_op": 1000,
        }
    )


def get_settings() -> Settings:
    """Factory so callers/tests can construct fresh, overridable settings."""

    return Settings()
