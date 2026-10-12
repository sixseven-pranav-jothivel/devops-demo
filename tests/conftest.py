"""Shared fixtures. Every test gets its own repository and app; nothing is shared between tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app
from app.repository.base import Repository
from app.repository.memory import InMemoryRepository
from app.repository.sqlite import SqliteRepository
from tests.helpers import FrozenClock

BACKENDS = ("memory", "sqlite")

# Every environment variable `Settings` reads; cleared so a developer shell never leaks in.
SETTINGS_ENV_VARS = (
    "APP_VERSION",
    "GIT_SHA",
    "BUILD_TIME",
    "DEPLOY_ENVIRONMENT",
    "PORT",
    "HOST",
    "REPOSITORY_BACKEND",
    "DATABASE_URL",
    "SEED_DATA",
    "TAX_RATE",
    "LOW_STOCK_THRESHOLD",
    "STOCKFLOW_DEMO_FAULT",
)

FULL_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_version="9.9.9-test",
        git_sha=FULL_SHA,
        build_time="2026-01-15T10:30:00Z",
        deploy_environment="ci",
    )


@pytest.fixture
def make_repo(tmp_path: Path, clock: FrozenClock) -> Callable[..., Repository]:
    """Factory: `make_repo("memory"|"sqlite", seed=True)`; SQLite DBs live under tmp_path."""
    counter = iter(range(1000))

    def build(backend: str, *, seed: bool = True, name: str | None = None) -> Repository:
        if backend == "memory":
            return InMemoryRepository(seed=seed, clock=clock)
        db = tmp_path / f"{name or f'stockflow-{next(counter)}'}.db"
        return SqliteRepository(f"sqlite:///{db}", clock=clock, seed=seed)

    return build


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def repo(backend: str, make_repo: Callable[..., Repository]) -> Repository:
    """Seeded repository, run once per backend (memory, sqlite)."""
    return make_repo(backend)


@pytest.fixture
def blank_repo(backend: str, make_repo: Callable[..., Repository]) -> Repository:
    """Empty (unseeded) repository, run once per backend."""
    return make_repo(backend, seed=False)


@pytest.fixture
def client(settings: Settings, repo: Repository) -> Iterator[TestClient]:
    """API client over a seeded repository, once per backend."""
    with TestClient(create_app(settings, repo)) as test_client:
        yield test_client


@pytest.fixture
def blank_client(settings: Settings, blank_repo: Repository) -> Iterator[TestClient]:
    with TestClient(create_app(settings, blank_repo)) as test_client:
        yield test_client
