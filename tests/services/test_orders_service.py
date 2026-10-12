"""Order service over real repositories (memory and SQLite)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.errors import (
    InsufficientStockError,
    OrderNotFoundError,
    ProductInactiveError,
    ProductNotFoundError,
)
from app.models.schemas import OrderCreate, ProductCreate
from app.repository.base import Repository
from app.services import orders as order_service
from tests.helpers import CAB_002, GAD_001, GAD_002, OLD_001, TAX, TOL_002, WID_001

pytestmark = pytest.mark.integration

D = Decimal


def request(*lines: tuple[int, int]) -> OrderCreate:
    return OrderCreate.model_validate(
        {"line_items": [{"product_id": p, "quantity": q} for p, q in lines]}
    )


def stock_of(repo: Repository, product_id: int) -> int:
    product = repo.get_product(product_id)
    assert product is not None
    return product.quantity_in_stock


def test_small_order_has_no_discount(repo: Repository) -> None:
    order = order_service.place_order(repo, request((WID_001, 2)), tax_rate=TAX)
    assert order.id == 1
    assert order.status == "confirmed"
    assert (order.subtotal, order.discount, order.tax, order.total) == (
        D("19.98"),
        D("0.00"),
        D("1.60"),
        D("21.58"),
    )
    [item] = order.line_items
    assert (item.product_id, item.sku, item.product_name) == (WID_001, "WID-001", "Standard Widget")
    assert (item.quantity, item.unit_price, item.line_total) == (2, D("9.99"), D("19.98"))


def test_order_over_100_gets_five_percent_off_and_tax_on_the_discounted_amount(
    repo: Repository,
) -> None:
    order = order_service.place_order(repo, request((GAD_001, 1)), tax_rate=TAX)
    # 129.99 - 6.50 (6.4995 half-up) = 123.49; tax 9.8792 -> 9.88; total 133.37
    assert (order.subtotal, order.discount, order.tax, order.total) == (
        D("129.99"),
        D("6.50"),
        D("9.88"),
        D("133.37"),
    )


def test_order_over_500_gets_ten_percent_off(repo: Repository) -> None:
    order = order_service.place_order(repo, request((GAD_002, 2)), tax_rate=TAX)
    # 698.00 - 69.80 = 628.20; tax 50.256 -> 50.26; total 678.46
    assert (order.subtotal, order.discount, order.tax, order.total) == (
        D("698.00"),
        D("69.80"),
        D("50.26"),
        D("678.46"),
    )


def test_multi_line_order_totals_are_summed_across_lines(repo: Repository) -> None:
    order = order_service.place_order(repo, request((WID_001, 3), (GAD_001, 1)), tax_rate=TAX)
    # 29.97 + 129.99 = 159.96; discount 7.998 -> 8.00; taxable 151.96; tax 12.1568 -> 12.16
    assert [i.line_total for i in order.line_items] == [D("29.97"), D("129.99")]
    assert (order.subtotal, order.discount, order.tax, order.total) == (
        D("159.96"),
        D("8.00"),
        D("12.16"),
        D("164.12"),
    )


def test_tax_rate_comes_from_the_caller(repo: Repository) -> None:
    order = order_service.place_order(repo, request((WID_001, 2)), tax_rate=D("0"))
    assert order.tax == D("0.00")
    assert order.total == D("19.98")


def test_stock_is_decremented_for_every_line(repo: Repository) -> None:
    order_service.place_order(repo, request((WID_001, 7), (GAD_001, 2)), tax_rate=TAX)
    assert stock_of(repo, WID_001) == 493
    assert stock_of(repo, GAD_001) == 38


def test_ordering_exactly_the_available_stock_succeeds(repo: Repository) -> None:
    order_service.place_order(repo, request((CAB_002, 3)), tax_rate=TAX)
    assert stock_of(repo, CAB_002) == 0


def test_ordering_one_more_than_available_fails_and_changes_nothing(repo: Repository) -> None:
    with pytest.raises(InsufficientStockError) as raised:
        order_service.place_order(repo, request((CAB_002, 4)), tax_rate=TAX)
    assert raised.value.details == [
        {"product_id": CAB_002, "sku": "CAB-002", "requested": 4, "available": 3}
    ]
    assert stock_of(repo, CAB_002) == 3
    assert repo.list_orders()[1] == 0


def test_out_of_stock_product_cannot_be_ordered(repo: Repository) -> None:
    with pytest.raises(InsufficientStockError):
        order_service.place_order(repo, request((TOL_002, 1)), tax_rate=TAX)


def test_unknown_product_is_reported_as_not_found(repo: Repository) -> None:
    with pytest.raises(ProductNotFoundError) as raised:
        order_service.place_order(repo, request((WID_001, 1), (999, 1)), tax_rate=TAX)
    assert raised.value.details == [{"product_id": 999}]
    assert stock_of(repo, WID_001) == 500


def test_inactive_product_is_rejected(repo: Repository) -> None:
    with pytest.raises(ProductInactiveError) as raised:
        order_service.place_order(repo, request((OLD_001, 1)), tax_rate=TAX)
    assert raised.value.details == [{"product_id": OLD_001, "sku": "OLD-001"}]


def test_a_failing_second_line_leaves_the_first_line_untouched(repo: Repository) -> None:
    with pytest.raises(InsufficientStockError):
        order_service.place_order(repo, request((WID_001, 10), (CAB_002, 99)), tax_rate=TAX)
    assert stock_of(repo, WID_001) == 500
    assert repo.list_orders() == ([], 0)


def test_order_ids_increase_and_rejections_do_not_consume_ids(repo: Repository) -> None:
    first = order_service.place_order(repo, request((WID_001, 1)), tax_rate=TAX)
    with pytest.raises(InsufficientStockError):
        order_service.place_order(repo, request((TOL_002, 1)), tax_rate=TAX)
    second = order_service.place_order(repo, request((WID_001, 1)), tax_rate=TAX)
    assert (first.id, second.id) == (1, 2)


def test_get_order_returns_the_stored_order(repo: Repository) -> None:
    placed = order_service.place_order(repo, request((WID_001, 2)), tax_rate=TAX)
    assert order_service.get_order(repo, placed.id) == placed


def test_get_unknown_order_raises_with_details(repo: Repository) -> None:
    with pytest.raises(OrderNotFoundError) as raised:
        order_service.get_order(repo, 42)
    assert raised.value.code == "order_not_found"
    assert raised.value.details == [{"order_id": 42}]


def test_list_orders_is_newest_first_and_paginates(repo: Repository) -> None:
    ids = [
        order_service.place_order(repo, request((WID_001, 1)), tax_rate=TAX).id for _ in range(5)
    ]
    items, total = order_service.list_orders(repo, limit=2, offset=0)
    assert total == 5
    assert [o.id for o in items] == [5, 4]
    items, _ = order_service.list_orders(repo, limit=2, offset=4)
    assert [o.id for o in items] == [1]
    assert ids == [1, 2, 3, 4, 5]
    assert order_service.list_orders(repo, limit=10, offset=10) == ([], 5)


def test_new_products_can_be_ordered_at_their_own_price(blank_repo: Repository) -> None:
    product = blank_repo.create_product(
        ProductCreate(sku="X-1", name="X", price=D("99.99"), quantity_in_stock=1)
    )
    order = order_service.place_order(blank_repo, request((product.id, 1)), tax_rate=TAX)
    assert order.total == D("107.99")
