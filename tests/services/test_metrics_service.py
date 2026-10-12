"""Metrics computation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.schemas import OrderCreate, ProductCreate, ProductUpdate
from app.repository.base import Repository
from app.services import metrics, orders
from tests.helpers import GAD_001, OLD_001, SEED_INVENTORY_VALUE, TAX, WID_001

pytestmark = pytest.mark.integration

D = Decimal


def test_seed_metrics(repo: Repository) -> None:
    result = metrics.compute_metrics(repo, low_stock_threshold=5)
    assert result.product_count == 10
    assert result.active_product_count == 9
    assert result.low_stock_count == 1  # HDMI cable (3); the drill (0) is out of stock instead
    assert result.out_of_stock_count == 1
    assert result.inventory_value == D(SEED_INVENTORY_VALUE)
    assert result.order_count == 0
    assert result.revenue_total == D("0.00")
    assert result.low_stock_threshold == 5


def test_inactive_products_are_excluded_from_value_and_stock_counts(repo: Repository) -> None:
    # OLD-001 (5.00 x 25 = 125.00) is inactive and must not be in the 30473.00 seed value.
    repo.update_product(OLD_001, ProductUpdate(active=True))
    assert metrics.compute_metrics(repo, low_stock_threshold=5).inventory_value == D("30598.00")


def test_low_stock_boundary(blank_repo: Repository) -> None:
    for sku, qty in (("Q-0", 0), ("Q-1", 1), ("Q-5", 5), ("Q-6", 6)):
        blank_repo.create_product(
            ProductCreate(sku=sku, name=sku, price=D("1.00"), quantity_in_stock=qty)
        )
    blank_repo.create_product(
        ProductCreate(sku="Q-OFF", name="off", price=D("1.00"), quantity_in_stock=2, active=False)
    )
    result = metrics.compute_metrics(blank_repo, low_stock_threshold=5)
    assert (
        result.low_stock_count == 2
    )  # qty 1 and qty 5 (inclusive); 0 is out-of-stock; inactive ignored
    assert result.out_of_stock_count == 1
    assert result.active_product_count == 4
    assert result.product_count == 5


def test_revenue_and_order_count_follow_orders(repo: Repository) -> None:
    for pid, qty in ((WID_001, 2), (GAD_001, 1)):
        orders.place_order(
            repo,
            OrderCreate.model_validate({"line_items": [{"product_id": pid, "quantity": qty}]}),
            tax_rate=TAX,
        )
    result = metrics.compute_metrics(repo, low_stock_threshold=5)
    assert result.order_count == 2
    assert result.revenue_total == D("21.58") + D("133.37")
    # Stock moved: 2 widgets and 1 gadget fewer.
    assert result.inventory_value == D(SEED_INVENTORY_VALUE) - D("9.99") * 2 - D("129.99")


def test_empty_catalogue(blank_repo: Repository) -> None:
    result = metrics.compute_metrics(blank_repo, low_stock_threshold=5)
    assert result.product_count == 0
    assert result.inventory_value == D("0.00")
    assert str(result.inventory_value) == "0.00"
