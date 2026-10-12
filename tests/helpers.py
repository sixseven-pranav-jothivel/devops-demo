"""Shared test helpers (plain functions and constants; fixtures live in conftest.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.models.domain import Product

T0 = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)

# Seed catalogue facts (docs/ARCHITECTURE.md section 6.4).
SEED_COUNT = 10
SEED_ACTIVE_COUNT = 9
SEED_INVENTORY_VALUE = "30473.00"
SEED_LOW_STOCK_IDS = [6, 8]  # stock <= 5 (HDMI cable: 3, drill: 0)
WID_001 = 1  # Standard Widget, 9.99, stock 500
GAD_001 = 3  # Smart Gadget, 129.99, stock 40
GAD_002 = 4  # Gadget Pro, 349.00, stock 15
CAB_002 = 6  # HDMI Cable, 12.80, stock 3 (low)
TOL_002 = 8  # Cordless Drill, 89.00, stock 0
OLD_001 = 10  # Discontinued Gizmo, inactive, stock 25

TAX = Decimal("0.08")


class FrozenClock:
    """Deterministic, advanceable clock for repositories."""

    def __init__(self, start: datetime = T0) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int = 60) -> datetime:
        self.current += timedelta(seconds=seconds)
        return self.current


def make_product(**overrides: Any) -> Product:
    """A valid in-memory Product for pure-function tests."""
    values: dict[str, Any] = {
        "id": 1,
        "sku": "TST-001",
        "name": "Test Product",
        "description": "",
        "price": Decimal("10.00"),
        "quantity_in_stock": 10,
        "category": "Testing",
        "active": True,
        "created_at": T0,
        "updated_at": T0,
    }
    values.update(overrides)
    return Product(**values)


def product_payload(**overrides: Any) -> dict[str, Any]:
    """A valid POST /api/products body."""
    body: dict[str, Any] = {
        "sku": "NEW-001",
        "name": "New Product",
        "price": "4.50",
        "quantity_in_stock": 20,
    }
    body.update(overrides)
    return body


def order_payload(*lines: tuple[int, int]) -> dict[str, Any]:
    return {"line_items": [{"product_id": pid, "quantity": qty} for pid, qty in lines]}


def assert_error(
    response: httpx.Response, status: int, code: str, *, details: list[Any] | None = None
) -> dict[str, Any]:
    """Assert the standard error body (docs/ARCHITECTURE.md 5.1) and return `error`."""
    assert response.status_code == status, response.text
    body = response.json()
    assert list(body) == ["error"]
    error = body["error"]
    assert set(error) == {"code", "message", "details"}
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    assert isinstance(error["details"], list)
    if details is not None:
        assert error["details"] == details
    return error  # type: ignore[no-any-return]
