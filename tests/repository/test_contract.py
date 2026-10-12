"""Repository contract: one suite, run against the in-memory AND the SQLite implementation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC
from decimal import Decimal

import pytest

from app.models.errors import (
    DuplicateSkuError,
    InsufficientStockError,
    NegativeStockError,
    ProductNotFoundError,
)
from app.models.schemas import NewOrder, NewOrderItem, ProductCreate, ProductUpdate
from app.repository.base import Repository, UnitOfWork
from tests.helpers import SEED_COUNT, FrozenClock

pytestmark = pytest.mark.integration

D = Decimal


def new_product(sku: str = "NEW-1", **kwargs: object) -> ProductCreate:
    values: dict[str, object] = {"sku": sku, "name": f"Product {sku}", "price": D("5.00")}
    values.update(kwargs)
    return ProductCreate.model_validate(values)


def new_order(product_id: int = 1, quantity: int = 1, price: str = "10.00") -> NewOrder:
    line_total = D(price) * quantity
    return NewOrder(
        line_items=[
            NewOrderItem(
                product_id=product_id,
                sku="SKU",
                product_name="Name",
                quantity=quantity,
                unit_price=D(price),
                line_total=line_total,
            )
        ],
        subtotal=line_total,
        discount=D("0.00"),
        tax=D("0.80"),
        total=line_total + D("0.80"),
    )


def run_unit(repo: Repository, body: Callable[[UnitOfWork], None]) -> None:
    """Run `body` inside one unit of work (exceptions propagate after the rollback)."""
    with repo.unit_of_work() as uow:
        body(uow)


def stock(repo: Repository, product_id: int) -> int:
    product = repo.get_product(product_id)
    assert product is not None
    return product.quantity_in_stock


# ------------------------------------------------------------------ identity and seeding
def test_backend_metadata(repo: Repository, backend: str) -> None:
    assert repo.backend_name == backend
    assert repo.persistent is (backend == "sqlite")


def test_ping_succeeds(repo: Repository) -> None:
    repo.ping()


def test_seeded_repository_has_the_documented_catalogue(repo: Repository) -> None:
    products, total = repo.list_products()
    assert total == SEED_COUNT
    assert [p.id for p in products] == list(range(1, 11))
    assert [p.sku for p in products] == [
        "WID-001", "WID-002", "GAD-001", "GAD-002", "CAB-001",
        "CAB-002", "TOL-001", "TOL-002", "PKG-001", "OLD-001",
    ]  # fmt: skip
    by_id = {p.id: p for p in products}
    assert by_id[1].price == D("9.99")
    assert by_id[1].quantity_in_stock == 500
    assert by_id[8].quantity_in_stock == 0
    assert by_id[10].active is False
    assert sum(1 for p in products if p.active) == 9


def test_blank_repository_is_empty(blank_repo: Repository) -> None:
    assert blank_repo.list_products() == ([], 0)
    assert blank_repo.list_orders() == ([], 0)


# ------------------------------------------------------------------ create / get
def test_create_assigns_sequential_ids_starting_at_one(blank_repo: Repository) -> None:
    first = blank_repo.create_product(new_product("A-1"))
    second = blank_repo.create_product(new_product("A-2"))
    assert (first.id, second.id) == (1, 2)


def test_create_returns_what_get_returns(blank_repo: Repository) -> None:
    created = blank_repo.create_product(
        new_product("A-1", description="d", category="Cat", quantity_in_stock=4, active=False)
    )
    assert blank_repo.get_product(created.id) == created
    assert created.description == "d"
    assert created.category == "Cat"
    assert created.quantity_in_stock == 4
    assert created.active is False


def test_create_stamps_timestamps_from_the_clock_as_aware_utc(
    blank_repo: Repository, clock: FrozenClock
) -> None:
    created = blank_repo.create_product(new_product())
    assert created.created_at == clock.current
    assert created.updated_at == clock.current
    assert created.created_at.tzinfo is not None
    assert created.created_at.utcoffset() == UTC.utcoffset(None)


def test_get_unknown_product_returns_none(repo: Repository) -> None:
    assert repo.get_product(999) is None
    assert repo.get_product(0) is None


@pytest.mark.parametrize("price", ["0.00", "0.01", "9.99", "129.99", "9999999999.99"])
def test_money_round_trips_exactly(blank_repo: Repository, price: str) -> None:
    created = blank_repo.create_product(new_product(price=D(price)))
    fetched = blank_repo.get_product(created.id)
    assert fetched is not None
    assert fetched.price == D(price)
    assert str(fetched.price) == price


# ------------------------------------------------------------------ duplicate SKU
def test_duplicate_sku_on_create_is_rejected(repo: Repository) -> None:
    with pytest.raises(DuplicateSkuError) as raised:
        repo.create_product(new_product("WID-001"))
    assert raised.value.details == [{"sku": "WID-001"}]
    assert repo.list_products()[1] == SEED_COUNT


@pytest.mark.parametrize("variant", ["wid-001", "Wid-001", "  WID-001  ", " wid-001"])
def test_duplicate_sku_is_detected_after_normalisation(repo: Repository, variant: str) -> None:
    with pytest.raises(DuplicateSkuError):
        repo.create_product(new_product(variant))


def test_a_rejected_duplicate_does_not_consume_an_id(repo: Repository) -> None:
    with pytest.raises(DuplicateSkuError):
        repo.create_product(new_product("WID-001"))
    assert repo.create_product(new_product("FRESH-1")).id == SEED_COUNT + 1


def test_patching_to_another_products_sku_is_rejected(repo: Repository) -> None:
    with pytest.raises(DuplicateSkuError) as raised:
        repo.update_product(1, ProductUpdate(sku=" wid-002 "))
    assert raised.value.details == [{"sku": "WID-002"}]
    product = repo.get_product(1)
    assert product is not None
    assert product.sku == "WID-001"


def test_patching_a_product_to_its_own_sku_is_allowed(repo: Repository) -> None:
    updated = repo.update_product(1, ProductUpdate(sku="wid-001", name="Same sku"))
    assert updated.sku == "WID-001"
    assert updated.name == "Same sku"


def test_changing_a_sku_frees_the_old_one(repo: Repository) -> None:
    repo.update_product(1, ProductUpdate(sku="WID-RENAMED"))
    reused = repo.create_product(new_product("WID-001"))
    assert reused.id == SEED_COUNT + 1
    with pytest.raises(DuplicateSkuError):
        repo.create_product(new_product("WID-RENAMED"))


# ------------------------------------------------------------------ update
def test_update_changes_only_the_provided_fields(repo: Repository, clock: FrozenClock) -> None:
    before = repo.get_product(2)
    assert before is not None
    clock.advance()
    after = repo.update_product(2, ProductUpdate(price=D("26.00"), active=False))
    assert after.price == D("26.00")
    assert after.active is False
    assert (after.sku, after.name, after.description, after.category) == (
        before.sku,
        before.name,
        before.description,
        before.category,
    )
    assert after.quantity_in_stock == before.quantity_in_stock
    assert after.created_at == before.created_at
    assert after.updated_at == clock.current
    assert after.updated_at > before.updated_at
    assert repo.get_product(2) == after


def test_update_can_blank_the_description(repo: Repository) -> None:
    assert repo.update_product(1, ProductUpdate(description="")).description == ""


def test_update_unknown_product(repo: Repository) -> None:
    with pytest.raises(ProductNotFoundError) as raised:
        repo.update_product(999, ProductUpdate(name="x"))
    assert raised.value.details == [{"product_id": 999}]


# ------------------------------------------------------------------ adjust_stock
def test_adjust_stock_up_down_and_to_zero(repo: Repository, clock: FrozenClock) -> None:
    clock.advance()
    assert repo.adjust_stock(6, 10).quantity_in_stock == 13
    assert repo.adjust_stock(6, -13).quantity_in_stock == 0
    product = repo.get_product(6)
    assert product is not None
    assert product.quantity_in_stock == 0
    assert product.updated_at == clock.current


def test_adjust_stock_below_zero_is_rejected_and_leaves_stock_unchanged(repo: Repository) -> None:
    with pytest.raises(NegativeStockError) as raised:
        repo.adjust_stock(6, -4)
    assert raised.value.details == [{"product_id": 6, "current": 3, "delta": -4}]
    assert stock(repo, 6) == 3


def test_adjust_stock_unknown_product(repo: Repository) -> None:
    with pytest.raises(ProductNotFoundError):
        repo.adjust_stock(999, 1)


# ------------------------------------------------------------------ listing
def test_list_orders_products_by_id_ascending(repo: Repository) -> None:
    assert [p.id for p in repo.list_products()[0]] == sorted(p.id for p in repo.list_products()[0])


@pytest.mark.parametrize(
    ("kwargs", "expected_ids"),
    [
        ({"q": "widget"}, [1, 2]),
        ({"q": "WIDGET"}, [1, 2]),
        ({"q": "  widget  "}, [1, 2]),
        ({"q": "cab-0"}, [5, 6]),  # matches on SKU too
        ({"q": "hdmi"}, [6]),
        ({"q": "nothing-like-this"}, []),
        ({"q": "%"}, []),  # LIKE wildcards are literal
        ({"q": "_"}, []),
        ({"category": "Widgets"}, [1, 2]),
        ({"category": "widgets"}, [1, 2]),
        ({"category": "GADGETS"}, [3, 4, 10]),
        ({"category": "Wid"}, []),  # exact, not substring
        ({"active": True}, [1, 2, 3, 4, 5, 6, 7, 8, 9]),
        ({"active": False}, [10]),
        ({"low_stock_threshold": 5}, [6, 8]),
        ({"low_stock_threshold": 3}, [6, 8]),
        ({"low_stock_threshold": 2}, [8]),
        ({"low_stock_threshold": 0}, [8]),
        ({"q": "gadget", "active": True}, [3, 4]),
        ({"category": "gadgets", "low_stock_threshold": 100}, [3, 4, 10]),
    ],
)
def test_list_filters(repo: Repository, kwargs: dict[str, object], expected_ids: list[int]) -> None:
    products, total = repo.list_products(**kwargs)  # type: ignore[arg-type]
    assert [p.id for p in products] == expected_ids
    assert total == len(expected_ids)


def test_list_pagination_and_total(repo: Repository) -> None:
    products, total = repo.list_products(limit=3, offset=0)
    assert ([p.id for p in products], total) == ([1, 2, 3], 10)
    products, total = repo.list_products(limit=3, offset=9)
    assert ([p.id for p in products], total) == ([10], 10)
    assert repo.list_products(limit=3, offset=10) == ([], 10)
    assert repo.list_products(limit=3, offset=99) == ([], 10)
    products, _ = repo.list_products(offset=8)  # no limit
    assert [p.id for p in products] == [9, 10]


def test_total_is_counted_after_filters_and_before_pagination(repo: Repository) -> None:
    products, total = repo.list_products(category="gadgets", limit=1, offset=1)
    assert [p.id for p in products] == [4]
    assert total == 3


# ------------------------------------------------------------------ unit of work
def test_unit_of_work_commits_stock_and_order_together(repo: Repository) -> None:
    with repo.unit_of_work() as uow:
        uow.decrement_stock(1, 5)
        stored = uow.add_order(new_order(1, 5, "9.99"))
    assert stored.id == 1
    assert stock(repo, 1) == 495
    assert repo.get_order(1) == stored
    assert repo.list_orders()[1] == 1


def test_add_order_assigns_id_status_and_clock_timestamp(
    repo: Repository, clock: FrozenClock
) -> None:
    with repo.unit_of_work() as uow:
        stored = uow.add_order(new_order())
    assert stored.id == 1
    assert stored.status == "confirmed"
    assert stored.created_at == clock.current
    assert stored.created_at.tzinfo is not None


def test_unit_of_work_reads_see_its_own_staged_writes(repo: Repository) -> None:
    with repo.unit_of_work() as uow:
        uow.decrement_stock(6, 2)
        inside = uow.get_product(6)
        assert inside is not None
        assert inside.quantity_in_stock == 1
    assert stock(repo, 6) == 1


def test_unit_of_work_get_unknown_product(repo: Repository) -> None:
    with repo.unit_of_work() as uow:
        assert uow.get_product(999) is None


def test_decrement_exact_stock_is_allowed_then_one_more_is_not(repo: Repository) -> None:
    with repo.unit_of_work() as uow:
        uow.decrement_stock(6, 3)
        with pytest.raises(InsufficientStockError) as raised:
            uow.decrement_stock(6, 1)
    assert raised.value.details == [
        {"product_id": 6, "sku": "CAB-002", "requested": 1, "available": 0}
    ]
    # The error was caught inside the block, so the first decrement commits.
    assert stock(repo, 6) == 0


def test_decrement_more_than_available_raises_and_nothing_changes(repo: Repository) -> None:
    with pytest.raises(InsufficientStockError), repo.unit_of_work() as uow:
        uow.decrement_stock(6, 4)
    assert stock(repo, 6) == 3


def test_decrement_unknown_product_raises_not_found(repo: Repository) -> None:
    with pytest.raises(ProductNotFoundError), repo.unit_of_work() as uow:
        uow.decrement_stock(999, 1)


def test_exception_in_the_body_rolls_everything_back(repo: Repository) -> None:
    def body(uow: UnitOfWork) -> None:
        uow.decrement_stock(1, 10)
        uow.decrement_stock(2, 20)
        uow.add_order(new_order(1, 10))
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_unit(repo, body)
    assert stock(repo, 1) == 500
    assert stock(repo, 2) == 200
    assert repo.list_orders() == ([], 0)
    assert repo.get_order(1) is None


def test_failure_on_a_later_line_rolls_back_the_earlier_decrement(repo: Repository) -> None:
    def body(uow: UnitOfWork) -> None:
        uow.decrement_stock(1, 10)  # succeeds (staged / executed)
        uow.decrement_stock(6, 99)  # fails mid-unit

    with pytest.raises(InsufficientStockError):
        run_unit(repo, body)
    assert stock(repo, 1) == 500
    assert stock(repo, 6) == 3


def test_rolled_back_order_does_not_consume_an_order_id(repo: Repository) -> None:
    def body(uow: UnitOfWork) -> None:
        uow.add_order(new_order())
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError, match="abort"):
        run_unit(repo, body)
    with repo.unit_of_work() as uow:
        assert uow.add_order(new_order()).id == 1


def test_the_repository_is_usable_after_a_failed_unit(repo: Repository) -> None:
    def body(uow: UnitOfWork) -> None:
        uow.decrement_stock(1, 1)
        raise RuntimeError("x")

    with pytest.raises(RuntimeError, match="x"):
        run_unit(repo, body)
    assert repo.adjust_stock(1, 1).quantity_in_stock == 501
    with repo.unit_of_work() as uow:
        uow.decrement_stock(1, 1)
    assert stock(repo, 1) == 500


def test_a_unit_without_writes_changes_nothing(repo: Repository) -> None:
    before = repo.list_products()
    with repo.unit_of_work() as uow:
        uow.get_product(1)
    assert repo.list_products() == before


# ------------------------------------------------------------------ orders
def test_order_round_trips_with_exact_money_and_line_order(repo: Repository) -> None:
    order = NewOrder(
        line_items=[
            NewOrderItem(
                product_id=3, sku="GAD-001", product_name="Smart Gadget", quantity=1,
                unit_price=D("129.99"), line_total=D("129.99"),
            ),
            NewOrderItem(
                product_id=1, sku="WID-001", product_name="Standard Widget", quantity=3,
                unit_price=D("9.99"), line_total=D("29.97"),
            ),
        ],
        subtotal=D("159.96"), discount=D("8.00"), tax=D("12.16"), total=D("164.12"),
    )  # fmt: skip
    with repo.unit_of_work() as uow:
        stored = uow.add_order(order)
    fetched = repo.get_order(stored.id)
    assert fetched == stored
    assert fetched is not None
    assert [i.product_id for i in fetched.line_items] == [3, 1]
    assert [str(i.unit_price) for i in fetched.line_items] == ["129.99", "9.99"]
    assert (fetched.subtotal, fetched.discount, fetched.tax, fetched.total) == (
        D("159.96"), D("8.00"), D("12.16"), D("164.12"),
    )  # fmt: skip
    assert fetched.line_items[1].product_name == "Standard Widget"


def test_get_unknown_order_returns_none(repo: Repository) -> None:
    assert repo.get_order(1) is None


def test_orders_list_newest_first_with_pagination(repo: Repository) -> None:
    for _ in range(5):
        with repo.unit_of_work() as uow:
            uow.add_order(new_order())
    orders, total = repo.list_orders()
    assert [o.id for o in orders] == [5, 4, 3, 2, 1]
    assert total == 5
    orders, total = repo.list_orders(limit=2, offset=1)
    assert [o.id for o in orders] == [4, 3]
    assert total == 5
    assert repo.list_orders(limit=2, offset=5) == ([], 5)


def test_order_snapshot_survives_later_product_edits(repo: Repository) -> None:
    with repo.unit_of_work() as uow:
        product = uow.get_product(1)
        assert product is not None
        stored = uow.add_order(
            NewOrder(
                line_items=[
                    NewOrderItem(
                        product_id=1, sku=product.sku, product_name=product.name, quantity=2,
                        unit_price=product.price, line_total=product.price * 2,
                    )
                ],
                subtotal=D("19.98"), discount=D("0.00"), tax=D("1.60"), total=D("21.58"),
            )
        )  # fmt: skip
    repo.update_product(1, ProductUpdate(price=D("99.00"), name="Renamed", sku="WID-NEW"))
    fetched = repo.get_order(stored.id)
    assert fetched is not None
    item = fetched.line_items[0]
    assert (item.unit_price, item.product_name, item.sku) == (
        D("9.99"),
        "Standard Widget",
        "WID-001",
    )
    assert item.line_total == D("19.98")
