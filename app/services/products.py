"""Product use-cases."""

from __future__ import annotations

from app.models.domain import Product
from app.models.errors import ProductNotFoundError
from app.models.schemas import ProductCreate, ProductUpdate, StockAdjustment
from app.repository.base import Repository


def create_product(repo: Repository, data: ProductCreate) -> Product:
    return repo.create_product(data)


def get_product(repo: Repository, product_id: int) -> Product:
    product = repo.get_product(product_id)
    if product is None:
        raise ProductNotFoundError(f"Product {product_id} not found.", [{"product_id": product_id}])
    return product


def list_products(
    repo: Repository,
    *,
    q: str | None,
    category: str | None,
    active: bool | None,
    low_stock: bool | None,
    low_stock_threshold: int,
    limit: int | None,
    offset: int,
) -> tuple[list[Product], int]:
    """List products. `low_stock=True` keeps stock <= threshold; `False`/`None` do not filter."""
    return repo.list_products(
        q=q,
        category=category,
        active=active,
        low_stock_threshold=low_stock_threshold if low_stock else None,
        limit=limit,
        offset=offset,
    )


def update_product(repo: Repository, product_id: int, changes: ProductUpdate) -> Product:
    return repo.update_product(product_id, changes)


def adjust_stock(repo: Repository, product_id: int, adjustment: StockAdjustment) -> Product:
    return repo.adjust_stock(product_id, adjustment.delta)
