"""Stock validation rules. Pure functions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.models.domain import Product
from app.models.errors import (
    InsufficientStockError,
    NegativeStockError,
    ProductInactiveError,
    ProductNotFoundError,
)
from app.models.schemas import OrderItemCreate as OrderLineRequest


def check_stock_available(product: Product, requested: int) -> None:
    """Raise InsufficientStockError if `requested` exceeds the product's stock."""
    if requested > product.quantity_in_stock:
        raise InsufficientStockError(
            f"Insufficient stock for '{product.sku}'.",
            [
                {
                    "product_id": product.id,
                    "sku": product.sku,
                    "requested": requested,
                    "available": product.quantity_in_stock,
                }
            ],
        )


def collect_order_problems(
    requested: Sequence[OrderLineRequest], products: Mapping[int, Product]
) -> None:
    """Raise the first failing category: not found, then inactive, then insufficient stock.

    Within a category every offending line is listed in the error's details.
    """
    missing = [line.product_id for line in requested if line.product_id not in products]
    if missing:
        raise ProductNotFoundError(
            "One or more products were not found.", [{"product_id": pid} for pid in missing]
        )

    inactive = [
        products[line.product_id] for line in requested if not products[line.product_id].active
    ]
    if inactive:
        raise ProductInactiveError(
            "One or more products are inactive.",
            [{"product_id": p.id, "sku": p.sku} for p in inactive],
        )

    shortages: list[dict[str, Any]] = []
    for line in requested:
        try:
            check_stock_available(products[line.product_id], line.quantity)
        except InsufficientStockError as exc:
            shortages.extend(exc.details)
    if shortages:
        raise InsufficientStockError("Insufficient stock for one or more items.", shortages)


def apply_stock_delta(current: int, delta: int, *, product_id: int) -> int:
    """Return `current + delta`; raise NegativeStockError if the result would be below zero."""
    updated = current + delta
    if updated < 0:
        raise NegativeStockError(
            "Stock cannot go below zero.",
            [{"product_id": product_id, "current": current, "delta": delta}],
        )
    return updated
