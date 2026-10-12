"""Inventory validation rules: stock checks, order problem precedence, stock deltas."""

from __future__ import annotations

import pytest

from app.models.errors import (
    InsufficientStockError,
    NegativeStockError,
    ProductInactiveError,
    ProductNotFoundError,
)
from app.models.schemas import OrderItemCreate
from app.services import inventory
from tests.helpers import make_product

pytestmark = pytest.mark.unit


def line(product_id: int, quantity: int) -> OrderItemCreate:
    return OrderItemCreate(product_id=product_id, quantity=quantity)


# ------------------------------------------------------------------ check_stock_available
def test_requesting_less_than_available_is_accepted() -> None:
    inventory.check_stock_available(make_product(quantity_in_stock=10), 9)


def test_requesting_exactly_the_available_stock_is_accepted() -> None:
    inventory.check_stock_available(make_product(quantity_in_stock=10), 10)


def test_requesting_one_more_than_available_is_rejected_with_details() -> None:
    product = make_product(id=7, sku="ABC-1", quantity_in_stock=10)
    with pytest.raises(InsufficientStockError) as raised:
        inventory.check_stock_available(product, 11)
    assert raised.value.code == "insufficient_stock"
    assert raised.value.status_code == 409
    assert raised.value.details == [
        {"product_id": 7, "sku": "ABC-1", "requested": 11, "available": 10}
    ]


def test_out_of_stock_product_rejects_any_quantity() -> None:
    with pytest.raises(InsufficientStockError) as raised:
        inventory.check_stock_available(make_product(quantity_in_stock=0), 1)
    assert raised.value.details[0]["available"] == 0


# ------------------------------------------------------------------ collect_order_problems
def test_a_valid_order_raises_nothing() -> None:
    products = {1: make_product(id=1, quantity_in_stock=5), 2: make_product(id=2, sku="B-2")}
    inventory.collect_order_problems([line(1, 5), line(2, 1)], products)


def test_exact_stock_for_every_line_is_accepted() -> None:
    products = {
        1: make_product(id=1, quantity_in_stock=3),
        2: make_product(id=2, quantity_in_stock=1),
    }
    inventory.collect_order_problems([line(1, 3), line(2, 1)], products)


def test_unknown_products_are_all_listed() -> None:
    products = {1: make_product(id=1)}
    with pytest.raises(ProductNotFoundError) as raised:
        inventory.collect_order_problems([line(98, 1), line(1, 1), line(99, 1)], products)
    assert raised.value.details == [{"product_id": 98}, {"product_id": 99}]


def test_inactive_products_are_all_listed() -> None:
    products = {
        1: make_product(id=1, sku="AAA-1", active=False),
        2: make_product(id=2, sku="BBB-2"),
        3: make_product(id=3, sku="CCC-3", active=False),
    }
    with pytest.raises(ProductInactiveError) as raised:
        inventory.collect_order_problems([line(1, 1), line(2, 1), line(3, 1)], products)
    assert raised.value.details == [
        {"product_id": 1, "sku": "AAA-1"},
        {"product_id": 3, "sku": "CCC-3"},
    ]


def test_every_shortage_is_listed_in_one_error() -> None:
    products = {
        1: make_product(id=1, sku="AAA-1", quantity_in_stock=1),
        2: make_product(id=2, sku="BBB-2", quantity_in_stock=100),
        3: make_product(id=3, sku="CCC-3", quantity_in_stock=0),
    }
    with pytest.raises(InsufficientStockError) as raised:
        inventory.collect_order_problems([line(1, 2), line(2, 100), line(3, 4)], products)
    assert raised.value.details == [
        {"product_id": 1, "sku": "AAA-1", "requested": 2, "available": 1},
        {"product_id": 3, "sku": "CCC-3", "requested": 4, "available": 0},
    ]


def test_precedence_not_found_beats_inactive_and_insufficient() -> None:
    products = {
        1: make_product(id=1, active=False),
        2: make_product(id=2, quantity_in_stock=0),
    }
    with pytest.raises(ProductNotFoundError):
        inventory.collect_order_problems([line(99, 1), line(1, 1), line(2, 5)], products)


def test_precedence_inactive_beats_insufficient() -> None:
    products = {
        1: make_product(id=1, active=False, quantity_in_stock=0),
        2: make_product(id=2, quantity_in_stock=0),
    }
    with pytest.raises(ProductInactiveError):
        inventory.collect_order_problems([line(2, 5), line(1, 1)], products)


def test_an_inactive_product_with_plenty_of_stock_is_still_rejected() -> None:
    products = {1: make_product(id=1, active=False, quantity_in_stock=1000)}
    with pytest.raises(ProductInactiveError):
        inventory.collect_order_problems([line(1, 1)], products)


# ------------------------------------------------------------------ apply_stock_delta
@pytest.mark.parametrize(
    ("current", "delta", "expected"),
    [(10, 5, 15), (10, -3, 7), (10, -10, 0), (0, 1, 1), (0, 1_000_000, 1_000_000)],
)
def test_apply_stock_delta_returns_the_new_quantity(
    current: int, delta: int, expected: int
) -> None:
    assert inventory.apply_stock_delta(current, delta, product_id=1) == expected


def test_stock_can_reach_exactly_zero() -> None:
    assert inventory.apply_stock_delta(4, -4, product_id=1) == 0


@pytest.mark.parametrize(("current", "delta"), [(2, -5), (0, -1), (10, -11)])
def test_stock_going_negative_is_rejected_with_details(current: int, delta: int) -> None:
    with pytest.raises(NegativeStockError) as raised:
        inventory.apply_stock_delta(current, delta, product_id=3)
    assert raised.value.code == "negative_stock"
    assert raised.value.status_code == 409
    assert raised.value.details == [{"product_id": 3, "current": current, "delta": delta}]
