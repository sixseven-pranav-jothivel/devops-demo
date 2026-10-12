"""Pricing: golden vectors, tier boundaries, half-up rounding, Decimal-only."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services import pricing

pytestmark = pytest.mark.unit

D = Decimal
RATE = D("0.08")

# (subtotal, discount, tax, total) at tax_rate 0.08 - docs/ARCHITECTURE.md section 8.1.
GOLDEN_VECTORS = [
    ("0.00", "0.00", "0.00", "0.00"),
    ("1.25", "0.00", "0.10", "1.35"),
    ("39.98", "0.00", "3.20", "43.18"),
    ("99.99", "0.00", "8.00", "107.99"),
    ("100.00", "5.00", "7.60", "102.60"),
    ("150.00", "7.50", "11.40", "153.90"),
    ("499.99", "25.00", "38.00", "512.99"),
    ("500.00", "50.00", "36.00", "486.00"),
    ("600.00", "60.00", "43.20", "583.20"),
]


@pytest.mark.parametrize(("subtotal", "discount", "tax", "total"), GOLDEN_VECTORS)
def test_golden_vectors(subtotal: str, discount: str, tax: str, total: str) -> None:
    totals = pricing.price_order([D(subtotal)], RATE)
    assert totals.subtotal == D(subtotal)
    assert totals.discount == D(discount)
    assert totals.tax == D(tax)
    assert totals.total == D(total)


@pytest.mark.parametrize(("subtotal", "discount", "tax", "total"), GOLDEN_VECTORS)
def test_golden_vectors_are_exact_two_decimal_places(
    subtotal: str, discount: str, tax: str, total: str
) -> None:
    totals = pricing.price_order([D(subtotal)], RATE)
    for value, expected in zip(
        (totals.subtotal, totals.discount, totals.tax, totals.total),
        (subtotal, discount, tax, total),
        strict=True,
    ):
        assert str(value) == expected  # same textual form, i.e. exactly 2 dp


@pytest.mark.parametrize(
    ("subtotal", "rate"),
    [
        ("0.00", "0"),
        ("0.01", "0"),
        ("99.99", "0"),
        ("100.00", "0.05"),
        ("100.01", "0.05"),
        ("499.99", "0.05"),
        ("500.00", "0.10"),
        ("500.01", "0.10"),
        ("100000.00", "0.10"),
    ],
)
def test_discount_tier_boundaries_are_inclusive(subtotal: str, rate: str) -> None:
    assert pricing.discount_rate_for(D(subtotal)) == D(rate)


def test_tier_table_is_ordered_highest_first_with_documented_values() -> None:
    assert (
        (D("500.00"), D("0.10")),
        (D("100.00"), D("0.05")),
    ) == pricing.DISCOUNT_TIERS
    thresholds = [threshold for threshold, _ in pricing.DISCOUNT_TIERS]
    assert thresholds == sorted(thresholds, reverse=True)


def test_crossing_a_tier_never_makes_the_customer_pay_more_in_discount_terms() -> None:
    # 99.99 pays 107.99; 100.00 pays 102.60: the first cent over the line is cheaper in total.
    below = pricing.price_order([D("99.99")], RATE).total
    at = pricing.price_order([D("100.00")], RATE).total
    assert at < below


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.004", "0.00"),
        ("0.0049", "0.00"),
        ("0.005", "0.01"),  # half-up (banker's rounding would give 0.00)
        ("0.015", "0.02"),
        ("0.025", "0.03"),  # banker's would give 0.02
        ("1.005", "1.01"),  # classic float trap: float(1.005) < 1.005
        ("2.675", "2.68"),  # float trap: round(2.675, 2) == 2.67
        ("2.665", "2.67"),
        ("10.994", "10.99"),
        ("10.995", "11.00"),
        ("0.00", "0.00"),
        ("7", "7.00"),
    ],
)
def test_quantize_money_rounds_half_up(value: str, expected: str) -> None:
    result = pricing.quantize_money(D(value))
    assert result == D(expected)
    assert str(result) == expected


def test_quantize_money_leaves_exact_cents_untouched() -> None:
    assert pricing.quantize_money(D("19.99")) == D("19.99")


@pytest.mark.parametrize(
    ("subtotal", "expected_discount"),
    [
        ("100.10", "5.01"),  # 5.005 -> 5.01 half-up (banker's: 5.00)
        ("500.05", "50.01"),  # 50.005 -> 50.01 half-up (banker's: 50.00)
        ("499.99", "25.00"),  # 24.9995 -> 25.00
        ("100.30", "5.02"),  # 5.015 -> 5.02
        ("120.50", "6.03"),  # 6.025 -> 6.03 (banker's: 6.02)
        ("99.99", "0.00"),
    ],
)
def test_discount_rounding_half_up(subtotal: str, expected_discount: str) -> None:
    assert pricing.calculate_discount(D(subtotal)) == D(expected_discount)


@pytest.mark.parametrize(
    ("taxable", "rate", "expected"),
    [
        ("0.60", "0.075", "0.05"),  # 0.045 -> 0.05 half-up (banker's: 0.04)
        ("0.20", "0.075", "0.02"),  # 0.015 -> 0.02
        ("1.00", "0.005", "0.01"),  # 0.005 -> 0.01
        ("1.00", "0.0049", "0.00"),
        ("94.99", "0.08", "7.60"),  # 7.5992 -> 7.60
        ("474.99", "0.08", "38.00"),  # 37.9992 -> 38.00
        ("0.00", "0.08", "0.00"),
    ],
)
def test_tax_rounding_half_up(taxable: str, rate: str, expected: str) -> None:
    assert pricing.calculate_tax(D(taxable), D(rate)) == D(expected)


def test_tax_is_charged_on_the_discounted_subtotal() -> None:
    totals = pricing.price_order([D("600.00")], RATE)
    assert totals.tax == D("43.20")  # 8 % of 540.00
    assert totals.tax != pricing.calculate_tax(D("600.00"), RATE)  # not of the undiscounted 600.00


@pytest.mark.parametrize(
    ("rate", "expected_tax", "expected_total"),
    [
        ("0", "0.00", "95.00"),
        ("0.08", "7.60", "102.60"),
        ("0.10", "9.50", "104.50"),
        ("1", "95.00", "190.00"),
    ],
)
def test_tax_rate_is_a_parameter(rate: str, expected_tax: str, expected_total: str) -> None:
    totals = pricing.price_order([D("100.00")], D(rate))
    assert totals.discount == D("5.00")
    assert totals.tax == D(expected_tax)
    assert totals.total == D(expected_total)


@pytest.mark.parametrize(
    ("unit_price", "quantity", "expected"),
    [
        ("9.99", 2, "19.98"),
        ("9.99", 1, "9.99"),
        ("0.00", 500, "0.00"),
        ("0.01", 10000, "100.00"),
        ("129.99", 3, "389.97"),
        ("12.80", 3, "38.40"),
        ("9999999999.99", 1, "9999999999.99"),
    ],
)
def test_line_total_is_price_times_quantity(unit_price: str, quantity: int, expected: str) -> None:
    result = pricing.calculate_line_total(D(unit_price), quantity)
    assert result == D(expected)
    assert str(result) == expected


def test_line_total_is_exact_where_floats_are_not() -> None:
    assert 0.1 * 3 != 0.3  # the float trap this module must avoid
    assert pricing.calculate_line_total(D("0.10"), 3) == D("0.30")
    assert pricing.calculate_subtotal([D("0.10"), D("0.20")]) == D("0.30")


def test_subtotal_is_the_exact_sum_of_line_totals() -> None:
    assert pricing.calculate_subtotal([D("19.98"), D("129.99"), D("0.03")]) == D("150.00")


def test_subtotal_of_nothing_is_zero_with_cents() -> None:
    result = pricing.calculate_subtotal([])
    assert result == D("0")
    assert str(result) == "0.00"


def test_subtotal_accepts_any_iterable() -> None:
    assert pricing.calculate_subtotal(D(x) for x in ("1.10", "2.20")) == D("3.30")


def test_calculate_total_adds_tax_and_subtracts_discount() -> None:
    assert pricing.calculate_total(D("100.00"), D("5.00"), D("7.60")) == D("102.60")


def test_price_order_sums_multiple_lines_before_tiering() -> None:
    # Two lines each under 100 but together over the 5 % threshold.
    totals = pricing.price_order([D("60.00"), D("40.00")], RATE)
    assert totals.subtotal == D("100.00")
    assert totals.discount == D("5.00")
    assert totals.total == D("102.60")


def test_order_totals_is_frozen() -> None:
    totals = pricing.price_order([D("1.25")], RATE)
    with pytest.raises(AttributeError):
        totals.total = D("0")  # type: ignore[misc]


def test_everything_is_decimal_never_float() -> None:
    totals = pricing.price_order([D("39.98")], RATE)
    for value in (totals.subtotal, totals.discount, totals.tax, totals.total):
        assert type(value) is Decimal
    assert type(pricing.discount_rate_for(D("10"))) is Decimal
    assert type(pricing.CENT) is Decimal
    assert all(
        type(threshold) is Decimal and type(rate) is Decimal
        for threshold, rate in pricing.DISCOUNT_TIERS
    )


def test_float_input_is_not_silently_accepted_as_money() -> None:
    # A float reaching quantize_money would already be imprecise; Decimal.quantize refuses floats.
    with pytest.raises(AttributeError):
        pricing.quantize_money(1.005)  # type: ignore[arg-type]


def test_sweep_invariants_hold_for_every_sampled_cent_amount() -> None:
    """Across 0.00 .. 1200.00: 2 dp money, total = subtotal - discount + tax, tiers hold."""
    for cents in range(0, 120_001, 7):
        subtotal = D(cents).scaleb(-2).quantize(pricing.CENT)
        totals = pricing.price_order([subtotal], RATE)
        assert totals.subtotal == subtotal
        assert D("0.00") <= totals.discount <= subtotal
        assert totals.total == totals.subtotal - totals.discount + totals.tax
        for value in (totals.discount, totals.tax, totals.total):
            assert value.as_tuple().exponent == -2
        expected_rate = D("0.10") if cents >= 50_000 else D("0.05") if cents >= 10_000 else D("0")
        assert pricing.discount_rate_for(subtotal) == expected_rate
