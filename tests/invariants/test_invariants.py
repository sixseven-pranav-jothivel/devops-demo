"""Business invariants (docs/ARCHITECTURE.md section 4), asserted on both repositories."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest

from app.models.errors import (
    DuplicateSkuError,
    InsufficientStockError,
    ProductInactiveError,
    ProductNotFoundError,
)
from app.models.schemas import NewOrder, OrderCreate, ProductCreate, ProductUpdate
from app.repository.base import Repository, UnitOfWork
from app.services import orders as order_service
from tests.helpers import CAB_002, GAD_001, OLD_001, TAX, TOL_002, WID_001

pytestmark = pytest.mark.integration

D = Decimal


def request(*lines: tuple[int, int]) -> OrderCreate:
    return OrderCreate.model_validate(
        {"line_items": [{"product_id": p, "quantity": q} for p, q in lines]}
    )


def snapshot(repo: Repository) -> tuple[list[tuple[int, int]], int]:
    """(id, stock) for every product, and the number of orders."""
    products, _ = repo.list_products()
    return [(p.id, p.quantity_in_stock) for p in products], repo.list_orders()[1]


# ---------------------------------------------------------- a rejected order mutates nothing
@pytest.mark.parametrize(
    ("lines", "error"),
    [
        pytest.param([(WID_001, 5), (999, 1)], ProductNotFoundError, id="unknown-product"),
        pytest.param([(WID_001, 5), (OLD_001, 1)], ProductInactiveError, id="inactive-product"),
        pytest.param([(WID_001, 5), (CAB_002, 4)], InsufficientStockError, id="short-by-one"),
        pytest.param([(WID_001, 5), (TOL_002, 1)], InsufficientStockError, id="out-of-stock"),
        pytest.param(
            [(WID_001, 501), (GAD_001, 41)], InsufficientStockError, id="every-line-short"
        ),
    ],
)
def test_a_rejected_order_changes_no_stock_and_creates_no_order(
    repo: Repository, lines: list[tuple[int, int]], error: type[Exception]
) -> None:
    before = snapshot(repo)
    with pytest.raises(error):
        order_service.place_order(repo, request(*lines), tax_rate=TAX)
    assert snapshot(repo) == before


# ---------------------------------------------------------- atomicity (failure injected mid-unit)
class FailingAddOrderUnit:
    """Delegates to a real unit of work but fails when the order is stored (after decrements)."""

    def __init__(self, inner: UnitOfWork) -> None:
        self._inner = inner

    def get_product(self, product_id: int) -> Any:
        return self._inner.get_product(product_id)

    def decrement_stock(self, product_id: int, quantity: int) -> None:
        self._inner.decrement_stock(product_id, quantity)

    def add_order(self, order: NewOrder) -> Any:
        raise RuntimeError("injected failure while storing the order")


class FlakyRepository:
    """Wraps a real repository; `fail` selects where the unit of work is sabotaged."""

    def __init__(self, inner: Repository, fail: str) -> None:
        self._inner = inner
        self._fail = fail

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @contextmanager
    def unit_of_work(self) -> Iterator[UnitOfWork]:
        with self._inner.unit_of_work() as uow:
            if self._fail == "add_order":
                yield FailingAddOrderUnit(uow)
            else:
                yield uow
                raise RuntimeError("injected failure just before commit")


@pytest.mark.parametrize("fail", ["add_order", "before-commit"])
def test_failure_mid_unit_rolls_back_stock_and_order_together(repo: Repository, fail: str) -> None:
    before = snapshot(repo)
    flaky: Any = FlakyRepository(repo, fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        order_service.place_order(flaky, request((WID_001, 5), (GAD_001, 2)), tax_rate=TAX)
    assert snapshot(repo) == before
    # ... and the next real order still gets id 1 (nothing half-written was left behind).
    placed = order_service.place_order(repo, request((WID_001, 1)), tax_rate=TAX)
    assert placed.id == 1


# ------------------------------------------------------------------ price captured at order time
def test_unit_price_is_captured_at_order_time(repo: Repository) -> None:
    placed = order_service.place_order(repo, request((WID_001, 2)), tax_rate=TAX)
    assert placed.line_items[0].unit_price == D("9.99")

    repo.update_product(WID_001, ProductUpdate(price=D("50.00"), name="Renamed"))

    stored = repo.get_order(placed.id)
    assert stored is not None
    assert stored.line_items[0].unit_price == D("9.99")
    assert stored.line_items[0].line_total == D("19.98")
    assert stored.line_items[0].product_name == "Standard Widget"
    assert stored.total == D("21.58")

    again = order_service.place_order(repo, request((WID_001, 1)), tax_rate=TAX)
    assert again.line_items[0].unit_price == D("50.00")


# ------------------------------------------------------------------ duplicate SKU
@pytest.mark.parametrize("variant", ["WID-001", "wid-001", "Wid-001", " wid-001 ", "\twid-001\n"])
def test_duplicate_sku_variants_are_rejected_on_create(repo: Repository, variant: str) -> None:
    with pytest.raises(DuplicateSkuError):
        repo.create_product(ProductCreate(sku=variant, name="dup", price=D("1.00")))
    assert repo.list_products()[1] == 10


@pytest.mark.parametrize("variant", ["WID-002", "wid-002", "  Wid-002  "])
def test_duplicate_sku_variants_are_rejected_on_patch(repo: Repository, variant: str) -> None:
    with pytest.raises(DuplicateSkuError):
        repo.update_product(WID_001, ProductUpdate(sku=variant))
    product = repo.get_product(WID_001)
    assert product is not None
    assert product.sku == "WID-001"


# ---------------------------------------------------------- stock never negative or oversold
def test_sequential_orders_cannot_oversell(blank_repo: Repository) -> None:
    product = blank_repo.create_product(
        ProductCreate(sku="LIM-1", name="Limited", price=D("1.00"), quantity_in_stock=5)
    )
    for _ in range(5):
        order_service.place_order(blank_repo, request((product.id, 1)), tax_rate=TAX)
    with pytest.raises(InsufficientStockError):
        order_service.place_order(blank_repo, request((product.id, 1)), tax_rate=TAX)
    fetched = blank_repo.get_product(product.id)
    assert fetched is not None
    assert fetched.quantity_in_stock == 0
    assert blank_repo.list_orders()[1] == 5


def test_order_totals_are_consistent_with_their_lines(repo: Repository) -> None:
    placed = order_service.place_order(
        repo, request((WID_001, 3), (GAD_001, 1), (CAB_002, 2)), tax_rate=TAX
    )
    assert placed.subtotal == sum((i.line_total for i in placed.line_items), D("0.00"))
    assert placed.total == placed.subtotal - placed.discount + placed.tax
    for item in placed.line_items:
        assert item.line_total == (item.unit_price * item.quantity).quantize(D("0.01"))


def test_all_stored_money_is_decimal_with_two_places(repo: Repository) -> None:
    placed = order_service.place_order(repo, request((GAD_001, 1)), tax_rate=TAX)
    stored = repo.get_order(placed.id)
    assert stored is not None
    for value in (stored.subtotal, stored.discount, stored.tax, stored.total):
        assert isinstance(value, Decimal)
        assert value.as_tuple().exponent == -2
    for product in repo.list_products()[0]:
        assert isinstance(product.price, Decimal)
        assert product.price.as_tuple().exponent == -2
