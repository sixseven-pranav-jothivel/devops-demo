"""Repository and unit-of-work interfaces."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from app.models.domain import Order, Product
from app.models.schemas import NewOrder, ProductCreate, ProductUpdate


class UnitOfWork(Protocol):
    """Exclusive, all-or-nothing scope. Reads inside see the unit's own staged writes."""

    def get_product(self, product_id: int) -> Product | None: ...

    def decrement_stock(self, product_id: int, quantity: int) -> None:
        """Raise InsufficientStockError if quantity_in_stock < quantity (atomic guard)."""
        ...

    def add_order(self, order: NewOrder) -> Order:
        """Assign id + created_at; return the stored Order."""
        ...


class Repository(Protocol):
    @property
    def backend_name(self) -> str: ...

    @property
    def persistent(self) -> bool: ...

    def ping(self) -> None:
        """Raise on failure."""
        ...

    # products
    def create_product(self, data: ProductCreate) -> Product: ...

    def get_product(self, product_id: int) -> Product | None: ...

    def list_products(
        self,
        *,
        q: str | None = None,
        category: str | None = None,
        active: bool | None = None,
        low_stock_threshold: int | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Product], int]: ...

    def update_product(self, product_id: int, changes: ProductUpdate) -> Product: ...

    def adjust_stock(self, product_id: int, delta: int) -> Product: ...

    # orders
    def unit_of_work(self) -> AbstractContextManager[UnitOfWork]:
        """Commit on normal exit, roll back on exception (the exception propagates)."""
        ...

    def get_order(self, order_id: int) -> Order | None: ...

    def list_orders(
        self, *, limit: int | None = None, offset: int = 0
    ) -> tuple[list[Order], int]: ...
