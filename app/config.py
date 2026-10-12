"""Environment-driven settings. No secrets, and no setting ever selects a defect or fault."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings read from the process environment (no prefix)."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    app_version: str = "0.0.0-dev"
    git_sha: str = "unknown"
    build_time: str | None = None
    deploy_environment: str = "local"
    port: int = Field(default=8000, ge=1, le=65535)
    # Container bind address; binding all interfaces is intentional (bandit B104 accepted).
    host: str = "0.0.0.0"  # nosec B104
    repository_backend: Literal["memory", "sqlite"] = "memory"
    database_url: str = "sqlite:///./stockflow.db"
    seed_data: bool = True
    tax_rate: Decimal = Field(default=Decimal("0.08"), ge=Decimal("0"), le=Decimal("1"))
    low_stock_threshold: int = Field(default=5, ge=0)


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings (cached). Tests build `Settings(...)` directly instead."""
    return Settings()
