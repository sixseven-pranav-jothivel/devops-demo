"""Order endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import RepositoryDep, SettingsDep
from app.models.domain import Order
from app.models.schemas import OrderCreate, OrderList
from app.services import orders as order_service

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.post("", response_model=Order, status_code=201)
def create_order(
    body: OrderCreate, repository: RepositoryDep, settings: SettingsDep, response: Response
) -> Order:
    order = order_service.place_order(repository, body, tax_rate=settings.tax_rate)
    response.headers["Location"] = f"/api/orders/{order.id}"
    return order


@router.get("", response_model=OrderList)
def list_orders(
    repository: RepositoryDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OrderList:
    items, total = order_service.list_orders(repository, limit=limit, offset=offset)
    return OrderList(items=items, total=total, limit=limit, offset=offset)


@router.get("/{order_id}", response_model=Order)
def get_order(order_id: int, repository: RepositoryDep) -> Order:
    return order_service.get_order(repository, order_id)
