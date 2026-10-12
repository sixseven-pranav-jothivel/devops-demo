"""/api/products: list, create, get, patch, stock."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers import (
    CAB_002,
    SEED_LOW_STOCK_IDS,
    TOL_002,
    WID_001,
    assert_error,
    order_payload,
    product_payload,
)

pytestmark = pytest.mark.integration

PRODUCT_KEYS = {
    "id", "sku", "name", "description", "price", "quantity_in_stock",
    "category", "active", "created_at", "updated_at",
}  # fmt: skip


def ids(response: Any) -> list[int]:
    return [item["id"] for item in response.json()["items"]]


# ------------------------------------------------------------------ list
def test_list_returns_the_seed_catalogue_in_the_documented_envelope(client: TestClient) -> None:
    response = client.get("/api/products")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert (body["total"], body["limit"], body["offset"]) == (10, 100, 0)
    assert ids(response) == list(range(1, 11))
    first = body["items"][0]
    assert set(first) == PRODUCT_KEYS
    assert first["sku"] == "WID-001"
    assert first["price"] == "9.99"  # money is a 2dp string
    assert first["created_at"].endswith("Z")


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("q=widget", [1, 2]),
        ("q=WIDGET", [1, 2]),
        ("q=cab-", [5, 6]),
        ("q=%25", []),
        ("category=widgets", [1, 2]),
        ("category=GADGETS", [3, 4, 10]),
        ("category=Gadget", []),
        ("active=false", [10]),
        ("active=true", [1, 2, 3, 4, 5, 6, 7, 8, 9]),
        ("low_stock=true", SEED_LOW_STOCK_IDS),
        ("low_stock=true&active=true", SEED_LOW_STOCK_IDS),
        ("q=gadget&active=true", [3, 4]),
        ("category=accessories&low_stock=true", [CAB_002]),
    ],
)
def test_list_filters(client: TestClient, query: str, expected: list[int]) -> None:
    response = client.get(f"/api/products?{query}")
    assert response.status_code == 200
    assert ids(response) == expected
    assert response.json()["total"] == len(expected)


def test_low_stock_false_does_not_filter(client: TestClient) -> None:
    """Documented backend behaviour: only `low_stock=true` filters."""
    assert client.get("/api/products?low_stock=false").json()["total"] == 10


def test_pagination(client: TestClient) -> None:
    response = client.get("/api/products?limit=3&offset=2")
    assert ids(response) == [3, 4, 5]
    body = response.json()
    assert (body["total"], body["limit"], body["offset"]) == (10, 3, 2)
    assert ids(client.get("/api/products?limit=3&offset=9")) == [10]
    assert ids(client.get("/api/products?limit=3&offset=10")) == []
    assert client.get("/api/products?limit=3&offset=10").json()["total"] == 10


def test_total_is_counted_after_filters_before_pagination(client: TestClient) -> None:
    body = client.get("/api/products?category=gadgets&limit=1&offset=1").json()
    assert [p["id"] for p in body["items"]] == [4]
    assert body["total"] == 3


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=201", "limit=-1", "limit=abc", "offset=-1", "active=maybe", "low_stock=2"],
)
def test_list_rejects_bad_query_parameters(client: TestClient, query: str) -> None:
    assert_error(client.get(f"/api/products?{query}"), 422, "validation_error")


def test_limit_bounds_are_inclusive(client: TestClient) -> None:
    assert client.get("/api/products?limit=1").status_code == 200
    assert client.get("/api/products?limit=200").status_code == 200


# ------------------------------------------------------------------ create
def test_create_returns_201_location_and_normalised_sku(blank_client: TestClient) -> None:
    response = blank_client.post(
        "/api/products", json=product_payload(sku="  wid-003 ", price="4.50", category="Widgets")
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == PRODUCT_KEYS
    assert body["id"] == 1
    assert response.headers["location"] == "/api/products/1"
    assert body["sku"] == "WID-003"
    assert body["price"] == "4.50"
    assert body["quantity_in_stock"] == 20
    assert body["category"] == "Widgets"
    assert body["active"] is True
    assert body["created_at"] == body["updated_at"]
    assert blank_client.get("/api/products/1").json() == body


def test_create_applies_defaults(blank_client: TestClient) -> None:
    response = blank_client.post(
        "/api/products", json={"sku": "min-1", "name": "Minimal", "price": 3}
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["description"], body["quantity_in_stock"]) == ("", 0)
    assert (body["category"], body["active"], body["price"]) == ("General", True, "3.00")


@pytest.mark.parametrize("price", ["0", "0.00", 19.99, 19, "19.9", "9999999999.99"])
def test_create_accepts_valid_money_as_string_or_number(
    blank_client: TestClient, price: Any
) -> None:
    response = blank_client.post("/api/products", json=product_payload(price=price))
    assert response.status_code == 201
    assert len(response.json()["price"].split(".")[1]) == 2


def test_create_duplicate_sku_returns_409(client: TestClient) -> None:
    response = client.post("/api/products", json=product_payload(sku="WID-001"))
    assert_error(response, 409, "duplicate_sku", details=[{"sku": "WID-001"}])


@pytest.mark.parametrize("variant", ["wid-001", "Wid-001", "  wid-001  "])
def test_create_duplicate_sku_is_case_and_whitespace_insensitive(
    client: TestClient, variant: str
) -> None:
    error = assert_error(
        client.post("/api/products", json=product_payload(sku=variant)), 409, "duplicate_sku"
    )
    assert error["details"] == [{"sku": "WID-001"}]
    assert client.get("/api/products").json()["total"] == 10


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price", "-0.01"),
        ("price", -5),
        ("price", "1.234"),
        ("price", 1.234),
        ("price", "abc"),
        ("price", "10000000000.00"),
        ("price", None),
        ("sku", "x"),
        ("sku", ""),
        ("sku", "has space"),
        ("sku", "-lead"),
        ("sku", "s" * 33),
        ("name", ""),
        ("name", "   "),
        ("name", "n" * 121),
        ("quantity_in_stock", -1),
        ("quantity_in_stock", 1.5),
        ("quantity_in_stock", "many"),
        ("description", "d" * 1001),
        ("category", ""),
        ("category", "c" * 61),
        ("active", "perhaps"),
    ],
)
def test_create_validation_errors(blank_client: TestClient, field: str, value: Any) -> None:
    response = blank_client.post("/api/products", json=product_payload(**{field: value}))
    error = assert_error(response, 422, "validation_error")
    assert error["details"]
    assert all(set(d) == {"field", "message"} for d in error["details"])
    assert error["details"][0]["field"] == field
    assert blank_client.get("/api/products").json()["total"] == 0


def test_create_negative_price_detail_message(blank_client: TestClient) -> None:
    response = blank_client.post("/api/products", json=product_payload(price="-1.00"))
    error = assert_error(response, 422, "validation_error")
    assert error["details"] == [
        {"field": "price", "message": "Input should be greater than or equal to 0"}
    ]


def test_create_three_decimal_places_are_rejected(blank_client: TestClient) -> None:
    response = blank_client.post("/api/products", json=product_payload(price="1.005"))
    error = assert_error(response, 422, "validation_error")
    assert error["details"][0]["field"] == "price"
    assert "decimal places" in error["details"][0]["message"]


@pytest.mark.parametrize("missing", ["sku", "name", "price"])
def test_create_requires_sku_name_and_price(blank_client: TestClient, missing: str) -> None:
    body = product_payload()
    del body[missing]
    error = assert_error(blank_client.post("/api/products", json=body), 422, "validation_error")
    assert error["details"] == [{"field": missing, "message": "Field required"}]


def test_create_rejects_unknown_fields(blank_client: TestClient) -> None:
    response = blank_client.post("/api/products", json=product_payload(colour="red"))
    error = assert_error(response, 422, "validation_error")
    assert error["details"][0]["field"] == "colour"


def test_create_rejects_a_non_json_body(blank_client: TestClient) -> None:
    response = blank_client.post(
        "/api/products", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert_error(response, 422, "validation_error")


def test_create_rejects_an_empty_body(blank_client: TestClient) -> None:
    assert_error(blank_client.post("/api/products"), 422, "validation_error")


def test_create_rejects_a_json_array_body(blank_client: TestClient) -> None:
    assert_error(blank_client.post("/api/products", json=[1, 2]), 422, "validation_error")


def test_validation_precedes_duplicate_check(client: TestClient) -> None:
    response = client.post("/api/products", json=product_payload(sku="WID-001", price="-1"))
    assert_error(response, 422, "validation_error")


# ------------------------------------------------------------------ get
def test_get_product(client: TestClient) -> None:
    response = client.get("/api/products/3")
    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["sku"], body["price"], body["quantity_in_stock"]) == (
        3, "GAD-001", "129.99", 40,
    )  # fmt: skip


@pytest.mark.parametrize("product_id", [99, 0, -1])
def test_get_unknown_product_is_404(client: TestClient, product_id: int) -> None:
    assert_error(
        client.get(f"/api/products/{product_id}"),
        404,
        "product_not_found",
        details=[{"product_id": product_id}],
    )


def test_get_with_a_non_integer_id_is_a_validation_error(client: TestClient) -> None:
    error = assert_error(client.get("/api/products/abc"), 422, "validation_error")
    assert error["details"][0]["field"] == "product_id"


# ------------------------------------------------------------------ patch
def test_patch_updates_only_the_given_fields_and_bumps_updated_at(
    client: TestClient, clock: Any
) -> None:
    before = client.get("/api/products/2").json()
    clock.advance(3600)
    response = client.patch("/api/products/2", json={"price": "26.00", "active": False})
    assert response.status_code == 200
    after = response.json()
    assert after["price"] == "26.00"
    assert after["active"] is False
    for field in ("sku", "name", "description", "category", "quantity_in_stock", "created_at"):
        assert after[field] == before[field]
    assert after["updated_at"] > before["updated_at"]
    assert client.get("/api/products/2").json() == after


def test_patch_all_patchable_fields(client: TestClient) -> None:
    response = client.patch(
        "/api/products/1",
        json={
            "sku": "wid-new",
            "name": "  New name ",
            "description": "",
            "price": 11.5,
            "category": "Other",
            "active": False,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["sku"], body["name"], body["description"]) == ("WID-NEW", "New name", "")
    assert (body["price"], body["category"], body["active"]) == ("11.50", "Other", False)


def test_patch_rejects_quantity_in_stock(client: TestClient) -> None:
    response = client.patch("/api/products/1", json={"quantity_in_stock": 5})
    error = assert_error(response, 422, "validation_error")
    assert error["details"][0]["field"] == "quantity_in_stock"
    assert client.get("/api/products/1").json()["quantity_in_stock"] == 500


def test_patch_to_an_existing_sku_is_409_and_changes_nothing(client: TestClient) -> None:
    before = client.get("/api/products/1").json()
    response = client.patch("/api/products/1", json={"sku": "wid-002"})
    assert_error(response, 409, "duplicate_sku", details=[{"sku": "WID-002"}])
    assert client.get("/api/products/1").json() == before


def test_patch_to_own_sku_in_another_case_is_allowed(client: TestClient) -> None:
    response = client.patch("/api/products/1", json={"sku": " wid-001 "})
    assert response.status_code == 200
    assert response.json()["sku"] == "WID-001"


def test_patch_unknown_product_is_404(client: TestClient) -> None:
    assert_error(
        client.patch("/api/products/99", json={"name": "x"}),
        404,
        "product_not_found",
        details=[{"product_id": 99}],
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": None},
        {"price": "-1"},
        {"price": "1.234"},
        {"sku": "x"},
        {"name": ""},
        {"active": "perhaps"},
        {"unknown": 1},
    ],
)
def test_patch_validation_errors(client: TestClient, body: dict[str, Any]) -> None:
    assert_error(client.patch("/api/products/1", json=body), 422, "validation_error")


def test_patch_empty_body_message(client: TestClient) -> None:
    error = assert_error(client.patch("/api/products/1", json={}), 422, "validation_error")
    assert "At least one field" in error["details"][0]["message"]


def test_patch_validation_precedes_not_found(client: TestClient) -> None:
    assert_error(client.patch("/api/products/99", json={}), 422, "validation_error")


def test_price_change_does_not_alter_existing_orders(client: TestClient) -> None:
    order = client.post("/api/orders", json=order_payload((WID_001, 2))).json()
    client.patch(f"/api/products/{WID_001}", json={"price": "99.00"})
    fetched = client.get(f"/api/orders/{order['id']}").json()
    assert fetched["line_items"][0]["unit_price"] == "9.99"
    assert fetched["total"] == "21.58"


# ------------------------------------------------------------------ stock
def test_stock_increase_and_decrease(client: TestClient) -> None:
    up = client.post(f"/api/products/{CAB_002}/stock", json={"delta": 7, "reason": "delivery"})
    assert up.status_code == 200
    assert up.json()["quantity_in_stock"] == 10
    down = client.post(f"/api/products/{CAB_002}/stock", json={"delta": -10})
    assert down.json()["quantity_in_stock"] == 0
    assert set(down.json()) == PRODUCT_KEYS


def test_stock_below_zero_is_409_and_unchanged(client: TestClient) -> None:
    response = client.post(f"/api/products/{CAB_002}/stock", json={"delta": -4})
    assert_error(
        response,
        409,
        "negative_stock",
        details=[{"product_id": CAB_002, "current": 3, "delta": -4}],
    )
    assert client.get(f"/api/products/{CAB_002}").json()["quantity_in_stock"] == 3


def test_stock_adjustment_on_an_out_of_stock_product(client: TestClient) -> None:
    assert_error(
        client.post(f"/api/products/{TOL_002}/stock", json={"delta": -1}), 409, "negative_stock"
    )
    assert (
        client.post(f"/api/products/{TOL_002}/stock", json={"delta": 1}).json()["quantity_in_stock"]
        == 1
    )


def test_stock_unknown_product_is_404(client: TestClient) -> None:
    assert_error(client.post("/api/products/99/stock", json={"delta": 1}), 404, "product_not_found")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"delta": 0},
        {"delta": 1.5},
        {"delta": "x"},
        {"delta": None},
        {"delta": 1_000_001},
        {"delta": -1_000_001},
        {"delta": 1, "reason": "r" * 201},
        {"delta": 1, "extra": True},
    ],
)
def test_stock_validation_errors(client: TestClient, body: dict[str, Any]) -> None:
    assert_error(client.post("/api/products/1/stock", json=body), 422, "validation_error")
    assert client.get("/api/products/1").json()["quantity_in_stock"] == 500


def test_stock_delta_bounds_are_inclusive(client: TestClient) -> None:
    assert client.post("/api/products/1/stock", json={"delta": 1_000_000}).status_code == 200
    assert client.post("/api/products/1/stock", json={"delta": -1_000_000}).status_code == 200


# ------------------------------------------------------------------ routing
def test_delete_is_not_allowed(client: TestClient) -> None:
    response = client.delete("/api/products/1")
    assert_error(response, 405, "method_not_allowed")
    assert "GET" in response.headers["allow"]


def test_put_collection_is_not_allowed(client: TestClient) -> None:
    assert_error(client.put("/api/products", json={}), 405, "method_not_allowed")
