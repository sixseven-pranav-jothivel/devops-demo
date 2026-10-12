"""Concurrency: no oversell, no lost updates. Barrier-synchronised, no sleeps, count asserts."""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.errors import DuplicateSkuError, InsufficientStockError, NegativeStockError
from app.models.schemas import OrderCreate, ProductCreate, StockAdjustment
from app.repository.base import Repository
from app.repository.memory import InMemoryRepository
from app.repository.sqlite import SqliteRepository
from app.services import orders as order_service
from app.services import products as product_service
from tests.helpers import TAX

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

D = Decimal
BARRIER_TIMEOUT = 30  # safety net only: a broken barrier fails the test instead of hanging it


class WidenRaceWindowClock:
    """A clock that yields the CPU each time it is read.

    Repositories read the clock in the middle of a read-modify-write, so this makes an unsafe
    implementation interleave on almost every run. It never changes what a *correct* implementation
    returns: assertions stay purely count-based.
    """

    def __call__(self) -> datetime:
        time.sleep(0.001)
        return datetime.now(UTC)


@pytest.fixture
def racy_repo(backend: str, tmp_path: Path) -> Repository:
    clock = WidenRaceWindowClock()
    if backend == "memory":
        return InMemoryRepository(seed=False, clock=clock)
    return SqliteRepository(f"sqlite:///{tmp_path / 'racy.db'}", clock=clock, seed=False)


def request(product_id: int, quantity: int) -> OrderCreate:
    return OrderCreate.model_validate(
        {"line_items": [{"product_id": product_id, "quantity": quantity}]}
    )


def run_concurrently(workers: int, job: Callable[[], str]) -> Counter[str]:
    """Run `job` in `workers` threads released together by a barrier; count the outcomes."""
    barrier = threading.Barrier(workers, timeout=BARRIER_TIMEOUT)

    def task() -> str:
        barrier.wait()
        return job()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(task) for _ in range(workers)]
        return Counter(f.result(timeout=BARRIER_TIMEOUT * 2) for f in futures)


def make_product(repo: Repository, stock: int) -> int:
    return repo.create_product(
        ProductCreate(sku="LIM-1", name="Limited", price=D("2.00"), quantity_in_stock=stock)
    ).id


def stock_of(repo: Repository, product_id: int) -> int:
    product = repo.get_product(product_id)
    assert product is not None
    return product.quantity_in_stock


def test_exactly_stock_many_single_unit_orders_succeed(blank_repo: Repository) -> None:
    stock, threads = 20, 60
    product_id = make_product(blank_repo, stock)

    def job() -> str:
        try:
            order_service.place_order(blank_repo, request(product_id, 1), tax_rate=TAX)
        except InsufficientStockError:
            return "rejected"
        return "ok"

    outcome = run_concurrently(threads, job)

    assert outcome == Counter(ok=stock, rejected=threads - stock)
    assert stock_of(blank_repo, product_id) == 0
    orders, total = blank_repo.list_orders()
    assert total == stock
    assert sorted(o.id for o in orders) == list(range(1, stock + 1))  # no gaps, no duplicates


def test_multi_unit_orders_never_oversell(blank_repo: Repository) -> None:
    """10 in stock, 25 threads each wanting 3: exactly 3 succeed and 1 unit is left."""
    product_id = make_product(blank_repo, 10)

    def job() -> str:
        try:
            order_service.place_order(blank_repo, request(product_id, 3), tax_rate=TAX)
        except InsufficientStockError:
            return "rejected"
        return "ok"

    outcome = run_concurrently(25, job)

    assert outcome == Counter(ok=3, rejected=22)
    assert stock_of(blank_repo, product_id) == 1
    assert blank_repo.list_orders()[1] == 3


def test_stock_is_never_negative_and_orders_match_sold_units(blank_repo: Repository) -> None:
    product_id = make_product(blank_repo, 7)

    def job() -> str:
        try:
            order_service.place_order(blank_repo, request(product_id, 2), tax_rate=TAX)
        except InsufficientStockError:
            return "rejected"
        return "ok"

    outcome = run_concurrently(16, job)
    sold_units = sum(
        item.quantity for order in blank_repo.list_orders()[0] for item in order.line_items
    )
    assert outcome["ok"] == 3
    assert sold_units == 6
    assert stock_of(blank_repo, product_id) == 7 - sold_units >= 0


def test_concurrent_stock_adjustments_do_not_lose_updates(blank_repo: Repository) -> None:
    stock, threads = 30, 50
    product_id = make_product(blank_repo, stock)

    def job() -> str:
        try:
            product_service.adjust_stock(blank_repo, product_id, StockAdjustment(delta=-1))
        except NegativeStockError:
            return "rejected"
        return "ok"

    outcome = run_concurrently(threads, job)

    assert outcome == Counter(ok=stock, rejected=threads - stock)
    assert stock_of(blank_repo, product_id) == 0


def test_concurrent_increments_are_all_applied(blank_repo: Repository) -> None:
    product_id = make_product(blank_repo, 0)

    def job() -> str:
        product_service.adjust_stock(blank_repo, product_id, StockAdjustment(delta=2))
        return "ok"

    run_concurrently(40, job)
    assert stock_of(blank_repo, product_id) == 80


def test_orders_and_adjustments_interleave_without_corrupting_stock(blank_repo: Repository) -> None:
    """Half the threads sell 1 unit, half restock 1: stock must equal start - sold + added."""
    product_id = make_product(blank_repo, 100)
    counter = iter(range(1000))
    lock = threading.Lock()

    def job() -> str:
        with lock:
            n = next(counter)
        if n % 2:
            product_service.adjust_stock(blank_repo, product_id, StockAdjustment(delta=1))
            return "added"
        order_service.place_order(blank_repo, request(product_id, 1), tax_rate=TAX)
        return "sold"

    outcome = run_concurrently(40, job)
    assert outcome == Counter(added=20, sold=20)
    assert stock_of(blank_repo, product_id) == 100
    assert blank_repo.list_orders()[1] == 20


def test_concurrent_duplicate_sku_creates_yield_exactly_one_product(blank_repo: Repository) -> None:
    def job() -> str:
        try:
            blank_repo.create_product(ProductCreate(sku="SAME-1", name="Same", price=D("1.00")))
        except DuplicateSkuError:
            return "duplicate"
        return "ok"

    outcome = run_concurrently(20, job)
    assert outcome == Counter(ok=1, duplicate=19)
    assert blank_repo.list_products()[1] == 1


# ---------------------------------------------------- same invariants, widened race window
def test_no_oversell_even_when_every_clock_read_invites_a_thread_switch(
    racy_repo: Repository,
) -> None:
    product_id = make_product(racy_repo, 5)

    def job() -> str:
        try:
            order_service.place_order(racy_repo, request(product_id, 1), tax_rate=TAX)
        except InsufficientStockError:
            return "rejected"
        return "ok"

    outcome = run_concurrently(20, job)

    assert outcome == Counter(ok=5, rejected=15)
    assert stock_of(racy_repo, product_id) == 0
    assert racy_repo.list_orders()[1] == 5


def test_stock_adjustments_are_atomic_even_when_every_clock_read_invites_a_thread_switch(
    racy_repo: Repository,
) -> None:
    product_id = make_product(racy_repo, 10)

    def take() -> str:
        try:
            product_service.adjust_stock(racy_repo, product_id, StockAdjustment(delta=-1))
        except NegativeStockError:
            return "rejected"
        return "ok"

    assert run_concurrently(30, take) == Counter(ok=10, rejected=20)
    assert stock_of(racy_repo, product_id) == 0

    def add() -> str:
        product_service.adjust_stock(racy_repo, product_id, StockAdjustment(delta=1))
        return "ok"

    run_concurrently(20, add)
    assert stock_of(racy_repo, product_id) == 20  # no lost update
