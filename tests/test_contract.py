"""OpenAPI contract: paths, methods, fields and constraints match docs/ARCHITECTURE.md section 5."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

EXPECTED_OPERATIONS = {
    ("get", "/health"),
    ("get", "/ready"),
    ("get", "/api/version"),
    ("get", "/api/metrics"),
    ("get", "/api/products"),
    ("post", "/api/products"),
    ("get", "/api/products/{product_id}"),
    ("patch", "/api/products/{product_id}"),
    ("post", "/api/products/{product_id}/stock"),
    ("get", "/api/orders"),
    ("post", "/api/orders"),
    ("get", "/api/orders/{order_id}"),
}


@pytest.fixture
def spec(client: TestClient) -> dict[str, Any]:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    return response.json()  # type: ignore[no-any-return]


def schema(spec: dict[str, Any], name: str) -> dict[str, Any]:
    return spec["components"]["schemas"][name]  # type: ignore[no-any-return]


def test_exactly_the_documented_operations_exist(spec: dict[str, Any]) -> None:
    actual = {
        (method, path)
        for path, item in spec["paths"].items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert actual == EXPECTED_OPERATIONS


def test_the_dashboard_is_not_part_of_the_api_schema(spec: dict[str, Any]) -> None:
    assert "/" not in spec["paths"]


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("post", "/api/products", "201"),
        ("post", "/api/orders", "201"),
        ("get", "/api/products", "200"),
        ("get", "/api/orders", "200"),
        ("get", "/api/products/{product_id}", "200"),
        ("patch", "/api/products/{product_id}", "200"),
        ("post", "/api/products/{product_id}/stock", "200"),
        ("get", "/api/orders/{order_id}", "200"),
        ("get", "/api/metrics", "200"),
        ("get", "/api/version", "200"),
    ],
)
def test_success_status_codes(spec: dict[str, Any], method: str, path: str, status: str) -> None:
    assert status in spec["paths"][path][method]["responses"]


def test_product_fields(spec: dict[str, Any]) -> None:
    assert set(schema(spec, "Product")["properties"]) == {
        "id", "sku", "name", "description", "price", "quantity_in_stock",
        "category", "active", "created_at", "updated_at",
    }  # fmt: skip


def test_order_and_item_fields(spec: dict[str, Any]) -> None:
    assert set(schema(spec, "Order")["properties"]) == {
        "id", "status", "line_items", "subtotal", "discount", "tax", "total", "created_at",
    }  # fmt: skip
    assert set(schema(spec, "OrderItem")["properties"]) == {
        "product_id", "sku", "product_name", "quantity", "unit_price", "line_total",
    }  # fmt: skip


def test_metrics_and_version_fields(spec: dict[str, Any]) -> None:
    assert set(schema(spec, "Metrics")["properties"]) == {
        "product_count", "active_product_count", "low_stock_count", "out_of_stock_count",
        "inventory_value", "order_count", "revenue_total", "low_stock_threshold",
    }  # fmt: skip
    assert set(schema(spec, "VersionInfo")["properties"]) == {
        "name", "version", "git_sha", "environment", "build_time", "repository", "persistent",
    }  # fmt: skip


def test_list_envelopes(spec: dict[str, Any]) -> None:
    for name in ("ProductList", "OrderList"):
        assert set(schema(spec, name)["properties"]) == {"items", "total", "limit", "offset"}


def test_product_create_contract(spec: dict[str, Any]) -> None:
    create = schema(spec, "ProductCreate")
    assert set(create["required"]) == {"sku", "name", "price"}
    assert create["additionalProperties"] is False
    assert set(create["properties"]) == {
        "sku", "name", "description", "price", "quantity_in_stock", "category", "active",
    }  # fmt: skip


def test_product_update_cannot_change_stock(spec: dict[str, Any]) -> None:
    update = schema(spec, "ProductUpdate")
    assert "quantity_in_stock" not in update["properties"]
    assert set(update["properties"]) == {
        "sku",
        "name",
        "description",
        "price",
        "category",
        "active",
    }
    assert update["additionalProperties"] is False


def test_order_create_limits(spec: dict[str, Any]) -> None:
    order = schema(spec, "OrderCreate")
    lines = order["properties"]["line_items"]
    assert (lines["minItems"], lines["maxItems"]) == (1, 50)
    item = schema(spec, "OrderItemCreate")
    assert item["properties"]["product_id"]["exclusiveMinimum"] == 0
    assert item["properties"]["quantity"]["exclusiveMinimum"] == 0
    assert item["properties"]["quantity"]["maximum"] == 10000


def test_stock_adjustment_limits(spec: dict[str, Any]) -> None:
    delta = schema(spec, "StockAdjustment")["properties"]["delta"]
    assert (delta["minimum"], delta["maximum"]) == (-1_000_000, 1_000_000)


def test_pagination_parameter_bounds(spec: dict[str, Any]) -> None:
    def params(path: str) -> dict[str, Any]:
        return {p["name"]: p["schema"] for p in spec["paths"][path]["get"]["parameters"]}

    products = params("/api/products")
    assert (products["limit"]["minimum"], products["limit"]["maximum"]) == (1, 200)
    assert products["limit"]["default"] == 100
    assert products["offset"]["minimum"] == 0
    assert {"q", "category", "active", "low_stock", "limit", "offset"} <= set(products)
    orders = params("/api/orders")
    assert (orders["limit"]["minimum"], orders["limit"]["maximum"]) == (1, 200)
    assert orders["limit"]["default"] == 50


def test_money_is_a_decimal_in_the_schema_never_a_float(spec: dict[str, Any]) -> None:
    """OpenAPI renders Decimal as number|string; it must never be declared as a bare float."""
    price = schema(spec, "Product")["properties"]["price"]
    assert price.get("type") == "string" or any(
        option.get("type") == "string" for option in price.get("anyOf", [])
    )
