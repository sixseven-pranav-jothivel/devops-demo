"""Request bodies, list envelopes and repository write models."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.models.domain import Money, Order, Product


def _normalise_sku(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


Sku = Annotated[
    str,
    BeforeValidator(_normalise_sku),
    Field(pattern=r"^[A-Z0-9][A-Z0-9_-]{1,31}$"),
]
ProductName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
ProductDescription = Annotated[str, StringConstraints(max_length=1000)]
ProductCategory = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)
]


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: Sku
    name: ProductName
    description: ProductDescription = ""
    price: Money
    quantity_in_stock: int = Field(default=0, ge=0)
    category: ProductCategory = "General"
    active: bool = True


class ProductUpdate(BaseModel):
    """Partial update. `quantity_in_stock` is deliberately absent: use the stock endpoint."""

    model_config = ConfigDict(extra="forbid")

    sku: Sku | None = None
    name: ProductName | None = None
    description: ProductDescription | None = None
    price: Money | None = None
    category: ProductCategory | None = None
    active: bool | None = None

    @model_validator(mode="after")
    def _require_changes(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        nulls = sorted(f for f in self.model_fields_set if getattr(self, f) is None)
        if nulls:
            raise ValueError(f"Fields cannot be null: {', '.join(nulls)}")
        return self


class StockAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delta: Annotated[int, Field(ge=-1_000_000, le=1_000_000)]
    reason: Annotated[str, StringConstraints(max_length=200)] | None = None

    @model_validator(mode="after")
    def _non_zero(self) -> Self:
        if self.delta == 0:
            raise ValueError("delta must be non-zero")
        return self


class OrderItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=10000)


class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_items: list[OrderItemCreate] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _unique_products(self) -> Self:
        ids = [item.product_id for item in self.line_items]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate product_id in line_items")
        return self


class ProductList(BaseModel):
    items: list[Product]
    total: int
    limit: int
    offset: int


class OrderList(BaseModel):
    items: list[Order]
    total: int
    limit: int
    offset: int


class NewOrderItem(BaseModel):
    """A priced order line handed to the repository (snapshot of product data)."""

    model_config = ConfigDict(frozen=True)

    product_id: int
    sku: str
    product_name: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal


class NewOrder(BaseModel):
    """A fully priced order handed to the repository; id and created_at are assigned there."""

    model_config = ConfigDict(frozen=True)

    line_items: list[NewOrderItem]
    subtotal: Decimal
    discount: Decimal
    tax: Decimal
    total: Decimal
