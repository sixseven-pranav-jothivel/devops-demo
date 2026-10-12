#!/usr/bin/env python3
"""Post-deploy smoke test for StockFlow. Standard library only.

Checks, in order (each is retried with bounded exponential backoff until it passes or its
time budget runs out, because a freshly deployed instance may still be starting or rolling):

  health    GET /health               200, {"status": "ok"}
  ready     GET /ready                200, {"status": "ready", ...}
  version   GET /api/version          200, name "stockflow", git_sha matches --expect-sha if given
  products  GET /api/products         200, non-empty catalogue
  order     POST /api/orders          one single-unit order round trip (skip with --skip-order)
  pricing   POST /api/orders          one order that crosses the 5 % discount tier (>= 100.00,
                                      < 500.00) with the pricing invariants asserted (see below)
  dashboard GET /                     200, HTML containing "StockFlow" (and the short sha)

The order and pricing checks are the only writes (use a high-stock product; both are skipped
with --skip-order). They are not retried after the server answered (a retry could create a second
order); only a failure to connect is retried.

Pricing invariants, all in Decimal, none of them hard-codes the tax rate:
  line_total == unit_price * quantity (half up), subtotal == sum(line totals)
  discount   == half_up(subtotal * tier rate)   (5 % from 100.00, 10 % from 500.00, else 0)
  tax        == half_up((subtotal - discount) * rate) for ONE rate shared by every order placed in
                this run (the rate is derived from the orders themselves: each order allows an
                interval of rates, the intervals must intersect and lie within [0, 1])
  total      == subtotal - discount + tax
This is what makes a defect such as "total ignores the discount" or "tax on the undiscounted
subtotal" visible right after a deploy.

Usage:
  python scripts/smoke_test.py --base-url https://stockflow.onrender.com --expect-sha "$GIT_SHA"
  SMOKE_BASE_URL=http://localhost:8000 python scripts/smoke_test.py

Exit status: 0 when every check passed, 1 when any check failed, 2 for usage errors.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 60.0  # seconds of retrying per check
DEFAULT_REQUEST_TIMEOUT = 10.0  # seconds for a single HTTP request
DEFAULT_INITIAL_BACKOFF = 1.0
DEFAULT_MAX_BACKOFF = 8.0
MIN_SHA_PREFIX = 7
USER_AGENT = "stockflow-smoke-test/1"


class CheckError(Exception):
    """A check did not pass; the message is shown to the operator as-is."""


class CheckSkippedError(Exception):
    """The check does not apply to the current data (for example no suitable product)."""


class RetryableError(CheckError):
    """A check failed in a way that may heal by itself (not started yet, still rolling out)."""


class ConnectError(RetryableError):
    """Nothing was sent or answered (connection refused/reset, DNS): always safe to retry."""


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError as exc:
            raise RetryableError(
                f"response is not valid JSON ({exc}): {self.text()[:200]!r}"
            ) from exc


class Client:
    def __init__(self, base_url: str, request_timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout

    def request(self, method: str, path: str, payload: Any = None) -> Response:
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(  # scheme validated in main()  # nosec B310
            self.base_url + path, data=data, method=method, headers=headers
        )
        url = f"{method} {self.base_url}{path}"
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:  # nosec B310
                return Response(resp.status, resp.read(), resp.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.read(), exc.headers.get("Content-Type", ""))
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise RetryableError(f"{url}: timed out after {self.request_timeout:g}s") from exc
            raise ConnectError(f"{url}: cannot connect ({exc.reason})") from exc
        except TimeoutError as exc:
            raise RetryableError(f"{url}: timed out after {self.request_timeout:g}s") from exc
        except OSError as exc:
            raise RetryableError(f"{url}: network error ({exc})") from exc


def expect_status(response: Response, expected: int, what: str) -> None:
    if response.status != expected:
        raise RetryableError(
            f"{what}: expected HTTP {expected}, got {response.status}: {response.text()[:200]!r}"
        )


# ---------------------------------------------------------------------------- checks
def check_health(client: Client, _ctx: Context) -> str:
    response = client.request("GET", "/health")
    expect_status(response, 200, "GET /health")
    body = response.json()
    if body != {"status": "ok"}:
        raise RetryableError(f"GET /health: expected {{'status': 'ok'}}, got {body!r}")
    return "status ok"


def check_ready(client: Client, _ctx: Context) -> str:
    response = client.request("GET", "/ready")
    expect_status(response, 200, "GET /ready")
    body = response.json()
    if not isinstance(body, dict) or body.get("status") != "ready":
        raise RetryableError(f"GET /ready: expected status 'ready', got {body!r}")
    return f"ready (repository: {body.get('repository', '?')})"


def sha_matches(expected: str, actual: str) -> bool:
    """Full match, or either side is a prefix of the other (at least 7 characters)."""
    expected, actual = expected.strip().lower(), actual.strip().lower()
    if not expected or not actual or actual == "unknown":
        return False
    if len(expected) < MIN_SHA_PREFIX or len(actual) < MIN_SHA_PREFIX:
        return expected == actual
    return actual.startswith(expected) or expected.startswith(actual)


def check_version(client: Client, ctx: Context) -> str:
    response = client.request("GET", "/api/version")
    expect_status(response, 200, "GET /api/version")
    body = response.json()
    if not isinstance(body, dict):
        raise RetryableError(f"GET /api/version: expected a JSON object, got {body!r}")
    if body.get("name") != "stockflow":
        raise RetryableError(
            f"GET /api/version: name is {body.get('name')!r}, expected 'stockflow'"
        )
    actual = str(body.get("git_sha", ""))
    ctx.git_sha = actual
    if ctx.expect_sha and not sha_matches(ctx.expect_sha, actual):
        raise RetryableError(
            f"GET /api/version: git_sha mismatch: expected {ctx.expect_sha!r}, deployed {actual!r} "
            "(old version still serving, or the wrong build was deployed)"
        )
    note = "matches --expect-sha" if ctx.expect_sha else "no --expect-sha given"
    return f"version {body.get('version')} sha {actual[:12]} env {body.get('environment')} ({note})"


def check_products(client: Client, ctx: Context) -> str:
    response = client.request("GET", "/api/products?limit=200")
    expect_status(response, 200, "GET /api/products")
    body = response.json()
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list) or not items:
        raise RetryableError(
            f"GET /api/products: expected a non-empty catalogue, got {body!r}"[:300]
        )
    ctx.products = items
    return f"{body.get('total', len(items))} product(s)"


def decimal_of(value: Any, what: str) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise CheckError(f"{what}: {value!r} is not a decimal amount") from exc


CENT = Decimal("0.01")
# Discount tiers from docs/ARCHITECTURE.md section 8.1 (subtotal threshold inclusive, rate).
DISCOUNT_TIERS = ((Decimal("500.00"), Decimal("0.10")), (Decimal("100.00"), Decimal("0.05")))
TIER_MIN = Decimal("100.00")
TIER_MAX = Decimal("500.00")  # exclusive: the pricing check targets the 5 % tier only
HALF_CENT = Decimal("0.005")
MAX_QUANTITY = 10000


def half_up(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def expected_discount(subtotal: Decimal) -> Decimal:
    for threshold, rate in DISCOUNT_TIERS:
        if subtotal >= threshold:
            return half_up(subtotal * rate)
    return Decimal("0.00")


def verify_order_pricing(order: dict[str, Any], product: dict[str, Any], quantity: int) -> str:
    """Assert the pricing invariants of a created order; narrow ctx-independent part."""
    try:
        subtotal = decimal_of(order["subtotal"], "order.subtotal")
        discount = decimal_of(order["discount"], "order.discount")
        tax = decimal_of(order["tax"], "order.tax")
        total = decimal_of(order["total"], "order.total")
        lines = order["line_items"]
        if len(lines) != 1:
            raise CheckError(f"order: expected 1 line item, got {len(lines)}")
        unit_price = decimal_of(lines[0]["unit_price"], "order.unit_price")
        line_total = decimal_of(lines[0]["line_total"], "order.line_total")
        line_qty = lines[0]["quantity"]
    except (KeyError, IndexError, TypeError) as exc:
        raise CheckError(f"POST /api/orders: unexpected order body: {order!r}"[:300]) from exc
    price = decimal_of(product["price"], "product.price")
    if unit_price != price:
        raise CheckError(f"order: unit_price {unit_price} differs from the product price {price}")
    if line_qty != quantity:
        raise CheckError(f"order: line quantity {line_qty} != ordered quantity {quantity}")
    if line_total != half_up(price * quantity):
        raise CheckError(
            f"order: line_total {line_total} != {quantity} x {price} = {half_up(price * quantity)}"
        )
    if subtotal != line_total:
        raise CheckError(f"order: subtotal {subtotal} != sum of line totals {line_total}")
    want_discount = expected_discount(subtotal)
    if discount != want_discount:
        raise CheckError(
            f"order: discount {discount} != expected {want_discount} for subtotal {subtotal}"
        )
    if total != subtotal - discount + tax:
        raise CheckError(
            f"order: total {total} != subtotal {subtotal} - discount {discount} + tax {tax}"
            f" (= {subtotal - discount + tax})"
        )
    return f"subtotal {subtotal} discount {discount} tax {tax} total {total}"


def narrow_tax_rate(ctx: Context, order: dict[str, Any]) -> str:
    """Intersect the tax-rate interval this order allows with the ones seen so far."""
    subtotal = Decimal(str(order["subtotal"]))
    taxable = subtotal - Decimal(str(order["discount"]))
    tax = Decimal(str(order["tax"]))
    if taxable <= 0:
        return "no tax base"
    low = max(Decimal(0), (tax - HALF_CENT) / taxable)
    high = min(Decimal(1), (tax + HALF_CENT) / taxable)
    ctx.rate_low = low if ctx.rate_low is None else max(ctx.rate_low, low)
    ctx.rate_high = high if ctx.rate_high is None else min(ctx.rate_high, high)
    if ctx.rate_low > ctx.rate_high:
        raise CheckError(
            f"order: tax {tax} on a taxable amount of {taxable} is inconsistent with the tax rate "
            f"implied by the other order(s) (rates must overlap: need {ctx.rate_low:.6f} <= rate "
            f"<= {ctx.rate_high:.6f}); tax is not a fixed rate of subtotal - discount"
        )
    return f"tax rate ~{(ctx.rate_low + ctx.rate_high) / 2:.3f}"


def place_and_verify(
    client: Client, ctx: Context, product: dict[str, Any], quantity: int
) -> tuple[dict[str, Any], str]:
    response = client.request(
        "POST",
        "/api/orders",
        {"line_items": [{"product_id": product["id"], "quantity": quantity}]},
    )
    if response.status != 201:
        raise CheckError(
            f"POST /api/orders: expected HTTP 201, got {response.status}: {response.text()[:200]!r}"
        )
    order = response.json()
    if not isinstance(order, dict) or "id" not in order:
        raise CheckError(f"POST /api/orders: unexpected order body: {order!r}"[:300])
    amounts = verify_order_pricing(order, product, quantity)
    rate = narrow_tax_rate(ctx, order)
    order_id = order["id"]
    fetched = client.request("GET", f"/api/orders/{order_id}")
    if fetched.status != 200:
        raise CheckError(
            f"GET /api/orders/{order_id}: expected HTTP 200 after creation, got {fetched.status}"
        )
    if fetched.json() != order:
        raise CheckError(f"GET /api/orders/{order_id}: stored order differs from the created one")
    return order, f"order #{order_id} {quantity} x {product.get('sku')}: {amounts}, {rate}"


def check_order(client: Client, ctx: Context) -> str:
    candidates = [
        p for p in ctx.products if p.get("active") and int(p.get("quantity_in_stock", 0)) > 0
    ]
    if not candidates:
        raise CheckError("order: no active product with stock to order (catalogue is sold out)")
    _, message = place_and_verify(client, ctx, candidates[0], 1)
    return message + " round-tripped"


def tier_candidate(products: list[dict[str, Any]]) -> tuple[dict[str, Any], int] | None:
    """The highest-stock active product that can reach 100.00 <= subtotal < 500.00 in stock."""
    best: tuple[dict[str, Any], int] | None = None
    for product in products:
        try:
            price = Decimal(str(product["price"]))
            stock = int(product["quantity_in_stock"])
        except (KeyError, ValueError, InvalidOperation):
            continue
        if not product.get("active") or price <= 0 or stock <= 0:
            continue
        quantity = int((TIER_MIN / price).to_integral_value(rounding="ROUND_CEILING"))
        quantity = max(quantity, 1)
        subtotal = half_up(price * quantity)
        if quantity > min(stock, MAX_QUANTITY) or not TIER_MIN <= subtotal < TIER_MAX:
            continue
        if best is None or stock > int(best[0]["quantity_in_stock"]):
            best = (product, quantity)
    return best


def check_pricing(client: Client, ctx: Context) -> str:
    choice = tier_candidate(ctx.products or [])
    if choice is None:
        raise CheckSkippedError(
            "no active product can reach a 100.00 - 499.99 subtotal with the stock available"
        )
    product, quantity = choice
    order, message = place_and_verify(client, ctx, product, quantity)
    if Decimal(str(order["discount"])) <= 0:
        raise CheckError(f"pricing: subtotal {order['subtotal']} got no discount (expected 5 %)")
    return message


def check_dashboard(client: Client, ctx: Context) -> str:
    response = client.request("GET", "/")
    expect_status(response, 200, "GET /")
    if "html" not in response.content_type.lower():
        raise RetryableError(f"GET /: expected HTML, got content-type {response.content_type!r}")
    html = response.text()
    if "StockFlow" not in html:
        raise RetryableError("GET /: page does not contain 'StockFlow'")
    if ctx.expect_sha and ctx.git_sha and ctx.git_sha[:7] not in html:
        raise RetryableError(f"GET /: page does not show the deployed commit {ctx.git_sha[:7]!r}")
    return "dashboard rendered"


@dataclass
class Context:
    expect_sha: str | None = None
    git_sha: str = ""
    products: list[dict[str, Any]] | None = None
    rate_low: Decimal | None = None  # tax-rate interval implied by the orders placed so far
    rate_high: Decimal | None = None

    def __post_init__(self) -> None:
        if self.products is None:
            self.products = []


@dataclass(frozen=True)
class Check:
    name: str
    run: Callable[[Client, Context], str]
    retry: bool = True  # False: only connection errors are retried (non-idempotent)


WRITE_CHECKS = frozenset({"order", "pricing"})

CHECKS: tuple[Check, ...] = (
    Check("health", check_health),
    Check("ready", check_ready),
    Check("version", check_version),
    Check("products", check_products),
    Check("order", check_order, retry=False),
    Check("pricing", check_pricing, retry=False),
    Check("dashboard", check_dashboard),
)


# ---------------------------------------------------------------------------- runner
def run_with_retry(
    check: Check,
    client: Client,
    ctx: Context,
    *,
    timeout: float,
    initial_backoff: float,
    max_backoff: float,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> tuple[bool, str, int]:
    """Run one check; returns (passed, message, attempts)."""
    deadline = now() + timeout
    delay = initial_backoff
    attempts = 0
    while True:
        attempts += 1
        try:
            return True, check.run(client, ctx), attempts
        except RetryableError as exc:
            # A non-idempotent check (the order) is only repeated when nothing reached the server.
            if not check.retry and not isinstance(exc, ConnectError):
                return False, str(exc), attempts
            message = str(exc)
        except CheckSkippedError as exc:
            return True, f"SKIP: {exc}", attempts
        except CheckError as exc:
            return False, str(exc), attempts
        remaining = deadline - now()
        if remaining <= 0:
            return (
                False,
                f"{message} (gave up after {attempts} attempt(s) / {timeout:g}s)",
                attempts,
            )
        sleep(min(delay, max_backoff, remaining))
        delay = min(delay * 2, max_backoff)


def run_smoke(
    base_url: str,
    *,
    expect_sha: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    initial_backoff: float = DEFAULT_INITIAL_BACKOFF,
    max_backoff: float = DEFAULT_MAX_BACKOFF,
    skip_order: bool = False,
    out: Callable[[str], None] = print,
) -> bool:
    client = Client(base_url, request_timeout)
    ctx = Context(expect_sha=expect_sha or None)
    out(f"StockFlow smoke test against {client.base_url}")
    if expect_sha:
        out(f"expecting git sha {expect_sha}")
    failures: list[str] = []
    for check in CHECKS:
        if skip_order and check.name in WRITE_CHECKS:
            out(f"[SKIP] {check.name}: --skip-order given (no writes)")
            continue
        if check.name in WRITE_CHECKS and "products" in failures:
            out(f"[SKIP] {check.name}: the products check failed, nothing to order")
            continue
        passed, message, attempts = run_with_retry(
            check,
            client,
            ctx,
            timeout=timeout,
            initial_backoff=initial_backoff,
            max_backoff=max_backoff,
        )
        if passed and message.startswith("SKIP: "):
            out(f"[SKIP] {check.name}: {message[6:]}")
            continue
        retries = f" after {attempts} attempts" if attempts > 1 else ""
        out(f"[{'PASS' if passed else 'FAIL'}] {check.name}: {message}{retries if passed else ''}")
        if not passed:
            failures.append(check.name)
            if check.name == "health":
                out("service is not alive; skipping the remaining checks")
                break
    if failures:
        out(f"SMOKE TEST FAILED: {', '.join(failures)}")
        return False
    out("SMOKE TEST PASSED")
    return True


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="StockFlow post-deploy smoke test (standard library only).",
        epilog="Exit status: 0 all checks passed, 1 a check failed, 2 usage error.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SMOKE_BASE_URL") or os.environ.get("BASE_URL") or DEFAULT_BASE_URL,
        help=f"service root (env SMOKE_BASE_URL or BASE_URL; default {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--expect-sha",
        default=os.environ.get("EXPECT_SHA") or None,
        help="git sha /api/version must report (env EXPECT_SHA); retried while an old one serves",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("SMOKE_TIMEOUT", DEFAULT_TIMEOUT)),
        help=f"seconds to retry each check (default {DEFAULT_TIMEOUT:g}; 0 = single attempt)",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
        help=f"seconds per HTTP request (default {DEFAULT_REQUEST_TIMEOUT:g})",
    )
    parser.add_argument(
        "--initial-backoff",
        type=float,
        default=DEFAULT_INITIAL_BACKOFF,
        help=f"first retry delay in s, doubled per attempt (default {DEFAULT_INITIAL_BACKOFF:g})",
    )
    parser.add_argument(
        "--max-backoff",
        type=float,
        default=DEFAULT_MAX_BACKOFF,
        help=f"upper bound for the retry delay in seconds (default {DEFAULT_MAX_BACKOFF:g})",
    )
    parser.add_argument(
        "--skip-order",
        action="store_true",
        help="read-only run: do not place the order round-trip",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.base_url.startswith(("http://", "https://")):
        print(
            f"error: --base-url must start with http:// or https://, got {args.base_url!r}",
            file=sys.stderr,
        )
        return 2
    if min(args.timeout, args.request_timeout, args.initial_backoff, args.max_backoff) < 0:
        print("error: timeouts and backoffs must not be negative", file=sys.stderr)
        return 2
    ok = run_smoke(
        args.base_url,
        expect_sha=args.expect_sha,
        timeout=args.timeout,
        request_timeout=args.request_timeout,
        initial_backoff=args.initial_backoff,
        max_backoff=args.max_backoff,
        skip_order=args.skip_order,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
