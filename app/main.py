"""Application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import errors, health, meta, orders, products, web
from app.config import Settings, get_settings
from app.repository.base import Repository
from app.repository.factory import build_repository

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: Settings | None = None, repository: Repository | None = None) -> FastAPI:
    """Build the app. Tests pass their own `Settings` and repository."""
    resolved = settings if settings is not None else get_settings()
    app = FastAPI(
        title="StockFlow",
        version=resolved.app_version,
        description="Inventory and order management API.",
    )
    app.state.settings = resolved
    app.state.repository = repository if repository is not None else build_repository(resolved)

    errors.install_error_handlers(app)
    for module in (health, meta, products, orders, web):
        app.include_router(module.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app: FastAPI = create_app()
