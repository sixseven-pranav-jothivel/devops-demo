"""Order pricing. Pure functions, Decimal only, every rounding is ROUND_HALF_UP to cents."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT: Decimal = Decimal("0.01")

# (subtotal threshold inclusive, rate), highest threshold first.
DISCOUNT_TIERS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("500.00"), Decimal("0.10")),
    (Decimal("100.00"), Decimal("0.05")),
)


@dataclass(frozen=True)
class OrderTotals:
    subtotal: Decimal
    discount: Decimal
    tax: Decimal
    total: Decimal


def quantize_money(value: Decimal) -> Decimal:
    """Round to cents, half up."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def calculate_line_total(unit_price: Decimal, quantity: int) -> Decimal:
    return quantize_money(unit_price * quantity)


def calculate_subtotal(line_totals: Iterable[Decimal]) -> Decimal:
    """Exact sum of the line totals."""
    return sum(line_totals, Decimal("0.00"))


def discount_rate_for(subtotal: Decimal) -> Decimal:
    """Rate of the first (highest) tier whose threshold is <= subtotal, else zero."""
    for threshold, rate in DISCOUNT_TIERS:
        if subtotal >= threshold:
            return rate
    return Decimal("0")


def calculate_discount(subtotal: Decimal) -> Decimal:
    return quantize_money(subtotal * discount_rate_for(subtotal))


def calculate_tax(taxable_amount: Decimal, tax_rate: Decimal) -> Decimal:
    return quantize_money(taxable_amount * tax_rate)


def calculate_total(subtotal: Decimal, discount: Decimal, tax: Decimal) -> Decimal:
    return subtotal - discount + tax


def price_order(line_totals: Iterable[Decimal], tax_rate: Decimal) -> OrderTotals:
    """Subtotal, tiered discount, tax on the discounted subtotal, and the grand total."""
    subtotal = calculate_subtotal(line_totals)
    discount = calculate_discount(subtotal)
    tax = calculate_tax(subtotal - discount, tax_rate)
    total = calculate_total(subtotal, discount, tax)
    return OrderTotals(subtotal=subtotal, discount=discount, tax=tax, total=total)
