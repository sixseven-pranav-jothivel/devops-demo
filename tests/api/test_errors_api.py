"""Error body shape, routing errors and the generic 500 path."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.repository.memory import InMemoryRepository
from tests.helpers import assert_error

pytestmark = pytest.mark.integration


def test_unknown_api_route_is_a_json_404(client: TestClient) -> None:
    response = client.get("/api/nope")
    assert response.headers["content-type"].startswith("application/json")
    assert_error(response, 404, "not_found", details=[])


def test_unknown_top_level_route_is_a_json_404(client: TestClient) -> None:
    assert_error(client.get("/does-not-exist"), 404, "not_found")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/health"),
        ("POST", "/ready"),
        ("DELETE", "/api/version"),
        ("PUT", "/api/metrics"),
        ("DELETE", "/api/products/1"),
        ("PATCH", "/api/products"),
        ("GET", "/api/products/1/stock"),
        ("DELETE", "/api/orders"),
    ],
)
def test_wrong_verb_is_a_json_405(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path)
    assert_error(response, 405, "method_not_allowed", details=[])
    assert response.headers["allow"]


def test_validation_details_use_dotted_field_paths(client: TestClient) -> None:
    body = {"line_items": [{"product_id": 1, "quantity": 1}, {"product_id": 2, "quantity": -1}]}
    error = assert_error(client.post("/api/orders", json=body), 422, "validation_error")
    assert error["details"][0]["field"] == "line_items.1.quantity"
    assert error["message"] == "Request validation failed."


def test_query_validation_errors_name_the_parameter(client: TestClient) -> None:
    error = assert_error(client.get("/api/products?limit=0"), 422, "validation_error")
    assert error["details"][0]["field"] == "limit"


def test_errors_never_leak_internals(client: TestClient) -> None:
    for response in (
        client.get("/api/products/99"),
        client.post("/api/products", json={"sku": "bad sku"}),
        client.get("/api/orders/99"),
    ):
        assert "Traceback" not in response.text
        assert "app/" not in response.text


class FailingRepository(InMemoryRepository):
    def list_products(self, **kwargs: Any) -> Any:
        raise RuntimeError("secret internal detail: /srv/app/db.sqlite password=hunter2")


def test_unhandled_exception_becomes_a_generic_500(settings: Settings) -> None:
    app = create_app(settings, FailingRepository())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/products")
        error = assert_error(response, 500, "internal_error", details=[])
        assert error["message"] == "An internal error occurred."
        assert "hunter2" not in response.text
        assert "secret" not in response.text
        assert "Traceback" not in response.text
        assert response.headers["content-type"].startswith("application/json")
        # the app keeps serving after a failed request
        assert client.get("/health").json() == {"status": "ok"}


def test_unhandled_exception_is_logged_server_side(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(settings, FailingRepository())
    with TestClient(app, raise_server_exceptions=False) as client, caplog.at_level("ERROR"):
        client.get("/api/products")
    assert any("Unhandled error" in record.message for record in caplog.records)
    assert any(record.exc_info for record in caplog.records)


def test_health_stays_up_while_data_routes_fail(settings: Settings) -> None:
    with TestClient(create_app(settings, FailingRepository()), raise_server_exceptions=False) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/api/products").status_code == 500
        assert c.get("/api/products/1").status_code == 200  # get_product is not broken
