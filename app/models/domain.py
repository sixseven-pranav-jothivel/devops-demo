"""Domain models. Money is `Decimal` (2 dp) and serialised as a 2-dp JSON string."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def _serialize_money(value: Decimal) -> str:
    return f"{value:.2f}"


def _serialize_datetime(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# Money: Decimal with <= 2 decimal places, >= 0 for inputs. JSON output is always a 2dp string.
Money = Annotated[
    Decimal,
    Field(max_digits=12, decimal_places=2, ge=0),
    PlainSerializer(_serialize_money, return_type=str, when_used="json"),
]

# Timestamps are timezone-aware UTC, serialised as ISO 8601 with a trailing Z.
UtcDatetime = Annotated[
    datetime,
    PlainSerializer(_serialize_datetime, return_type=str, when_used="json"),
]


class Product(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    sku: str
    name: str
    description: str
    price: Money
    quantity_in_stock: int
    category: str
    active: bool
    created_at: UtcDatetime
    updated_at: UtcDatetime


class OrderItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    product_id: int
    sku: str
    product_name: str
    quantity: int
    unit_price: Money
    line_total: Money


class Order(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    status: Literal["confirmed"]
    line_items: list[OrderItem]
    subtotal: Money
    discount: Money
    tax: Money
    total: Money
    created_at: UtcDatetime


class Metrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    product_count: int
    active_product_count: int
    low_stock_count: int
    out_of_stock_count: int
    inventory_value: Money
    order_count: int
    revenue_total: Money
    low_stock_threshold: int
