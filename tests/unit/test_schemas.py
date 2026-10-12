"""Request-schema validation (pydantic) - no I/O."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.models.schemas import (
    OrderCreate,
    ProductCreate,
    ProductUpdate,
    StockAdjustment,
)

pytestmark = pytest.mark.unit

BASE: dict[str, Any] = {"sku": "abc-1", "name": "Thing", "price": "1.00"}


def create(**overrides: Any) -> ProductCreate:
    return ProductCreate(**{**BASE, **overrides})


# ------------------------------------------------------------------ ProductCreate
def test_defaults_are_applied() -> None:
    product = create()
    assert product.description == ""
    assert product.quantity_in_stock == 0
    assert product.category == "General"
    assert product.active is True


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [("wid-003", "WID-003"), ("  wid-003  ", "WID-003"), ("Wid_003", "WID_003"), ("a1", "A1")],
)
def test_sku_is_stripped_and_upper_cased(raw: str, normalised: str) -> None:
    assert create(sku=raw).sku == normalised


@pytest.mark.parametrize("sku", ["", " ", "a", "-abc", "_abc", "ab cd", "ab/cd", "a" * 33, "ab.cd"])
def test_invalid_skus_are_rejected(sku: str) -> None:
    with pytest.raises(ValidationError):
        create(sku=sku)


def test_sku_boundary_lengths() -> None:
    assert create(sku="a" * 32).sku == "A" * 32
    assert create(sku="ab").sku == "AB"


@pytest.mark.parametrize("price", ["0", "0.00", "4.5", "4.50", 4.5, 19, "9999999999.99"])
def test_valid_prices(price: Any) -> None:
    assert isinstance(create(price=price).price, Decimal)


@pytest.mark.parametrize(
    "price", ["-0.01", "-1", -5, "1.234", 1.234, "0.001", "abc", "", None, "10000000000.00", "NaN"]
)
def test_invalid_prices_are_rejected(price: Any) -> None:
    with pytest.raises(ValidationError):
        create(price=price)


def test_price_keeps_decimal_precision_from_json_numbers() -> None:
    assert create(price=19.99).price == Decimal("19.99")


@pytest.mark.parametrize("name", ["", "   ", "x" * 121])
def test_invalid_names(name: str) -> None:
    with pytest.raises(ValidationError):
        create(name=name)


def test_name_is_stripped_and_may_be_120_long() -> None:
    assert create(name="  Widget  ").name == "Widget"
    assert len(create(name="x" * 120).name) == 120


def test_description_and_category_limits() -> None:
    assert len(create(description="d" * 1000).description) == 1000
    with pytest.raises(ValidationError):
        create(description="d" * 1001)
    with pytest.raises(ValidationError):
        create(category="")
    with pytest.raises(ValidationError):
        create(category="c" * 61)


def test_negative_opening_stock_is_rejected() -> None:
    with pytest.raises(ValidationError):
        create(quantity_in_stock=-1)


def test_unknown_fields_are_forbidden() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        create(colour="red")


@pytest.mark.parametrize("missing", ["sku", "name", "price"])
def test_required_fields(missing: str) -> None:
    body = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(ValidationError, match="missing"):
        ProductCreate(**body)


# ------------------------------------------------------------------ ProductUpdate
def test_update_requires_at_least_one_field() -> None:
    with pytest.raises(ValidationError, match="At least one field"):
        ProductUpdate()


@pytest.mark.parametrize("field", ["sku", "name", "description", "price", "category", "active"])
def test_update_fields_cannot_be_null(field: str) -> None:
    with pytest.raises(ValidationError, match="cannot be null"):
        ProductUpdate(**{field: None})


def test_update_rejects_quantity_in_stock() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ProductUpdate(quantity_in_stock=5)  # type: ignore[call-arg]


def test_update_tracks_which_fields_were_set() -> None:
    update = ProductUpdate(name="New", active=False)
    assert update.model_fields_set == {"name", "active"}


def test_update_may_clear_the_description() -> None:
    assert ProductUpdate(description="").description == ""


def test_update_normalises_sku_and_validates_price() -> None:
    assert ProductUpdate(sku=" new-1 ").sku == "NEW-1"
    with pytest.raises(ValidationError):
        ProductUpdate(price="1.234")
    with pytest.raises(ValidationError):
        ProductUpdate(price="-1")


# ------------------------------------------------------------------ StockAdjustment
@pytest.mark.parametrize("delta", [-1_000_000, -1, 1, 1_000_000])
def test_valid_deltas(delta: int) -> None:
    assert StockAdjustment(delta=delta).delta == delta


@pytest.mark.parametrize("delta", [0, 1_000_001, -1_000_001, 1.5, "x", None])
def test_invalid_deltas(delta: Any) -> None:
    with pytest.raises(ValidationError):
        StockAdjustment(delta=delta)


def test_reason_is_optional_and_limited_to_200() -> None:
    assert StockAdjustment(delta=1).reason is None
    assert StockAdjustment(delta=1, reason="r" * 200).reason == "r" * 200
    with pytest.raises(ValidationError):
        StockAdjustment(delta=1, reason="r" * 201)


# ------------------------------------------------------------------ OrderCreate
def order(*lines: tuple[int, int]) -> OrderCreate:
    return OrderCreate(
        line_items=[{"product_id": pid, "quantity": qty} for pid, qty in lines]  # type: ignore[misc]
    )


def test_a_valid_order() -> None:
    parsed = order((1, 2), (3, 1))
    assert [(i.product_id, i.quantity) for i in parsed.line_items] == [(1, 2), (3, 1)]


def test_order_needs_at_least_one_line() -> None:
    with pytest.raises(ValidationError):
        OrderCreate(line_items=[])


def test_order_allows_50_lines_but_not_51() -> None:
    assert len(order(*[(i, 1) for i in range(1, 51)]).line_items) == 50
    with pytest.raises(ValidationError):
        order(*[(i, 1) for i in range(1, 52)])


@pytest.mark.parametrize("quantity", [0, -1, 10001])
def test_invalid_quantities(quantity: int) -> None:
    with pytest.raises(ValidationError):
        order((1, quantity))


def test_quantity_boundaries() -> None:
    assert order((1, 1)).line_items[0].quantity == 1
    assert order((1, 10000)).line_items[0].quantity == 10000


@pytest.mark.parametrize("product_id", [0, -1])
def test_invalid_product_ids(product_id: int) -> None:
    with pytest.raises(ValidationError):
        order((product_id, 1))


def test_duplicate_product_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Duplicate product_id"):
        order((1, 1), (2, 1), (1, 3))


def test_order_lines_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        OrderCreate(line_items=[{"product_id": 1, "quantity": 1, "price": "0.01"}])  # type: ignore[list-item]
