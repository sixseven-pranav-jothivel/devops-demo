"""scripts/smoke_test.py against a real local app (uvicorn, loopback) and a scripted stub."""

from __future__ import annotations

import ast
import importlib.util
import json
import socket
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Callable, Iterator
from decimal import ROUND_HALF_UP, Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import uvicorn

from app.config import Settings
from app.main import create_app
from app.repository.memory import InMemoryRepository
from tests.conftest import FULL_SHA

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "smoke_test.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smoke_test", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_test"] = module
    spec.loader.exec_module(module)
    return module


smoke = load_script()


# ------------------------------------------------------------------ a real app on a loopback port
class _Server(uvicorn.Server):
    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.ready = threading.Event()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        self.ready.set()


@pytest.fixture
def live_url(settings: Settings) -> Iterator[str]:
    app = create_app(settings, InMemoryRepository())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = _Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    assert server.ready.wait(timeout=30), "uvicorn did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=30)
    sock.close()


def run(url: str, *args: str) -> int:
    return int(smoke.main(["--base-url", url, "--timeout", "0", *args]))


def test_passes_against_a_real_app(live_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(live_url) == 0
    out = capsys.readouterr().out
    for name in ("health", "ready", "version", "products", "order", "pricing", "dashboard"):
        assert f"[PASS] {name}:" in out
    assert "SMOKE TEST PASSED" in out
    assert "FAIL" not in out


def test_the_order_round_trip_really_places_an_order(live_url: str) -> None:
    assert run(live_url) == 0
    with urllib.request.urlopen(f"{live_url}/api/orders") as response:
        body = json.load(response)
    assert body["total"] == 2
    tier_order, single_order = body["items"]  # newest first
    assert single_order["line_items"][0]["quantity"] == 1
    assert single_order["discount"] == "0.00"
    # the pricing check crosses the 5 % tier on the highest-stock product (CAB-001, 14 x 7.25)
    assert tier_order["line_items"][0]["sku"] == "CAB-001"
    assert tier_order["line_items"][0]["quantity"] == 14
    assert tier_order["subtotal"] == "101.50"
    assert tier_order["discount"] == "5.08"
    assert tier_order["total"] == "104.13"


def test_expected_sha_full_and_prefix_match(live_url: str) -> None:
    assert run(live_url, "--expect-sha", FULL_SHA) == 0
    assert run(live_url, "--expect-sha", FULL_SHA[:7]) == 0
    assert run(live_url, "--expect-sha", FULL_SHA.upper()) == 0


def test_wrong_sha_fails_with_a_clear_message(
    live_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(live_url, "--expect-sha", "f" * 40) == 1
    out = capsys.readouterr().out
    assert "[FAIL] version:" in out
    assert "git_sha mismatch" in out
    assert "f" * 40 in out
    assert FULL_SHA in out
    assert "SMOKE TEST FAILED: version" in out


def test_skip_order_makes_no_writes(live_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(live_url, "--skip-order") == 0
    out = capsys.readouterr().out
    assert "[SKIP] order: --skip-order given" in out
    assert "[SKIP] pricing: --skip-order given" in out
    with urllib.request.urlopen(f"{live_url}/api/orders") as response:
        assert json.load(response)["total"] == 0


def test_unreachable_service_fails_fast_on_health(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here any more
    assert run(f"http://127.0.0.1:{port}") == 1
    out = capsys.readouterr().out
    assert "[FAIL] health:" in out
    assert "cannot connect" in out
    assert "skipping the remaining checks" in out
    assert "SMOKE TEST FAILED: health" in out


def test_cli_exit_codes_via_a_subprocess(live_url: str) -> None:
    ok = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--base-url", live_url, "--timeout", "0"],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "SMOKE TEST PASSED" in ok.stdout

    bad = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--base-url", live_url, "--timeout", "0",
         "--expect-sha", "0" * 40],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert bad.returncode == 1
    assert "SMOKE TEST FAILED" in bad.stdout


# ------------------------------------------------------------------ usage and configuration
def test_bad_scheme_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert smoke.main(["--base-url", "ftp://example.com"]) == 2
    assert "must start with http" in capsys.readouterr().err


def test_negative_timeouts_are_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert smoke.main(["--base-url", "http://x", "--timeout", "-1"]) == 2
    assert "negative" in capsys.readouterr().err


def test_base_url_and_expected_sha_can_come_from_the_environment(
    live_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMOKE_BASE_URL", live_url)
    monkeypatch.setenv("EXPECT_SHA", FULL_SHA)
    assert smoke.main(["--timeout", "0"]) == 0
    monkeypatch.setenv("EXPECT_SHA", "1" * 40)
    assert smoke.main(["--timeout", "0"]) == 1


def test_base_url_falls_back_to_base_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMOKE_BASE_URL", raising=False)
    monkeypatch.setenv("BASE_URL", "http://example.invalid:1234")
    assert smoke.parse_args([]).base_url == "http://example.invalid:1234"


def test_script_uses_only_the_standard_library() -> None:
    tree = ast.parse(SCRIPT.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


# ------------------------------------------------------------------ retry / backoff (fake clock)
class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def retry(check: Any, clock: FakeClock, **kwargs: float) -> tuple[bool, str, int]:
    params = {"timeout": 30.0, "initial_backoff": 1.0, "max_backoff": 8.0, **kwargs}
    return smoke.run_with_retry(  # type: ignore[no-any-return]
        check, object(), object(), sleep=clock.sleep, now=clock, **params
    )


def make_check(outcomes: list[Exception | str], *, retry_flag: bool = True) -> Any:
    calls = iter(outcomes)

    def run_check(_client: object, _ctx: object) -> str:
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return smoke.Check("t", run_check, retry=retry_flag)


def test_backoff_doubles_and_is_capped() -> None:
    clock = FakeClock()
    check = make_check([smoke.RetryableError("not yet")] * 8 + ["ok"])
    passed, message, attempts = retry(check, clock, timeout=100.0)
    assert (passed, message, attempts) == (True, "ok", 9)
    assert clock.sleeps == [1, 2, 4, 8, 8, 8, 8, 8]


def test_retrying_stops_at_the_deadline_and_reports_the_last_error() -> None:
    clock = FakeClock()
    check = make_check([smoke.RetryableError("still down")] * 100)
    passed, message, attempts = retry(check, clock, timeout=10.0)
    assert passed is False
    assert "still down" in message
    assert "gave up after" in message
    assert clock.now == pytest.approx(10.0)  # never sleeps past the budget
    assert attempts == len(clock.sleeps) + 1


def test_zero_timeout_means_a_single_attempt() -> None:
    clock = FakeClock()
    passed, _, attempts = retry(make_check([smoke.RetryableError("down")] * 3), clock, timeout=0.0)
    assert (passed, attempts, clock.sleeps) == (False, 1, [])


def test_definitive_failures_are_not_retried() -> None:
    clock = FakeClock()
    passed, message, attempts = retry(make_check([smoke.CheckError("wrong")] * 3), clock)
    assert (passed, message, attempts, clock.sleeps) == (False, "wrong", 1, [])


def test_non_idempotent_checks_retry_only_connection_errors() -> None:
    clock = FakeClock()
    check = make_check([smoke.ConnectError("refused"), "done"], retry_flag=False)
    assert retry(check, clock) == (True, "done", 2)
    clock = FakeClock()
    check = make_check([smoke.RetryableError("HTTP 500"), "done"], retry_flag=False)
    assert retry(check, clock) == (False, "HTTP 500", 1)


@pytest.mark.parametrize(
    ("expected", "actual", "result"),
    [
        ("a" * 40, "a" * 40, True),
        ("a" * 7, "a" * 40, True),
        ("A" * 40, "a" * 40, True),
        ("a" * 40, "a" * 7, True),
        ("b" * 40, "a" * 40, False),
        ("abc", "abcdef0", False),  # too short to be a prefix
        ("a" * 40, "unknown", False),
        ("a" * 40, "", False),
        ("", "a" * 40, False),
    ],
)
def test_sha_matching(expected: str, actual: str, result: bool) -> None:
    assert smoke.sha_matches(expected, actual) is result


# ------------------------------------------------------------------ scripted stub server
GOOD_ORDER = {
    "id": 7, "status": "confirmed", "subtotal": "9.99", "discount": "0.00", "tax": "0.80",
    "total": "10.79", "created_at": "2026-01-15T10:30:00Z",
    "line_items": [{"product_id": 1, "sku": "WID-001", "product_name": "W", "quantity": 1,
                    "unit_price": "9.99", "line_total": "9.99"}],
}  # fmt: skip
GOOD_PRODUCT = {"id": 1, "sku": "WID-001", "price": "9.99", "active": True, "quantity_in_stock": 5}
STUB_PRODUCTS = [
    {"id": 1, "sku": "WID-001", "price": "9.99", "active": True, "quantity_in_stock": 500},
    {"id": 5, "sku": "CAB-001", "price": "7.25", "active": True, "quantity_in_stock": 800},
]

Route = Callable[[bytes], tuple[int, Any, str]]


def priced_order(
    order_id: int, product: dict[str, Any], quantity: int, rate: str
) -> dict[str, Any]:
    """A correct order as the real service would price it (independent re-implementation)."""
    cent = Decimal("0.01")
    price = Decimal(product["price"])
    line_total = (price * quantity).quantize(cent, rounding=ROUND_HALF_UP)
    tier = Decimal("0.10") if line_total >= 500 else Decimal("0.05") if line_total >= 100 else 0
    discount = (line_total * Decimal(tier)).quantize(cent, rounding=ROUND_HALF_UP)
    tax = ((line_total - discount) * Decimal(rate)).quantize(cent, rounding=ROUND_HALF_UP)
    return {
        "id": order_id, "status": "confirmed", "subtotal": str(line_total),
        "discount": str(discount), "tax": str(tax), "total": str(line_total - discount + tax),
        "created_at": "2026-01-15T10:30:00Z",
        "line_items": [{
            "product_id": product["id"], "sku": product["sku"], "product_name": "n",
            "quantity": quantity, "unit_price": product["price"], "line_total": str(line_total),
        }],
    }  # fmt: skip


class Stub:
    """A scripted StockFlow look-alike: static routes plus a correct, dynamic order service."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Route] = {}
        self.requests: list[tuple[str, str]] = []
        self.posted: list[dict[str, Any]] = []
        self.orders: dict[int, dict[str, Any]] = {}
        self.products: list[dict[str, Any]] = [dict(p) for p in STUB_PRODUCTS]
        self.tax_rate = "0.08"
        # Hook to corrupt the priced order before it is returned (simulates a pricing defect).
        self.corrupt: Callable[[dict[str, Any]], dict[str, Any]] = lambda order: order
        self.reset_good()

    def reset_good(self) -> None:
        def jsonr(body: Any, status: int = 200) -> Route:
            return lambda _body: (status, json.dumps(body), "application/json")

        self.routes = {
            ("GET", "/health"): jsonr({"status": "ok"}),
            ("GET", "/ready"): jsonr({"status": "ready", "repository": "memory"}),
            ("GET", "/api/version"): jsonr(
                {"name": "stockflow", "version": "1", "git_sha": FULL_SHA, "environment": "t"}
            ),
            ("GET", "/"): lambda _b: (
                200,
                f"<html>StockFlow {FULL_SHA[:7]}</html>",
                "text/html",
            ),
            ("GET", "/api/products"): lambda _b: (
                200,
                json.dumps({"items": self.products, "total": len(self.products)}),
                "application/json",
            ),
            ("POST", "/api/orders"): self.place_order,
        }

    def place_order(self, body: bytes) -> tuple[int, Any, str]:
        line = json.loads(body)["line_items"][0]
        self.posted.append(line)
        product = next(p for p in self.products if p["id"] == line["product_id"])
        order_id = 7 + len(self.orders)
        order = self.corrupt(priced_order(order_id, product, line["quantity"], self.tax_rate))
        self.orders[order_id] = order
        return 201, json.dumps(order), "application/json"

    def set(
        self, method: str, path: str, status: int, body: Any, ctype: str = "application/json"
    ) -> None:
        text = body if isinstance(body, str) else json.dumps(body)
        self.routes[(method, path)] = lambda _body: (status, text, ctype)

    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p in self.requests if (m, p) == (method, path))


@pytest.fixture
def stub() -> Iterator[tuple[Stub, str]]:
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            path = self.path.split("?")[0]
            state.requests.append((self.command, path))
            route = state.routes.get((self.command, path))
            if route is None and self.command == "GET" and path.startswith("/api/orders/"):
                order = state.orders.get(int(path.rsplit("/", 1)[1]))
                if order is not None:
                    route = lambda _b: (200, json.dumps(order), "application/json")  # noqa: E731
            status, body, ctype = route(raw) if route else (404, "{}", "application/json")
            payload = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _serve  # noqa: N815

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    yield state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=30)


def test_stub_baseline_passes(stub: tuple[Stub, str]) -> None:
    _, url = stub
    assert run(url) == 0


@pytest.mark.parametrize(
    ("method", "path", "status", "body", "ctype", "failing", "fragment"),
    [
        ("GET", "/health", 500, {"error": "x"}, "application/json", "health", "expected HTTP 200"),
        ("GET", "/health", 200, "not json", "text/plain", "health", "not valid JSON"),
        ("GET", "/health", 200, {"status": "degraded"}, "application/json", "health", "degraded"),
        ("GET", "/ready", 503, {"status": "not_ready"}, "application/json", "ready", "got 503"),
        ("GET", "/ready", 200, {"status": "starting"}, "application/json", "ready", "starting"),
        (
            "GET",
            "/api/version",
            200,
            {"name": "other", "git_sha": "x"},
            "application/json",
            "version",
            "expected 'stockflow'",
        ),
        ("GET", "/api/version", 200, [1], "application/json", "version", "JSON object"),
        (
            "GET",
            "/api/products",
            200,
            {"items": [], "total": 0},
            "application/json",
            "products",
            "non-empty",
        ),
        ("GET", "/api/products", 500, {}, "application/json", "products", "got 500"),
        (
            "POST",
            "/api/orders",
            409,
            {"error": {"code": "insufficient_stock"}},
            "application/json",
            "order",
            "expected HTTP 201, got 409",
        ),
        (
            "POST",
            "/api/orders",
            201,
            {"id": 7},
            "application/json",
            "order",
            "unexpected order body",
        ),
        ("GET", "/api/orders/7", 404, {}, "application/json", "order", "after creation, got 404"),
        ("GET", "/", 200, "<html>nothing</html>", "text/html", "dashboard", "StockFlow"),
        ("GET", "/", 200, "StockFlow", "application/json", "dashboard", "expected HTML"),
    ],
)
def test_each_check_reports_a_clear_failure(
    stub: tuple[Stub, str],
    capsys: pytest.CaptureFixture[str],
    *,
    method: str,
    path: str,
    status: int,
    body: Any,
    ctype: str,
    failing: str,
    fragment: str,
) -> None:
    state, url = stub
    state.set(method, path, status, body, ctype)
    assert run(url) == 1
    out = capsys.readouterr().out
    assert f"[FAIL] {failing}:" in out
    assert fragment in out
    assert f"SMOKE TEST FAILED: {failing}" in out


def test_inconsistent_order_totals_fail(
    stub: tuple[Stub, str], capsys: pytest.CaptureFixture[str]
) -> None:
    state, url = stub
    state.set("POST", "/api/orders", 201, {**GOOD_ORDER, "total": "99.99"})
    assert run(url) == 1
    assert "total 99.99 != subtotal 9.99" in capsys.readouterr().out


def test_wrong_unit_price_fails(stub: tuple[Stub, str], capsys: pytest.CaptureFixture[str]) -> None:
    state, url = stub
    state.set(
        "GET", "/api/products", 200, {"items": [{**GOOD_PRODUCT, "price": "5.00"}], "total": 1}
    )
    assert run(url) == 1
    assert "unit_price 9.99 differs" in capsys.readouterr().out


def test_sold_out_catalogue_fails_the_order_check(
    stub: tuple[Stub, str], capsys: pytest.CaptureFixture[str]
) -> None:
    state, url = stub
    state.set(
        "GET",
        "/api/products",
        200,
        {"items": [{**GOOD_PRODUCT, "quantity_in_stock": 0}], "total": 1},
    )
    assert run(url) == 1
    assert "sold out" in capsys.readouterr().out
    assert state.count("POST", "/api/orders") == 0


def test_all_failures_are_reported_not_just_the_first(
    stub: tuple[Stub, str], capsys: pytest.CaptureFixture[str]
) -> None:
    state, url = stub
    state.set("GET", "/ready", 503, {})
    state.set("GET", "/", 500, "")
    assert run(url) == 1
    assert "SMOKE TEST FAILED: ready, dashboard" in capsys.readouterr().out


def test_failed_products_check_skips_the_order(
    stub: tuple[Stub, str], capsys: pytest.CaptureFixture[str]
) -> None:
    state, url = stub
    state.set("GET", "/api/products", 500, {})
    assert run(url) == 1
    out = capsys.readouterr().out
    assert "[SKIP] order" in out
    assert state.count("POST", "/api/orders") == 0


def test_a_rejected_order_is_never_retried(stub: tuple[Stub, str]) -> None:
    state, url = stub
    state.set("POST", "/api/orders", 500, {})
    ok = smoke.run_smoke(
        url, timeout=5.0, initial_backoff=0.0, max_backoff=0.0, out=lambda _m: None
    )
    assert ok is False
    assert state.count("POST", "/api/orders") == 2  # order and pricing: one attempt each


def test_a_flaky_health_endpoint_is_retried_until_it_recovers(
    stub: tuple[Stub, str], capsys: pytest.CaptureFixture[str]
) -> None:
    state, url = stub
    original = state.routes[("GET", "/health")]
    calls = {"n": 0}

    def flaky(_body: bytes) -> tuple[int, Any, str]:
        calls["n"] += 1
        return (503, "{}", "application/json") if calls["n"] < 3 else original(_body)

    state.routes[("GET", "/health")] = flaky
    ok = smoke.run_smoke(url, timeout=30.0, initial_backoff=0.0, max_backoff=0.0)
    assert ok is True
    assert calls["n"] == 3
    assert "after 3 attempts" in capsys.readouterr().out


def test_a_rolling_deploy_is_waited_out_until_the_expected_sha_appears(
    stub: tuple[Stub, str],
) -> None:
    state, url = stub
    new_sha = "9" * 40
    calls = {"n": 0}

    def version(_body: bytes) -> tuple[int, Any, str]:
        calls["n"] += 1
        sha = FULL_SHA if calls["n"] < 4 else new_sha
        return (
            200,
            json.dumps({"name": "stockflow", "version": "2", "git_sha": sha}),
            "application/json",
        )

    state.routes[("GET", "/api/version")] = version
    state.routes[("GET", "/")] = lambda _b: (200, "<html>StockFlow 9999999</html>", "text/html")
    ok = smoke.run_smoke(
        url,
        expect_sha=new_sha,
        timeout=30.0,
        initial_backoff=0.0,
        max_backoff=0.0,
        out=lambda _m: None,
    )
    assert ok is True
    assert calls["n"] == 4


def test_output_is_line_oriented_and_names_the_target(
    stub: tuple[Stub, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _, url = stub
    run(url, "--expect-sha", FULL_SHA)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"StockFlow smoke test against {url}"
    assert lines[1] == f"expecting git sha {FULL_SHA}"
    assert all(line.startswith(("[", "StockFlow", "expecting", "SMOKE")) for line in lines)


# ------------------------------------------------------------------ pricing invariants
def test_stub_baseline_places_the_tier_order_on_the_highest_stock_product(
    stub: tuple[Stub, str], capsys: pytest.CaptureFixture[str]
) -> None:
    state, url = stub
    assert run(url) == 0
    assert state.posted == [
        {"product_id": 1, "quantity": 1},
        {"product_id": 5, "quantity": 14},  # 14 x 7.25 = 101.50, the first quantity past 100.00
    ]
    out = capsys.readouterr().out
    assert "[PASS] pricing: order #8 14 x CAB-001: subtotal 101.50 discount 5.08" in out


def test_a_total_that_ignores_the_discount_fails_with_a_clear_message(
    stub: tuple[Stub, str], capsys: pytest.CaptureFixture[str]
) -> None:
    state, url = stub
    state.corrupt = lambda o: {**o, "total": str(Decimal(o["subtotal"]) + Decimal(o["tax"]))}
    assert run(url) == 1
    out = capsys.readouterr().out
    assert "[PASS] order:" in out  # the undiscounted order is unaffected by this defect
    assert (
        "[FAIL] pricing: order: total 109.21 != subtotal 101.50 - discount 5.08 + tax 7.71" in out
    )
    assert "(= 104.13)" in out
    assert "SMOKE TEST FAILED: pricing" in out


@pytest.mark.parametrize(
    ("discount", "fragment"),
    [
        ("0.00", "discount 0.00 != expected 5.08 for subtotal 101.50"),
        ("3.05", "discount 3.05 != expected 5.08"),
        ("5.07", "discount 5.07 != expected 5.08"),
    ],
)
def test_a_wrong_discount_fails(
    stub: tuple[Stub, str], capsys: pytest.CaptureFixture[str], discount: str, fragment: str
) -> None:
    state, url = stub

    def corrupt(order: dict[str, Any]) -> dict[str, Any]:
        if Decimal(order["discount"]) == 0:
            return order
        total = Decimal(order["subtotal"]) - Decimal(discount) + Decimal(order["tax"])
        return {**order, "discount": discount, "total": str(total)}

    state.corrupt = corrupt
    assert run(url) == 1
    out = capsys.readouterr().out
    assert "[FAIL] pricing:" in out
    assert fragment in out


def test_discount_is_rounded_half_up_not_down(
    stub: tuple[Stub, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """101.50 x 5 % = 5.075: half-up is 5.08. A half-down/banker's implementation says 5.07."""
    state, url = stub

    def corrupt(order: dict[str, Any]) -> dict[str, Any]:
        if Decimal(order["discount"]) == 0:
            return order
        return {
            **order,
            "discount": "5.07",
            "total": str(Decimal("101.50") - Decimal("5.07") + Decimal(order["tax"])),
        }

    state.corrupt = corrupt
    assert run(url) == 1
    assert "discount 5.07 != expected 5.08" in capsys.readouterr().out


def test_tax_on_the_undiscounted_subtotal_is_detected_without_knowing_the_rate(
    stub: tuple[Stub, str], capsys: pytest.CaptureFixture[str]
) -> None:
    state, url = stub

    def corrupt(order: dict[str, Any]) -> dict[str, Any]:
        if Decimal(order["discount"]) == 0:
            return order
        tax = (Decimal(order["subtotal"]) * Decimal("0.08")).quantize(Decimal("0.01"))
        total = Decimal(order["subtotal"]) - Decimal(order["discount"]) + tax
        return {**order, "tax": str(tax), "total": str(total)}

    state.corrupt = corrupt
    assert run(url) == 1
    out = capsys.readouterr().out
    assert "[FAIL] pricing:" in out
    assert "inconsistent with the tax rate" in out


@pytest.mark.parametrize("rate", ["0.08", "0.10", "0.0725", "0", "0.2"])
def test_any_consistent_tax_rate_passes_because_the_rate_is_not_hard_coded(
    stub: tuple[Stub, str], rate: str
) -> None:
    state, url = stub
    state.tax_rate = rate
    assert run(url) == 0


def test_a_changed_catalogue_without_a_suitable_product_skips_the_tier_order(
    stub: tuple[Stub, str], capsys: pytest.CaptureFixture[str]
) -> None:
    state, url = stub
    state.products = [
        {"id": 3, "sku": "BIG-1", "price": "700.00", "active": True, "quantity_in_stock": 9},
        {"id": 4, "sku": "TINY-1", "price": "9.99", "active": True, "quantity_in_stock": 3},
    ]
    assert run(url) == 0
    out = capsys.readouterr().out
    assert "[SKIP] pricing: no active product can reach a 100.00 - 499.99 subtotal" in out
    assert "[PASS] order:" in out
    assert state.posted == [{"product_id": 3, "quantity": 1}]


def test_the_tier_order_uses_a_different_product_when_the_catalogue_changes(
    stub: tuple[Stub, str],
) -> None:
    state, url = stub
    state.products = [
        {"id": 2, "sku": "MID-1", "price": "129.99", "active": True, "quantity_in_stock": 40},
        {"id": 9, "sku": "OFF-1", "price": "50.00", "active": False, "quantity_in_stock": 9999},
    ]
    assert run(url) == 0
    assert state.posted[-1] == {"product_id": 2, "quantity": 1}


@pytest.mark.parametrize(
    ("products", "expected"),
    [
        ([{"price": "100.00", "quantity_in_stock": 5, "active": True}], 1),
        ([{"price": "499.99", "quantity_in_stock": 1, "active": True}], 1),
        ([{"price": "500.00", "quantity_in_stock": 9, "active": True}], None),
        ([{"price": "33.34", "quantity_in_stock": 9, "active": True}], 3),  # 100.02
        ([{"price": "0.01", "quantity_in_stock": 10000, "active": True}], 10000),
        ([{"price": "0.01", "quantity_in_stock": 9999, "active": True}], None),
        ([{"price": "10.00", "quantity_in_stock": 9, "active": True}], None),  # needs 10
        ([{"price": "10.00", "quantity_in_stock": 10, "active": True}], 10),
        ([{"price": "10.00", "quantity_in_stock": 10, "active": False}], None),
        ([{"price": "0.00", "quantity_in_stock": 10, "active": True}], None),
        ([{"price": "50.00", "quantity_in_stock": 0, "active": True}], None),
        ([{"quantity_in_stock": 5, "active": True}], None),
        ([], None),
    ],
)
def test_tier_candidate_quantity(products: list[dict[str, Any]], expected: int | None) -> None:
    choice = smoke.tier_candidate(products)
    assert (None if choice is None else choice[1]) == expected


def test_tier_candidate_prefers_the_most_stock() -> None:
    a = {"id": 1, "price": "20.00", "quantity_in_stock": 50, "active": True}
    b = {"id": 2, "price": "25.00", "quantity_in_stock": 900, "active": True}
    c = {"id": 3, "price": "30.00", "quantity_in_stock": 100, "active": True}
    choice = smoke.tier_candidate([a, b, c])
    assert choice is not None
    assert (choice[0]["id"], choice[1]) == (2, 4)


@pytest.mark.parametrize(
    ("subtotal", "expected"),
    [
        ("0.00", "0.00"),
        ("99.99", "0.00"),
        ("100.00", "5.00"),
        ("101.50", "5.08"),
        ("499.99", "25.00"),
        ("500.00", "50.00"),
        ("500.05", "50.01"),
    ],
)
def test_expected_discount(subtotal: str, expected: str) -> None:
    assert smoke.expected_discount(Decimal(subtotal)) == Decimal(expected)


def order_with(subtotal: str, discount: str, tax: str) -> dict[str, str]:
    return {"subtotal": subtotal, "discount": discount, "tax": tax}


def test_tax_rate_intervals_must_intersect() -> None:
    ctx = smoke.Context()
    smoke.narrow_tax_rate(ctx, order_with("9.99", "0.00", "0.80"))  # rate in [0.0800, 0.0801)
    smoke.narrow_tax_rate(ctx, order_with("101.50", "5.08", "7.71"))  # consistent with 0.08
    assert ctx.rate_low <= Decimal("0.08") <= ctx.rate_high
    with pytest.raises(smoke.CheckError, match="inconsistent with the tax rate"):
        smoke.narrow_tax_rate(ctx, order_with("101.50", "5.08", "8.12"))  # 8 % of the gross


def test_orders_without_a_tax_base_do_not_constrain_the_rate() -> None:
    ctx = smoke.Context()
    assert smoke.narrow_tax_rate(ctx, order_with("0.00", "0.00", "0.00")) == "no tax base"
    assert ctx.rate_low is None
