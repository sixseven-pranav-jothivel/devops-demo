"""Domain errors. The API layer maps each to the standard error body."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for expected, client-facing business errors."""

    code: str = "domain_error"
    status_code: int = 400

    def __init__(self, message: str, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: list[dict[str, Any]] = details if details is not None else []


class ProductNotFoundError(DomainError):
    code = "product_not_found"
    status_code = 404


class OrderNotFoundError(DomainError):
    code = "order_not_found"
    status_code = 404


class DuplicateSkuError(DomainError):
    code = "duplicate_sku"
    status_code = 409


class ProductInactiveError(DomainError):
    code = "product_inactive"
    status_code = 409


class InsufficientStockError(DomainError):
    code = "insufficient_stock"
    status_code = 409


class NegativeStockError(DomainError):
    code = "negative_stock"
    status_code = 409
