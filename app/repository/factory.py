"""Repository construction from settings."""

from __future__ import annotations

from app.config import Settings
from app.repository.base import Repository
from app.repository.memory import InMemoryRepository
from app.repository.sqlite import SqliteRepository


def build_repository(settings: Settings) -> Repository:
    if settings.repository_backend == "sqlite":
        return SqliteRepository(settings.database_url, seed=settings.seed_data)
    return InMemoryRepository(seed=settings.seed_data)
