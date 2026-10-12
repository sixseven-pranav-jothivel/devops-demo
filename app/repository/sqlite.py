"""SQLite repository (SQLAlchemy 2). Money is stored as integer cents."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from threading import RLock
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    create_engine,
    event,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)
from sqlalchemy.pool import StaticPool

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

CENTS = Decimal(100)


def _to_cents(value: Decimal) -> int:
    return int((value * CENTS).to_integral_value())


def _from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / CENTS).quantize(Decimal("0.01"))


def _to_naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _to_aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class ProductRow(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price_cents >= 0", name="ck_products_price_non_negative"),
        CheckConstraint("quantity_in_stock >= 0", name="ck_products_stock_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_in_stock: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    subtotal_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    discount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    tax_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    items: Mapped[list[OrderItemRow]] = relationship(
        order_by="OrderItemRow.position", lazy="selectin", cascade="all, delete-orphan"
    )


class OrderItemRow(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    sku: Mapped[str] = mapped_column(String(32), nullable=False)
    product_name: Mapped[str] = mapped_column(String(120), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    line_total_cents: Mapped[int] = mapped_column(Integer, nullable=False)


def _product(row: ProductRow) -> Product:
    return Product(
        id=row.id,
        sku=row.sku,
        name=row.name,
        description=row.description,
        price=_from_cents(row.price_cents),
        quantity_in_stock=row.quantity_in_stock,
        category=row.category,
        active=row.active,
        created_at=_to_aware_utc(row.created_at),
        updated_at=_to_aware_utc(row.updated_at),
    )


def _order(row: OrderRow) -> Order:
    return Order(
        id=row.id,
        status="confirmed",
        line_items=[
            OrderItem(
                product_id=item.product_id,
                sku=item.sku,
                product_name=item.product_name,
                quantity=item.quantity,
                unit_price=_from_cents(item.unit_price_cents),
                line_total=_from_cents(item.line_total_cents),
            )
            for item in row.items
        ],
        subtotal=_from_cents(row.subtotal_cents),
        discount=_from_cents(row.discount_cents),
        tax=_from_cents(row.tax_cents),
        total=_from_cents(row.total_cents),
        created_at=_to_aware_utc(row.created_at),
    )


def _duplicate_sku(sku: str) -> DuplicateSkuError:
    return DuplicateSkuError(f"SKU '{sku}' already exists.", [{"sku": sku}])


def _not_found(product_id: int) -> ProductNotFoundError:
    return ProductNotFoundError(f"Product {product_id} not found.", [{"product_id": product_id}])


def _is_unique_violation(exc: IntegrityError) -> bool:
    return "UNIQUE" in str(exc.orig).upper()


class _SqliteUnitOfWork:
    def __init__(self, session: Session, clock: Callable[[], datetime]) -> None:
        self._session = session
        self._clock = clock

    def get_product(self, product_id: int) -> Product | None:
        row = self._session.get(ProductRow, product_id)
        return None if row is None else _product(row)

    def decrement_stock(self, product_id: int, quantity: int) -> None:
        result = self._session.execute(
            update(ProductRow)
            .where(ProductRow.id == product_id, ProductRow.quantity_in_stock >= quantity)
            .values(
                quantity_in_stock=ProductRow.quantity_in_stock - quantity,
                updated_at=_to_naive_utc(self._clock()),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            self._session.expire_all()
            row = self._session.get(ProductRow, product_id)
            if row is None:
                raise _not_found(product_id)
            raise InsufficientStockError(
                f"Insufficient stock for '{row.sku}'.",
                [
                    {
                        "product_id": product_id,
                        "sku": row.sku,
                        "requested": quantity,
                        "available": row.quantity_in_stock,
                    }
                ],
            )
        self._session.expire_all()

    def add_order(self, order: NewOrder) -> Order:
        row = OrderRow(
            status="confirmed",
            subtotal_cents=_to_cents(order.subtotal),
            discount_cents=_to_cents(order.discount),
            tax_cents=_to_cents(order.tax),
            total_cents=_to_cents(order.total),
            created_at=_to_naive_utc(self._clock()),
            items=[
                OrderItemRow(
                    position=index,
                    product_id=item.product_id,
                    sku=item.sku,
                    product_name=item.product_name,
                    quantity=item.quantity,
                    unit_price_cents=_to_cents(item.unit_price),
                    line_total_cents=_to_cents(item.line_total),
                )
                for index, item in enumerate(order.line_items)
            ],
        )
        self._session.add(row)
        self._session.flush()
        return _order(row)


class SqliteRepository:
    def __init__(
        self,
        database_url: str,
        clock: Callable[[], datetime] = utc_now,
        seed: bool = True,
    ) -> None:
        self._clock = clock
        self._lock = RLock()
        self._engine = self._build_engine(database_url)
        Base.metadata.create_all(self._engine)
        if seed:
            with self._lock, Session(self._engine) as session:
                empty = session.scalar(select(func.count()).select_from(ProductRow)) == 0
            if empty:
                for data in SEED_PRODUCTS:
                    self.create_product(data)

    @staticmethod
    def _build_engine(database_url: str) -> Engine:
        url = make_url(database_url)
        kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
        if url.database in (None, "", ":memory:"):
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, **kwargs)

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_connection: Any, _record: Any) -> None:
            # Take over transaction control so "begin" can emit BEGIN IMMEDIATE for writers.
            dbapi_connection.isolation_level = None
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

        @event.listens_for(engine, "begin")
        def _on_begin(conn: Connection) -> None:
            if conn.get_execution_options().get("immediate"):
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                conn.exec_driver_sql("BEGIN")

        return engine

    @property
    def backend_name(self) -> str:
        return "sqlite"

    @property
    def persistent(self) -> bool:
        return True

    def ping(self) -> None:
        with self._lock, self._engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    @contextmanager
    def _read(self) -> Iterator[Session]:
        with self._lock, Session(self._engine) as session:
            yield session

    @contextmanager
    def _write(self) -> Iterator[Session]:
        """Serialised write transaction (BEGIN IMMEDIATE); rolls back on any exception."""
        with self._lock:
            conn = self._engine.connect().execution_options(immediate=True)
            try:
                with Session(bind=conn, expire_on_commit=False) as session:
                    try:
                        yield session
                        session.commit()
                    except BaseException:
                        session.rollback()
                        raise
            finally:
                conn.close()

    # ------------------------------------------------------------------ products
    def create_product(self, data: ProductCreate) -> Product:
        now = _to_naive_utc(self._clock())
        try:
            with self._write() as session:
                row = ProductRow(
                    sku=data.sku,
                    name=data.name,
                    description=data.description,
                    price_cents=_to_cents(data.price),
                    quantity_in_stock=data.quantity_in_stock,
                    category=data.category,
                    active=data.active,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
                return _product(row)
        except IntegrityError as exc:
            if _is_unique_violation(exc):
                raise _duplicate_sku(data.sku) from exc
            raise

    def get_product(self, product_id: int) -> Product | None:
        with self._read() as session:
            row = session.get(ProductRow, product_id)
            return None if row is None else _product(row)

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
        conditions = []
        needle = q.strip().lower() if q else ""
        if needle:
            conditions.append(
                or_(
                    func.lower(ProductRow.name).contains(needle, autoescape=True),
                    func.lower(ProductRow.sku).contains(needle, autoescape=True),
                )
            )
        if category:
            conditions.append(func.lower(ProductRow.category) == category.lower())
        if active is not None:
            conditions.append(ProductRow.active == active)
        if low_stock_threshold is not None:
            conditions.append(ProductRow.quantity_in_stock <= low_stock_threshold)
        with self._read() as session:
            total = session.scalar(select(func.count()).select_from(ProductRow).where(*conditions))
            stmt = select(ProductRow).where(*conditions).order_by(ProductRow.id).offset(offset)
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = session.scalars(stmt).all()
            return [_product(r) for r in rows], int(total or 0)

    def update_product(self, product_id: int, changes: ProductUpdate) -> Product:
        values = {name: getattr(changes, name) for name in changes.model_fields_set}
        try:
            with self._write() as session:
                row = session.get(ProductRow, product_id)
                if row is None:
                    raise _not_found(product_id)
                for name, value in values.items():
                    if name == "price":
                        row.price_cents = _to_cents(value)
                    else:
                        setattr(row, name, value)
                row.updated_at = _to_naive_utc(self._clock())
                session.flush()
                return _product(row)
        except IntegrityError as exc:
            if _is_unique_violation(exc):
                raise _duplicate_sku(str(values.get("sku"))) from exc
            raise

    def adjust_stock(self, product_id: int, delta: int) -> Product:
        with self._write() as session:
            row = session.get(ProductRow, product_id)
            if row is None:
                raise _not_found(product_id)
            row.quantity_in_stock = apply_stock_delta(
                row.quantity_in_stock, delta, product_id=product_id
            )
            row.updated_at = _to_naive_utc(self._clock())
            session.flush()
            return _product(row)

    # ------------------------------------------------------------------ orders
    @contextmanager
    def unit_of_work(self) -> Iterator[UnitOfWork]:
        with self._write() as session:
            yield _SqliteUnitOfWork(session, self._clock)

    def get_order(self, order_id: int) -> Order | None:
        with self._read() as session:
            row = session.get(OrderRow, order_id)
            return None if row is None else _order(row)

    def list_orders(self, *, limit: int | None = None, offset: int = 0) -> tuple[list[Order], int]:
        with self._read() as session:
            total = session.scalar(select(func.count()).select_from(OrderRow))
            stmt = select(OrderRow).order_by(OrderRow.id.desc()).offset(offset)
            if limit is not None:
                stmt = stmt.limit(limit)
            return [_order(r) for r in session.scalars(stmt).all()], int(total or 0)
