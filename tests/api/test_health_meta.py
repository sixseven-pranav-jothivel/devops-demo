"""/health, /ready, /api/version, /api/metrics."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.repository.base import Repository
from app.repository.memory import InMemoryRepository
from tests.conftest import FULL_SHA
from tests.helpers import SEED_INVENTORY_VALUE, WID_001, order_payload

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------ /health
def test_health_is_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


class ExplodingRepository:
    """Any use at all fails: proves a route does not touch the repository."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"repository.{name} must not be touched")


def test_health_does_not_touch_the_repository(settings: Settings) -> None:
    app = create_app(settings, ExplodingRepository())  # type: ignore[arg-type]
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}


# ------------------------------------------------------------------ /ready
def test_ready_reports_the_backend(client: TestClient, backend: str) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "repository": backend}


class BrokenPingRepository(InMemoryRepository):
    def ping(self) -> None:
        raise ConnectionError("database is down")


def test_ready_returns_503_when_the_repository_ping_fails(settings: Settings) -> None:
    with TestClient(create_app(settings, BrokenPingRepository())) as client:
        response = client.get("/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["detail"]  # short reason
        assert "database is down" not in response.text  # no internal message leaks
        assert set(body) == {"status", "detail"}
        # liveness is independent of readiness
        assert client.get("/health").status_code == 200


# ------------------------------------------------------------------ /api/version
def test_version_reports_settings_and_repository(client: TestClient, backend: str) -> None:
    response = client.get("/api/version")
    assert response.status_code == 200
    assert response.json() == {
        "name": "stockflow",
        "version": "9.9.9-test",
        "git_sha": FULL_SHA,
        "environment": "ci",
        "build_time": "2026-01-15T10:30:00Z",
        "repository": backend,
        "persistent": backend == "sqlite",
    }


def test_version_without_build_time_is_null(repo: Repository) -> None:
    with TestClient(create_app(Settings(), repo)) as client:
        body = client.get("/api/version").json()
    assert body["build_time"] is None
    assert body["version"] == "0.0.0-dev"
    assert body["git_sha"] == "unknown"
    assert body["environment"] == "local"


def test_version_is_driven_by_environment_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("APP_VERSION", "3.1.4")
    monkeypatch.setenv("GIT_SHA", "deadbeef" * 5)
    monkeypatch.setenv("BUILD_TIME", "2026-03-01T12:00:00Z")
    monkeypatch.setenv("DEPLOY_ENVIRONMENT", "production")
    monkeypatch.setenv("REPOSITORY_BACKEND", "sqlite")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'env.db'}")
    with TestClient(create_app()) as client:  # no explicit settings: read from the environment
        assert client.get("/api/version").json() == {
            "name": "stockflow",
            "version": "3.1.4",
            "git_sha": "deadbeef" * 5,
            "environment": "production",
            "build_time": "2026-03-01T12:00:00Z",
            "repository": "sqlite",
            "persistent": True,
        }
        assert client.get("/ready").json() == {"status": "ready", "repository": "sqlite"}
        assert client.get("/api/products").json()["total"] == 10


def test_default_environment_uses_the_memory_repository() -> None:
    with TestClient(create_app()) as client:
        body = client.get("/api/version").json()
    assert body["repository"] == "memory"
    assert body["persistent"] is False


def test_openapi_version_follows_app_version(client: TestClient) -> None:
    assert client.get("/openapi.json").json()["info"]["version"] == "9.9.9-test"


# ------------------------------------------------------------------ /api/metrics
def test_metrics_for_the_seed_catalogue(client: TestClient) -> None:
    response = client.get("/api/metrics")
    assert response.status_code == 200
    assert response.json() == {
        "product_count": 10,
        "active_product_count": 9,
        "low_stock_count": 1,
        "out_of_stock_count": 1,
        "inventory_value": SEED_INVENTORY_VALUE,
        "order_count": 0,
        "revenue_total": "0.00",
        "low_stock_threshold": 5,
    }


def test_metrics_follow_orders_and_stock_changes(client: TestClient) -> None:
    placed = client.post("/api/orders", json=order_payload((WID_001, 2)))
    assert placed.status_code == 201
    client.post("/api/products/6/stock", json={"delta": 10})  # HDMI cable: 3 -> 13, no longer low
    metrics = client.get("/api/metrics").json()
    assert metrics["order_count"] == 1
    assert metrics["revenue_total"] == "21.58"
    assert metrics["low_stock_count"] == 0
    # 30473.00 - 2 x 9.99 (sold) + 10 x 12.80 (restocked)
    assert metrics["inventory_value"] == "30581.02"


def test_metrics_low_stock_threshold_comes_from_settings(repo: Repository) -> None:
    with TestClient(create_app(Settings(low_stock_threshold=20), repo)) as client:
        metrics = client.get("/api/metrics").json()
    assert metrics["low_stock_threshold"] == 20
    assert (
        metrics["low_stock_count"] == 2
    )  # HDMI cable (3) and Gadget Pro (15); the drill (0) is out
