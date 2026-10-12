"""SQLite-only behaviour: persistence, schema constraints, in-memory URL, rollback on SQL errors."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.schemas import NewOrder, NewOrderItem, ProductCreate, ProductUpdate
from app.repository.sqlite import SqliteRepository
from tests.helpers import FrozenClock

pytestmark = pytest.mark.integration

D = Decimal


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "specific.db"


def open_repo(path: Path, *, seed: bool = True) -> SqliteRepository:
    return SqliteRepository(f"sqlite:///{path}", clock=FrozenClock(), seed=seed)


def test_data_survives_reopening_the_database(db_path: Path) -> None:
    first = open_repo(db_path)
    first.adjust_stock(1, -7)
    first.create_product(ProductCreate(sku="KEEP-1", name="Keep", price=D("1.00")))
    del first

    second = open_repo(db_path)
    product = second.get_product(1)
    assert product is not None
    assert product.quantity_in_stock == 493
    assert second.list_products()[1] == 11


def test_seeding_an_existing_non_empty_database_does_not_duplicate_the_catalogue(
    db_path: Path,
) -> None:
    open_repo(db_path)
    again = open_repo(db_path)
    assert again.list_products()[1] == 10


def test_unseeded_database_stays_empty(db_path: Path) -> None:
    assert open_repo(db_path, seed=False).list_products()[1] == 0


def test_each_database_file_is_isolated(tmp_path: Path) -> None:
    a = open_repo(tmp_path / "a.db")
    b = open_repo(tmp_path / "b.db")
    a.adjust_stock(1, -1)
    product = b.get_product(1)
    assert product is not None
    assert product.quantity_in_stock == 500


def test_in_memory_url_works_and_is_shared_across_calls() -> None:
    repo = SqliteRepository("sqlite:///:memory:", seed=True)
    repo.adjust_stock(1, -1)
    product = repo.get_product(1)
    assert product is not None
    assert product.quantity_in_stock == 499
    repo.ping()


def test_money_is_stored_as_integer_cents(db_path: Path) -> None:
    open_repo(db_path)
    with sqlite3.connect(db_path) as conn:
        cents, typ = conn.execute(
            "SELECT price_cents, typeof(price_cents) FROM products WHERE id = 1"
        ).fetchone()
    assert (cents, typ) == (999, "integer")


def test_database_rejects_negative_stock_and_price_and_duplicate_sku(db_path: Path) -> None:
    open_repo(db_path)
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute("UPDATE products SET quantity_in_stock = -1 WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute("UPDATE products SET price_cents = -1 WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute("UPDATE products SET sku = 'WID-002' WHERE id = 1")


def test_foreign_keys_are_enforced(db_path: Path) -> None:
    repo = open_repo(db_path)
    order = NewOrder(
        line_items=[
            NewOrderItem(
                product_id=999, sku="X", product_name="X", quantity=1,
                unit_price=D("1.00"), line_total=D("1.00"),
            )
        ],
        subtotal=D("1.00"), discount=D("0.00"), tax=D("0.08"), total=D("1.08"),
    )  # fmt: skip

    def store_order() -> None:
        with repo.unit_of_work() as uow:
            uow.add_order(order)

    with pytest.raises(IntegrityError):
        store_order()
    assert repo.list_orders() == ([], 0)


def test_a_failed_write_leaves_no_open_transaction(db_path: Path) -> None:
    """After a failed unit another connection can still write (the write lock was released)."""
    repo = open_repo(db_path)

    def failing_unit() -> None:
        with repo.unit_of_work() as uow:
            uow.decrement_stock(1, 1)
            raise RuntimeError("fail")

    with pytest.raises(RuntimeError, match="fail"):
        failing_unit()
    with sqlite3.connect(db_path, timeout=1) as conn:
        conn.execute("UPDATE products SET quantity_in_stock = quantity_in_stock + 1 WHERE id = 2")
    product = repo.get_product(2)
    assert product is not None
    assert product.quantity_in_stock == 201


def test_a_non_sku_integrity_error_is_not_reported_as_a_duplicate_sku(db_path: Path) -> None:
    """Bypass schema validation to hit the CHECK constraint: it must surface as itself."""
    repo = open_repo(db_path, seed=False)
    negative = ProductCreate.model_construct(
        sku="NEG-1",
        name="Negative",
        description="",
        price=D("-1.00"),
        quantity_in_stock=0,
        category="General",
        active=True,
    )
    with pytest.raises(IntegrityError, match="CHECK"):
        repo.create_product(negative)
    assert repo.list_products() == ([], 0)

    created = repo.create_product(ProductCreate(sku="OK-1", name="Fine", price=D("1.00")))
    bad_update = ProductUpdate.model_construct(_fields_set={"price"}, price=D("-5.00"))
    with pytest.raises(IntegrityError, match="CHECK"):
        repo.update_product(created.id, bad_update)
    unchanged = repo.get_product(created.id)
    assert unchanged is not None
    assert unchanged.price == D("1.00")
