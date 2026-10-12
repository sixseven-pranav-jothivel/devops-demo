"""Server-rendered dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.deps import RepositoryDep, SettingsDep
from app.services import metrics as metrics_service
from app.services import orders as order_service
from app.services import products as product_service

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)

router = APIRouter(include_in_schema=False)

RECENT_ORDERS = 10


@router.get("/", response_class=HTMLResponse)
def index(request: Request, repository: RepositoryDep, settings: SettingsDep) -> HTMLResponse:
    products, _ = product_service.list_products(
        repository,
        q=None,
        category=None,
        active=None,
        low_stock=None,
        low_stock_threshold=settings.low_stock_threshold,
        limit=None,
        offset=0,
    )
    recent_orders, _ = order_service.list_orders(repository, limit=RECENT_ORDERS, offset=0)
    context = {
        "request": request,
        "app_version": settings.app_version,
        "git_sha": settings.git_sha,
        "deploy_environment": settings.deploy_environment,
        "build_time": settings.build_time,
        "persistent": repository.persistent,
        "tax_rate": str(settings.tax_rate),
        "products": products,
        "recent_orders": recent_orders,
        "metrics": metrics_service.compute_metrics(
            repository, low_stock_threshold=settings.low_stock_threshold
        ),
        "categories": sorted({p.category for p in products}),
    }
    return templates.TemplateResponse(request, "index.html", context)
