"""Product endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import RepositoryDep, SettingsDep
from app.models.domain import Product
from app.models.schemas import ProductCreate, ProductList, ProductUpdate, StockAdjustment
from app.services import products as product_service

router = APIRouter(prefix="/api/products", tags=["products"])


@router.get("", response_model=ProductList)
def list_products(
    repository: RepositoryDep,
    settings: SettingsDep,
    *,
    q: str | None = None,
    category: str | None = None,
    active: bool | None = None,
    low_stock: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProductList:
    items, total = product_service.list_products(
        repository,
        q=q,
        category=category,
        active=active,
        low_stock=low_stock,
        low_stock_threshold=settings.low_stock_threshold,
        limit=limit,
        offset=offset,
    )
    return ProductList(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=Product, status_code=201)
def create_product(body: ProductCreate, repository: RepositoryDep, response: Response) -> Product:
    product = product_service.create_product(repository, body)
    response.headers["Location"] = f"/api/products/{product.id}"
    return product


@router.get("/{product_id}", response_model=Product)
def get_product(product_id: int, repository: RepositoryDep) -> Product:
    return product_service.get_product(repository, product_id)


@router.patch("/{product_id}", response_model=Product)
def update_product(product_id: int, body: ProductUpdate, repository: RepositoryDep) -> Product:
    return product_service.update_product(repository, product_id, body)


@router.post("/{product_id}/stock", response_model=Product)
def adjust_stock(product_id: int, body: StockAdjustment, repository: RepositoryDep) -> Product:
    return product_service.adjust_stock(repository, product_id, body)
