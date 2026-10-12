"""Settings and repository factory."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.repository.factory import build_repository
from app.repository.memory import InMemoryRepository
from app.repository.sqlite import SqliteRepository

pytestmark = pytest.mark.unit


def test_defaults() -> None:
    settings = Settings()
    assert settings.app_version == "0.0.0-dev"
    assert settings.git_sha == "unknown"
    assert settings.build_time is None
    assert settings.deploy_environment == "local"
    assert settings.port == 8000
    assert settings.repository_backend == "memory"
    assert settings.seed_data is True
    assert settings.tax_rate == Decimal("0.08")
    assert settings.low_stock_threshold == 5


def test_environment_variables_are_read_without_a_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_VERSION", "2.3.4")
    monkeypatch.setenv("GIT_SHA", "abc123")
    monkeypatch.setenv("BUILD_TIME", "2026-02-01T00:00:00Z")
    monkeypatch.setenv("DEPLOY_ENVIRONMENT", "production")
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setenv("REPOSITORY_BACKEND", "sqlite")
    monkeypatch.setenv("SEED_DATA", "false")
    monkeypatch.setenv("TAX_RATE", "0.10")
    monkeypatch.setenv("LOW_STOCK_THRESHOLD", "9")
    settings = Settings()
    assert settings.app_version == "2.3.4"
    assert settings.git_sha == "abc123"
    assert settings.build_time == "2026-02-01T00:00:00Z"
    assert settings.deploy_environment == "production"
    assert settings.port == 10000
    assert settings.repository_backend == "sqlite"
    assert settings.seed_data is False
    assert settings.tax_rate == Decimal("0.10")
    assert settings.low_stock_threshold == 9


def test_unknown_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOMETHING_ELSE", "x")
    assert Settings().app_version == "0.0.0-dev"


@pytest.mark.parametrize("rate", ["-0.01", "1.01"])
def test_tax_rate_must_be_between_zero_and_one(rate: str) -> None:
    with pytest.raises(ValidationError):
        Settings(tax_rate=Decimal(rate))


@pytest.mark.parametrize("rate", ["0", "1"])
def test_tax_rate_bounds_are_inclusive(rate: str) -> None:
    assert Settings(tax_rate=Decimal(rate)).tax_rate == Decimal(rate)


@pytest.mark.parametrize("port", [0, 65536])
def test_port_must_be_valid(port: int) -> None:
    with pytest.raises(ValidationError):
        Settings(port=port)


def test_backend_must_be_known() -> None:
    with pytest.raises(ValidationError):
        Settings(repository_backend="postgres")  # type: ignore[arg-type]


def test_negative_low_stock_threshold_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(low_stock_threshold=-1)


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_VERSION", "first")
    first = get_settings()
    monkeypatch.setenv("APP_VERSION", "second")
    assert get_settings() is first
    assert first.app_version == "first"
    get_settings.cache_clear()
    assert get_settings().app_version == "second"


def test_factory_builds_a_seeded_memory_repository_by_default() -> None:
    repo = build_repository(Settings())
    assert isinstance(repo, InMemoryRepository)
    assert repo.list_products()[1] == 10


def test_factory_respects_seed_data_flag() -> None:
    repo = build_repository(Settings(seed_data=False))
    assert repo.list_products()[1] == 0


def test_factory_builds_sqlite(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'factory.db'}"
    repo = build_repository(Settings(repository_backend="sqlite", database_url=url))
    assert isinstance(repo, SqliteRepository)
    assert repo.list_products()[1] == 10
    assert (tmp_path / "factory.db").exists()
