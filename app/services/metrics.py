"""Dashboard / API metrics, computed from the repository (demo scale)."""

from __future__ import annotations

from decimal import Decimal

from app.models.domain import Metrics
from app.repository.base import Repository
from app.services import pricing


def compute_metrics(repo: Repository, *, low_stock_threshold: int) -> Metrics:
    products, _ = repo.list_products(limit=None)
    orders, _ = repo.list_orders(limit=None)
    active = [p for p in products if p.active]
    inventory_value = sum((p.price * p.quantity_in_stock for p in active), Decimal("0.00"))
    revenue = sum((o.total for o in orders), Decimal("0.00"))
    return Metrics(
        product_count=len(products),
        active_product_count=len(active),
        low_stock_count=sum(1 for p in active if 0 < p.quantity_in_stock <= low_stock_threshold),
        out_of_stock_count=sum(1 for p in active if p.quantity_in_stock == 0),
        inventory_value=pricing.quantize_money(inventory_value),
        order_count=len(orders),
        revenue_total=pricing.quantize_money(revenue),
        low_stock_threshold=low_stock_threshold,
    )
