"""/api/orders: placement, pricing, validation, error precedence, listing."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.repository.base import Repository
from tests.helpers import (
    CAB_002,
    GAD_001,
    GAD_002,
    OLD_001,
    TOL_002,
    WID_001,
    assert_error,
    order_payload,
    product_payload,
)

pytestmark = pytest.mark.integration

ORDER_KEYS = {"id", "status", "line_items", "subtotal", "discount", "tax", "total", "created_at"}
ITEM_KEYS = {"product_id", "sku", "product_name", "quantity", "unit_price", "line_total"}


def stock(client: TestClient, product_id: int) -> int:
    return int(client.get(f"/api/products/{product_id}").json()["quantity_in_stock"])


def state(client: TestClient) -> tuple[list[int], int]:
    items = client.get("/api/products").json()["items"]
    return [p["quantity_in_stock"] for p in items], client.get("/api/orders").json()["total"]


# ------------------------------------------------------------------ happy path
def test_place_order_returns_201_with_documented_shape(client: TestClient) -> None:
    response = client.post("/api/orders", json=order_payload((WID_001, 2)))
    assert response.status_code == 201
    assert response.headers["location"] == "/api/orders/1"
    body = response.json()
    assert set(body) == ORDER_KEYS
    assert body["id"] == 1
    assert body["status"] == "confirmed"
    assert body["created_at"] == "2026-01-15T10:30:00Z"
    assert set(body["line_items"][0]) == ITEM_KEYS
    assert body["line_items"] == [
        {
            "product_id": 1,
            "sku": "WID-001",
            "product_name": "Standard Widget",
            "quantity": 2,
            "unit_price": "9.99",
            "line_total": "19.98",
        }
    ]
    assert (body["subtotal"], body["discount"], body["tax"], body["total"]) == (
        "19.98", "0.00", "1.60", "21.58",
    )  # fmt: skip


def test_place_order_decrements_stock_and_is_retrievable(client: TestClient) -> None:
    placed = client.post("/api/orders", json=order_payload((WID_001, 7), (GAD_001, 2))).json()
    assert stock(client, WID_001) == 493
    assert stock(client, GAD_001) == 38
    assert client.get(f"/api/orders/{placed['id']}").json() == placed


def test_multi_line_order_with_a_five_percent_discount(client: TestClient) -> None:
    body = client.post("/api/orders", json=order_payload((WID_001, 3), (GAD_001, 1))).json()
    assert [i["line_total"] for i in body["line_items"]] == ["29.97", "129.99"]
    assert (body["subtotal"], body["discount"], body["tax"], body["total"]) == (
        "159.96", "8.00", "12.16", "164.12",
    )  # fmt: skip


def test_ten_percent_tier_on_a_big_order(client: TestClient) -> None:
    body = client.post("/api/orders", json=order_payload((GAD_002, 2))).json()
    assert (body["subtotal"], body["discount"], body["tax"], body["total"]) == (
        "698.00", "69.80", "50.26", "678.46",
    )  # fmt: skip


@pytest.mark.parametrize(
    ("price", "discount", "tax", "total"),
    [
        ("1.25", "0.00", "0.10", "1.35"),
        ("99.99", "0.00", "8.00", "107.99"),
        ("100.00", "5.00", "7.60", "102.60"),
        ("150.00", "7.50", "11.40", "153.90"),
        ("499.99", "25.00", "38.00", "512.99"),
        ("500.00", "50.00", "36.00", "486.00"),
        ("600.00", "60.00", "43.20", "583.20"),
    ],
)
def test_tier_boundaries_end_to_end(
    blank_client: TestClient, price: str, discount: str, tax: str, total: str
) -> None:
    pid = blank_client.post(
        "/api/products", json=product_payload(sku="BND-1", price=price, quantity_in_stock=1)
    ).json()["id"]
    body = blank_client.post("/api/orders", json=order_payload((pid, 1))).json()
    assert (body["subtotal"], body["discount"], body["tax"], body["total"]) == (
        price, discount, tax, total,
    )  # fmt: skip


def test_tax_rate_setting_is_honoured(repo: Repository) -> None:
    with TestClient(create_app(Settings(tax_rate=Decimal("0.10")), repo)) as client:
        body = client.post("/api/orders", json=order_payload((WID_001, 2))).json()
    assert (body["tax"], body["total"]) == ("2.00", "21.98")


def test_ordering_exactly_the_available_stock_is_accepted(client: TestClient) -> None:
    assert client.post("/api/orders", json=order_payload((CAB_002, 3))).status_code == 201
    assert stock(client, CAB_002) == 0
    assert_error(
        client.post("/api/orders", json=order_payload((CAB_002, 1))), 409, "insufficient_stock"
    )


def test_one_more_than_available_is_rejected(client: TestClient) -> None:
    response = client.post("/api/orders", json=order_payload((CAB_002, 4)))
    assert_error(
        response,
        409,
        "insufficient_stock",
        details=[{"product_id": CAB_002, "sku": "CAB-002", "requested": 4, "available": 3}],
    )
    assert stock(client, CAB_002) == 3


def test_price_is_captured_at_order_time(client: TestClient) -> None:
    first = client.post("/api/orders", json=order_payload((WID_001, 1))).json()
    client.patch(f"/api/products/{WID_001}", json={"price": "100.00"})
    second = client.post("/api/orders", json=order_payload((WID_001, 1))).json()
    assert first["line_items"][0]["unit_price"] == "9.99"
    assert second["line_items"][0]["unit_price"] == "100.00"
    assert client.get(f"/api/orders/{first['id']}").json()["total"] == first["total"]


# ------------------------------------------------------------------ business errors
def test_unknown_product_lists_every_missing_id(client: TestClient) -> None:
    response = client.post("/api/orders", json=order_payload((WID_001, 1), (98, 1), (99, 1)))
    assert_error(
        response, 404, "product_not_found", details=[{"product_id": 98}, {"product_id": 99}]
    )


def test_inactive_product_is_rejected(client: TestClient) -> None:
    response = client.post("/api/orders", json=order_payload((OLD_001, 1)))
    assert_error(
        response, 409, "product_inactive", details=[{"product_id": OLD_001, "sku": "OLD-001"}]
    )


def test_every_shortage_is_listed(client: TestClient) -> None:
    response = client.post(
        "/api/orders", json=order_payload((TOL_002, 2), (CAB_002, 9), (WID_001, 1))
    )
    assert_error(
        response,
        409,
        "insufficient_stock",
        details=[
            {"product_id": TOL_002, "sku": "TOL-002", "requested": 2, "available": 0},
            {"product_id": CAB_002, "sku": "CAB-002", "requested": 9, "available": 3},
        ],
    )


@pytest.mark.parametrize(
    ("lines", "status", "code"),
    [
        # not found > inactive > insufficient
        ([(99, 1), (OLD_001, 1), (TOL_002, 5)], 404, "product_not_found"),
        ([(OLD_001, 1), (TOL_002, 5)], 409, "product_inactive"),
        ([(WID_001, 1), (TOL_002, 5)], 409, "insufficient_stock"),
        ([(99, 1), (TOL_002, 5)], 404, "product_not_found"),
    ],
)
def test_error_precedence(
    client: TestClient, lines: list[tuple[int, int]], status: int, code: str
) -> None:
    assert_error(client.post("/api/orders", json=order_payload(*lines)), status, code)


def test_validation_error_beats_business_errors(client: TestClient) -> None:
    body = {
        "line_items": [
            {"product_id": 99, "quantity": 1},  # would be 404
            {"product_id": TOL_002, "quantity": 0},  # invalid: 422 wins
        ]
    }
    error = assert_error(client.post("/api/orders", json=body), 422, "validation_error")
    assert error["details"][0]["field"] == "line_items.1.quantity"


@pytest.mark.parametrize(
    "lines",
    [
        [(99, 1)],
        [(OLD_001, 1)],
        [(WID_001, 5), (OLD_001, 1)],
        [(WID_001, 5), (GAD_001, 2), (TOL_002, 1)],
        [(WID_001, 501)],
    ],
)
def test_a_rejected_order_mutates_nothing(client: TestClient, lines: list[tuple[int, int]]) -> None:
    before = state(client)
    assert client.post("/api/orders", json=order_payload(*lines)).status_code in (404, 409)
    assert state(client) == before
    assert client.get("/api/metrics").json()["order_count"] == 0
    assert client.get("/api/metrics").json()["inventory_value"] == "30473.00"


def test_rejected_orders_do_not_consume_order_ids(client: TestClient) -> None:
    client.post("/api/orders", json=order_payload((TOL_002, 1)))
    assert client.post("/api/orders", json=order_payload((WID_001, 1))).json()["id"] == 1


# ------------------------------------------------------------------ validation
def test_empty_line_items(client: TestClient) -> None:
    error = assert_error(
        client.post("/api/orders", json={"line_items": []}), 422, "validation_error"
    )
    assert error["details"][0]["field"] == "line_items"


@pytest.mark.parametrize("quantity", [0, -1, 10001, 1.5, "x"])
def test_invalid_quantity(client: TestClient, quantity: Any) -> None:
    body = {"line_items": [{"product_id": 1, "quantity": quantity}]}
    error = assert_error(client.post("/api/orders", json=body), 422, "validation_error")
    assert error["details"][0]["field"] == "line_items.0.quantity"


def test_quantity_zero_message(client: TestClient) -> None:
    body = {"line_items": [{"product_id": 1, "quantity": 0}]}
    error = assert_error(client.post("/api/orders", json=body), 422, "validation_error")
    assert error["details"] == [
        {"field": "line_items.0.quantity", "message": "Input should be greater than 0"}
    ]


@pytest.mark.parametrize("product_id", [0, -3, "abc", None])
def test_invalid_product_id(client: TestClient, product_id: Any) -> None:
    body = {"line_items": [{"product_id": product_id, "quantity": 1}]}
    error = assert_error(client.post("/api/orders", json=body), 422, "validation_error")
    assert error["details"][0]["field"] == "line_items.0.product_id"


def test_more_than_50_lines(client: TestClient) -> None:
    body = {"line_items": [{"product_id": 1, "quantity": 1} for _ in range(51)]}
    assert_error(client.post("/api/orders", json=body), 422, "validation_error")


def test_duplicate_product_ids(client: TestClient) -> None:
    error = assert_error(
        client.post("/api/orders", json=order_payload((1, 1), (2, 1), (1, 2))),
        422,
        "validation_error",
    )
    assert "Duplicate product_id" in error["details"][0]["message"]


@pytest.mark.parametrize("body", [{}, {"line_items": None}, {"line_items": "x"}, {"lines": []}, []])
def test_malformed_bodies(client: TestClient, body: Any) -> None:
    assert_error(client.post("/api/orders", json=body), 422, "validation_error")


def test_missing_body(client: TestClient) -> None:
    assert_error(client.post("/api/orders"), 422, "validation_error")


def test_client_supplied_prices_are_not_accepted(client: TestClient) -> None:
    body = {"line_items": [{"product_id": 1, "quantity": 1, "unit_price": "0.01"}]}
    error = assert_error(client.post("/api/orders", json=body), 422, "validation_error")
    assert error["details"][0]["field"] == "line_items.0.unit_price"


# ------------------------------------------------------------------ listing / get
@pytest.fixture
def five_orders(client: TestClient) -> TestClient:
    for _ in range(5):
        assert client.post("/api/orders", json=order_payload((WID_001, 1))).status_code == 201
    return client


def test_list_orders_empty(client: TestClient) -> None:
    assert client.get("/api/orders").json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


def test_list_orders_is_newest_first(five_orders: TestClient) -> None:
    body = five_orders.get("/api/orders").json()
    assert [o["id"] for o in body["items"]] == [5, 4, 3, 2, 1]
    assert (body["total"], body["limit"], body["offset"]) == (5, 50, 0)
    assert set(body["items"][0]) == ORDER_KEYS


def test_list_orders_pagination(five_orders: TestClient) -> None:
    body = five_orders.get("/api/orders?limit=2&offset=1").json()
    assert [o["id"] for o in body["items"]] == [4, 3]
    assert (body["total"], body["limit"], body["offset"]) == (5, 2, 1)
    assert five_orders.get("/api/orders?limit=2&offset=5").json()["items"] == []


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1", "limit=x"])
def test_list_orders_rejects_bad_pagination(client: TestClient, query: str) -> None:
    assert_error(client.get(f"/api/orders?{query}"), 422, "validation_error")


def test_get_order_not_found(client: TestClient) -> None:
    assert_error(client.get("/api/orders/99"), 404, "order_not_found", details=[{"order_id": 99}])


def test_get_order_with_a_bad_id(client: TestClient) -> None:
    assert_error(client.get("/api/orders/abc"), 422, "validation_error")


def test_orders_cannot_be_modified_or_deleted(five_orders: TestClient) -> None:
    assert_error(five_orders.delete("/api/orders/1"), 405, "method_not_allowed")
    assert_error(five_orders.put("/api/orders/1", json={}), 405, "method_not_allowed")
    assert_error(five_orders.patch("/api/orders/1", json={}), 405, "method_not_allowed")


def test_order_flow_is_repeatable_on_fresh_apps(
    settings: Settings, make_repo: Callable[..., Repository], backend: str
) -> None:
    """Each test builds its own app and repo: two apps never share state."""
    one = TestClient(create_app(settings, make_repo(backend)))
    two = TestClient(create_app(settings, make_repo(backend)))
    with one, two:
        one.post("/api/orders", json=order_payload((WID_001, 10)))
        assert stock(one, WID_001) == 490
        assert stock(two, WID_001) == 500
