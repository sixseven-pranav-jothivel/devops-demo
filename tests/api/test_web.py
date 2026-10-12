"""GET / (server-rendered dashboard) and static assets."""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.repository.base import Repository
from tests.conftest import FULL_SHA
from tests.helpers import WID_001, assert_error, order_payload, product_payload

pytestmark = pytest.mark.integration


def test_dashboard_is_html(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text.lstrip().lower().startswith("<!doctype html>")
    assert "<title>StockFlow dashboard</title>" in response.text


def test_dashboard_shows_release_metadata(client: TestClient) -> None:
    html = client.get("/").text
    assert "9.9.9-test" in html  # version
    assert FULL_SHA[:7] in html  # short commit
    assert FULL_SHA in html  # full sha in the footer tooltip
    assert ">ci<" in html  # environment badge
    assert "2026-01-15T10:30:00Z" in html  # build time


def test_dashboard_order_preview_follows_configured_tax_rate(repo: Repository) -> None:
    with TestClient(create_app(Settings(tax_rate=Decimal("0.10")), repo)) as client:
        html = client.get("/").text
    assert 'data-tax-rate="0.10"' in html


def test_dashboard_without_build_time_says_n_a(repo: Repository) -> None:
    with TestClient(create_app(Settings(), repo)) as client:
        html = client.get("/").text
    assert "0.0.0-dev" in html
    assert "n/a" in html
    assert "unknown" in html  # sha placeholder


def test_non_persistent_banner_is_shown_for_the_memory_backend(
    settings: Settings, make_repo: object
) -> None:
    repo = make_repo("memory")  # type: ignore[operator]
    with TestClient(create_app(settings, repo)) as client:
        html = client.get("/").text
    assert 'id="persistence-banner"' in html
    assert "Demo data is not persistent" in html
    assert "reset whenever the service restarts or redeploys" in html


def test_banner_is_hidden_for_a_persistent_backend(settings: Settings, make_repo: object) -> None:
    repo = make_repo("sqlite")  # type: ignore[operator]
    with TestClient(create_app(settings, repo)) as client:
        html = client.get("/").text
    assert "persistence-banner" not in html
    assert "not persistent" not in html


def test_seed_products_metrics_and_categories_are_rendered(client: TestClient) -> None:
    html = client.get("/").text
    for name in ("Standard Widget", "Smart Gadget", "Cordless Drill", "Discontinued Gizmo"):
        assert name in html
    for sku in ("WID-001", "TOL-002", "OLD-001"):
        assert sku in html
    assert "$9.99" in html
    assert "$30,473.00" in html  # inventory value
    assert "Showing 10 products" in html
    for category in ("Widgets", "Gadgets", "Accessories", "Tools", "Packaging"):
        assert f'<option value="{category}">' in html
    assert "Out of stock" in html
    assert "Low stock" in html
    assert "Inactive" in html


def test_inactive_products_cannot_be_added_to_an_order_from_the_page(client: TestClient) -> None:
    html = client.get("/").text
    assert 'aria-label="Add Standard Widget to order"' in html
    assert "Add Discontinued Gizmo to order" not in html


def test_recent_orders_are_rendered_newest_first(client: TestClient) -> None:
    assert "No orders yet." in client.get("/").text
    for _ in range(3):
        client.post("/api/orders", json=order_payload((WID_001, 2)))
    html = client.get("/").text
    assert "$21.58" in html
    order_cells = re.findall(r'data-label="Order" class="strong">#(\d+)<', html)
    assert order_cells == ["3", "2", "1"]


def test_dashboard_shows_only_the_ten_most_recent_orders(client: TestClient) -> None:
    for _ in range(12):
        client.post("/api/orders", json=order_payload((WID_001, 1)))
    html = client.get("/").text
    order_cells = re.findall(r'data-label="Order" class="strong">#(\d+)<', html)
    assert order_cells == [str(i) for i in range(12, 2, -1)]


def test_dashboard_reflects_new_products_immediately(client: TestClient) -> None:
    client.post("/api/products", json=product_payload(name="Fresh Arrival", sku="FRESH-1"))
    html = client.get("/").text
    assert "Fresh Arrival" in html
    assert "Showing 11 products" in html


# ------------------------------------------------------------------ escaping
XSS = "<script>alert('xss')</script>"


def test_malicious_product_name_and_description_are_escaped(client: TestClient) -> None:
    created = client.post(
        "/api/products",
        json=product_payload(
            name=XSS,
            description='"><img src=x onerror=alert(1)>',
            category="<b>Evil</b>",
        ),
    )
    assert created.status_code == 201
    html = client.get("/").text
    assert "<script>alert('xss')</script>" not in html
    assert "&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;" in html
    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "<b>Evil</b>" not in html
    assert "&lt;b&gt;Evil&lt;/b&gt;" in html


def test_malicious_name_cannot_break_out_of_attributes(client: TestClient) -> None:
    client.post("/api/products", json=product_payload(name='x" onmouseover="alert(1)'))
    html = client.get("/").text
    assert 'onmouseover="alert(1)' not in html
    assert "&#34; onmouseover=&#34;alert(1)" in html


def test_page_has_no_inline_script_blocks_other_than_none(client: TestClient) -> None:
    """The page ships only a deferred external script; nothing inline can be injected into."""
    scripts = re.findall(r"<script\b[^>]*>", client.get("/").text)
    assert scripts == ['<script src="/static/js/app.js?v=9.9.9-test" defer>']


def test_malicious_settings_values_are_escaped(repo: Repository) -> None:
    hostile = Settings(deploy_environment="<i>prod</i>", app_version="<u>1</u>")
    with TestClient(create_app(hostile, repo)) as client:
        html = client.get("/").text
    assert "<i>prod</i>" not in html
    assert "&lt;i&gt;prod&lt;/i&gt;" in html
    assert "<u>1</u>" not in html


# ------------------------------------------------------------------ static assets
def test_stylesheet_is_served(client: TestClient) -> None:
    response = client.get("/static/css/app.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert len(response.text) > 500


def test_javascript_is_served(client: TestClient) -> None:
    response = client.get("/static/js/app.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "/api" in response.text  # the page talks to the JSON API


def test_assets_referenced_by_the_page_are_served(client: TestClient) -> None:
    html = client.get("/").text
    refs = re.findall(r'(?:href|src)="(/static/[^"?]+)', html)
    assert "/static/css/app.css" in refs
    assert "/static/js/app.js" in refs
    for ref in refs:
        assert client.get(ref).status_code == 200, ref


def test_page_has_no_external_assets(client: TestClient) -> None:
    html = client.get("/").text
    assert not re.findall(r'(?:href|src)="(?:https?:)?//', html)
    assert "cdn" not in html.lower()


def test_missing_static_file_is_a_json_404(client: TestClient) -> None:
    assert_error(client.get("/static/css/missing.css"), 404, "not_found")


@pytest.mark.parametrize(
    "path", ["/static/../main.py", "/static/%2e%2e/main.py", "/static/..%2fmain.py"]
)
def test_static_path_traversal_is_refused(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert "create_app" not in response.text
