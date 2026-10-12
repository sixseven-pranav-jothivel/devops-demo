"""In-memory repository (production). Thread-safe; units of work stage writes until commit."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from threading import RLock

from app.models.domain import Order, OrderItem, Product, utc_now
from app.models.errors import (
    DuplicateSkuError,
    InsufficientStockError,
    ProductNotFoundError,
)
from app.models.schemas import NewOrder, ProductCreate, ProductUpdate
from app.repository.base import UnitOfWork
from app.repository.seed import SEED_PRODUCTS
from app.services.inventory import apply_stock_delta


def _duplicate_sku(sku: str) -> DuplicateSkuError:
    return DuplicateSkuError(f"SKU '{sku}' already exists.", [{"sku": sku}])


class _MemoryUnitOfWork:
    def __init__(self, repo: InMemoryRepository) -> None:
        self._repo = repo
        self.stock: dict[int, int] = {}
        self.orders: list[Order] = []

    def get_product(self, product_id: int) -> Product | None:
        product = self._repo.products.get(product_id)
        if product is None:
            return None
        if product_id in self.stock:
            return product.model_copy(update={"quantity_in_stock": self.stock[product_id]})
        return product

    def decrement_stock(self, product_id: int, quantity: int) -> None:
        product = self.get_product(product_id)
        if product is None:
            raise ProductNotFoundError(
                f"Product {product_id} not found.", [{"product_id": product_id}]
            )
        if product.quantity_in_stock < quantity:
            raise InsufficientStockError(
                f"Insufficient stock for '{product.sku}'.",
                [
                    {
                        "product_id": product_id,
                        "sku": product.sku,
                        "requested": quantity,
                        "available": product.quantity_in_stock,
                    }
                ],
            )
        self.stock[product_id] = product.quantity_in_stock - quantity

    def add_order(self, order: NewOrder) -> Order:
        stored = Order(
            id=self._repo.next_order_id + len(self.orders),
            status="confirmed",
            line_items=[OrderItem(**item.model_dump()) for item in order.line_items],
            subtotal=order.subtotal,
            discount=order.discount,
            tax=order.tax,
            total=order.total,
            created_at=self._repo.clock(),
        )
        self.orders.append(stored)
        return stored


class InMemoryRepository:
    def __init__(self, seed: bool = True, clock: Callable[[], datetime] = utc_now) -> None:
        self.clock = clock
        self.lock = RLock()
        self.products: dict[int, Product] = {}
        self.orders: dict[int, Order] = {}
        self.sku_index: dict[str, int] = {}
        self.next_product_id = 1
        self.next_order_id = 1
        if seed:
            for data in SEED_PRODUCTS:
                self.create_product(data)

    @property
    def backend_name(self) -> str:
        return "memory"

    @property
    def persistent(self) -> bool:
        return False

    def ping(self) -> None:
        with self.lock:
            len(self.products)

    # ------------------------------------------------------------------ products
    def create_product(self, data: ProductCreate) -> Product:
        with self.lock:
            if data.sku in self.sku_index:
                raise _duplicate_sku(data.sku)
            now = self.clock()
            product = Product(
                id=self.next_product_id,
                sku=data.sku,
                name=data.name,
                description=data.description,
                price=data.price,
                quantity_in_stock=data.quantity_in_stock,
                category=data.category,
                active=data.active,
                created_at=now,
                updated_at=now,
            )
            self.products[product.id] = product
            self.sku_index[product.sku] = product.id
            self.next_product_id += 1
            return product

    def get_product(self, product_id: int) -> Product | None:
        with self.lock:
            return self.products.get(product_id)

    def list_products(
        self,
        *,
        q: str | None = None,
        category: str | None = None,
        active: bool | None = None,
        low_stock_threshold: int | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Product], int]:
        needle = q.strip().lower() if q else ""
        wanted_category = category.lower() if category else None
        with self.lock:
            matches = [
                p
                for p in sorted(self.products.values(), key=lambda item: item.id)
                if (not needle or needle in p.name.lower() or needle in p.sku.lower())
                and (wanted_category is None or p.category.lower() == wanted_category)
                and (active is None or p.active == active)
                and (low_stock_threshold is None or p.quantity_in_stock <= low_stock_threshold)
            ]
        total = len(matches)
        end = None if limit is None else offset + limit
        return matches[offset:end], total

    def update_product(self, product_id: int, changes: ProductUpdate) -> Product:
        with self.lock:
            current = self.products.get(product_id)
            if current is None:
                raise ProductNotFoundError(
                    f"Product {product_id} not found.", [{"product_id": product_id}]
                )
            values = {name: getattr(changes, name) for name in changes.model_fields_set}
            new_sku = values.get("sku")
            if new_sku is not None and self.sku_index.get(new_sku, product_id) != product_id:
                raise _duplicate_sku(new_sku)
            updated = current.model_copy(update={**values, "updated_at": self.clock()})
            if updated.sku != current.sku:
                del self.sku_index[current.sku]
                self.sku_index[updated.sku] = product_id
            self.products[product_id] = updated
            return updated

    def adjust_stock(self, product_id: int, delta: int) -> Product:
        with self.lock:
            current = self.products.get(product_id)
            if current is None:
                raise ProductNotFoundError(
                    f"Product {product_id} not found.", [{"product_id": product_id}]
                )
            quantity = apply_stock_delta(current.quantity_in_stock, delta, product_id=product_id)
            updated = current.model_copy(
                update={"quantity_in_stock": quantity, "updated_at": self.clock()}
            )
            self.products[product_id] = updated
            return updated

    # ------------------------------------------------------------------ orders
    @contextmanager
    def unit_of_work(self) -> Iterator[UnitOfWork]:
        with self.lock:
            uow = _MemoryUnitOfWork(self)
            yield uow
            # Reached only when the body raised nothing: apply the staged writes.
            now = self.clock()
            for product_id, quantity in uow.stock.items():
                self.products[product_id] = self.products[product_id].model_copy(
                    update={"quantity_in_stock": quantity, "updated_at": now}
                )
            for order in uow.orders:
                self.orders[order.id] = order
                self.next_order_id = order.id + 1

    def get_order(self, order_id: int) -> Order | None:
        with self.lock:
            return self.orders.get(order_id)

    def list_orders(self, *, limit: int | None = None, offset: int = 0) -> tuple[list[Order], int]:
        with self.lock:
            ordered = sorted(self.orders.values(), key=lambda item: item.id, reverse=True)
        end = None if limit is None else offset + limit
        return ordered[offset:end], len(ordered)
