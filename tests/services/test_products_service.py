"""Product service over real repositories (memory and SQLite)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.errors import DuplicateSkuError, NegativeStockError, ProductNotFoundError
from app.models.schemas import ProductCreate, ProductUpdate, StockAdjustment
from app.repository.base import Repository
from app.services import products as product_service
from tests.helpers import CAB_002, TOL_002, WID_001

pytestmark = pytest.mark.integration


def listing(repo: Repository, **kwargs: object) -> tuple[list[int], int]:
    params: dict[str, object] = {
        "q": None,
        "category": None,
        "active": None,
        "low_stock": None,
        "low_stock_threshold": 5,
        "limit": None,
        "offset": 0,
    }
    params.update(kwargs)
    items, total = product_service.list_products(repo, **params)  # type: ignore[arg-type]
    return [p.id for p in items], total


def test_create_then_get(blank_repo: Repository) -> None:
    created = product_service.create_product(
        blank_repo, ProductCreate(sku="new-1", name="New", price=Decimal("1.00"))
    )
    assert created.sku == "NEW-1"
    assert product_service.get_product(blank_repo, created.id) == created


def test_get_unknown_product_raises_with_details(repo: Repository) -> None:
    with pytest.raises(ProductNotFoundError) as raised:
        product_service.get_product(repo, 12345)
    assert raised.value.details == [{"product_id": 12345}]


def test_create_duplicate_sku_is_rejected(repo: Repository) -> None:
    with pytest.raises(DuplicateSkuError):
        product_service.create_product(
            repo, ProductCreate(sku=" wid-001 ", name="Dup", price=Decimal("1.00"))
        )


def test_list_without_filters_returns_everything_in_id_order(repo: Repository) -> None:
    ids, total = listing(repo)
    assert ids == list(range(1, 11))
    assert total == 10


def test_low_stock_true_keeps_only_products_at_or_below_the_threshold(repo: Repository) -> None:
    assert listing(repo, low_stock=True) == ([CAB_002, TOL_002], 2)


def test_low_stock_threshold_is_inclusive(repo: Repository) -> None:
    assert listing(repo, low_stock=True, low_stock_threshold=3) == ([CAB_002, TOL_002], 2)
    assert listing(repo, low_stock=True, low_stock_threshold=2) == ([TOL_002], 1)
    assert listing(repo, low_stock=True, low_stock_threshold=0) == ([TOL_002], 1)


def test_low_stock_false_and_none_do_not_filter(repo: Repository) -> None:
    """Documented backend behaviour: only low_stock=true filters."""
    assert listing(repo, low_stock=False)[1] == 10
    assert listing(repo, low_stock=None)[1] == 10


def test_filters_combine(repo: Repository) -> None:
    assert listing(repo, category="widgets", active=True) == ([1, 2], 2)
    assert listing(repo, q="cable", low_stock=True) == ([CAB_002], 1)


def test_pagination_reports_the_unpaginated_total(repo: Repository) -> None:
    ids, total = listing(repo, limit=3, offset=2)
    assert ids == [3, 4, 5]
    assert total == 10


def test_update_changes_only_the_given_fields(repo: Repository) -> None:
    before = product_service.get_product(repo, WID_001)
    after = product_service.update_product(repo, WID_001, ProductUpdate(name="Renamed"))
    assert after.name == "Renamed"
    assert after.price == before.price
    assert after.quantity_in_stock == before.quantity_in_stock
    assert after.sku == before.sku


def test_update_to_an_existing_sku_is_rejected(repo: Repository) -> None:
    with pytest.raises(DuplicateSkuError):
        product_service.update_product(repo, WID_001, ProductUpdate(sku="wid-002"))


def test_adjust_stock_up_and_down(repo: Repository) -> None:
    assert (
        product_service.adjust_stock(repo, CAB_002, StockAdjustment(delta=7)).quantity_in_stock
        == 10
    )
    assert (
        product_service.adjust_stock(repo, CAB_002, StockAdjustment(delta=-10)).quantity_in_stock
        == 0
    )


def test_adjust_stock_below_zero_is_rejected_and_unchanged(repo: Repository) -> None:
    with pytest.raises(NegativeStockError):
        product_service.adjust_stock(repo, CAB_002, StockAdjustment(delta=-4))
    assert product_service.get_product(repo, CAB_002).quantity_in_stock == 3


def test_adjust_stock_unknown_product(repo: Repository) -> None:
    with pytest.raises(ProductNotFoundError):
        product_service.adjust_stock(repo, 999, StockAdjustment(delta=1))
