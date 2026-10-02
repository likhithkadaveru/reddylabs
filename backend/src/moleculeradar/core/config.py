"""Application configuration.

All configuration arrives from the environment. Nothing secret is ever committed;
`ops/.env.example` documents the contract.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Environment -----------------------------------------------------
    environment: Literal["development", "staging", "production"] = "development"

    # ---- Database --------------------------------------------------------
    database_url: str = "postgresql+psycopg://postgres@localhost:5432/moleculeradar"

    # ---- Object storage for raw payloads --------------------------------
    # Local filesystem by default; S3-compatible storage in production.
    raw_storage_backend: Literal["filesystem", "s3"] = "filesystem"
    raw_storage_path: str = "./var/raw"
    raw_storage_bucket: str | None = None

    # ---- Outbound HTTP ---------------------------------------------------
    # A contactable user agent is a condition of polite use on several public
    # APIs (NCBI, Crossref). Ingestion refuses to run without a contact email.
    http_user_agent: str = "MoleculeRadar/0.1 (+https://llamalabs.example; {contact})"
    http_contact_email: str | None = None
    http_timeout_seconds: float = 30.0
    http_max_retries: int = 4
    # Conservative default: public APIs are shared infrastructure and we have
    # not yet confirmed published rate limits for every source.
    http_requests_per_second: float = 2.0

    # ---- Source credentials (optional, never required for Tier 1) --------
    openfda_api_key: str | None = None
    ncbi_api_key: str | None = None

    # ---- Focus company ---------------------------------------------------
    # The company whose capability graph drives SELL/BUILD scoring. This is
    # configuration, not schema: Molecule Radar is not hard-wired to one client.
    focus_company_name: str = "Dr. Reddy's Laboratories"
    focus_company_alias_seeds: list[str] = Field(
        default_factory=lambda: [
            "Dr. Reddy's Laboratories Ltd",
            "Dr. Reddy's Laboratories Limited",
            "Dr Reddys Laboratories Ltd",
            "Dr Reddy's Laboratories Inc",
            "Dr. Reddy's Laboratories, Inc.",
            "Dr.Reddy's Laboratories Ltd",
            "DRL",
            "Dr Reddys",
            "Dr. Reddy's",
        ]
    )

    # ---- Entity resolution thresholds -----------------------------------
    # Above auto_accept: merge automatically. Below review_floor: treat as a new
    # entity. In between: queue for human review. Never silently merge.
    er_auto_accept_threshold: float = 0.95
    er_review_floor_threshold: float = 0.80

    # ---- Security --------------------------------------------------------
    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 720
    auth_enabled: bool = True
    cors_allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    rate_limit_per_minute: int = 120

    # ---- LLM -------------------------------------------------------------
    anthropic_api_key: str | None = None
    llm_model: str = "claude-sonnet-5-5"
    llm_enabled: bool = False

    @field_validator("database_url")
    @classmethod
    def _require_psycopg_driver(cls, v: str) -> str:
        if v.startswith("postgresql://"):
            # Normalise to the psycopg 3 driver rather than silently using psycopg2.
            return v.replace("postgresql://", "postgresql+psycopg://", 1)
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def user_agent(self) -> str:
        contact = self.http_contact_email or "no-contact-configured"
        return self.http_user_agent.format(contact=contact)

    def validate_for_production(self) -> list[str]:
        """Return a list of configuration problems that must block a production boot."""
        problems: list[str] = []
        if not self.is_production:
            return problems
        if not self.jwt_secret or len(self.jwt_secret) < 32:
            problems.append("MR_JWT_SECRET must be set and at least 32 characters in production.")
        if not self.auth_enabled:
            problems.append("MR_AUTH_ENABLED must not be disabled in production.")
        if not self.http_contact_email:
            problems.append(
                "MR_HTTP_CONTACT_EMAIL must be set: several public APIs require a "
                "contactable user agent as a condition of use."
            )
        if self.raw_storage_backend == "filesystem":
            problems.append(
                "MR_RAW_STORAGE_BACKEND=filesystem is not durable; use s3 in production."
            )
        if any(o == "*" for o in self.cors_allowed_origins):
            problems.append("Wildcard CORS origin is not permitted in production.")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
