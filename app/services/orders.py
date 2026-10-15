"""Order use-cases. Placement is one atomic unit of work."""

from __future__ import annotations

from decimal import Decimal

from app.models.domain import Order
from app.models.errors import OrderNotFoundError
from app.models.schemas import NewOrder, NewOrderItem, OrderCreate
from app.repository.base import Repository
from app.services import inventory, pricing


def place_order(repo: Repository, request: OrderCreate, *, tax_rate: Decimal) -> Order:
    """Validate, price, decrement stock and store the order atomically.

    All validation happens before any write; any error leaves stock and orders untouched.
    """
    with repo.unit_of_work() as uow:
        products = {}
        for line in request.line_items:
            product = uow.get_product(line.product_id)
            if product is not None:
                products[line.product_id] = product
        inventory.collect_order_problems(request.line_items, products)

        items = [
            NewOrderItem(
                product_id=line.product_id,
                sku=products[line.product_id].sku,
                product_name=products[line.product_id].name,
                quantity=line.quantity,
                unit_price=products[line.product_id].price,
                line_total=pricing.calculate_line_total(
                    products[line.product_id].price, line.quantity
                ),
            )
            for line in request.line_items
        ]
        totals = pricing.price_order([item.line_total for item in items], tax_rate)
        for item in items:
            uow.decrement_stock(item.product_id, item.quantity)
        return uow.add_order(
            NewOrder(
                line_items=items,
                subtotal=totals.subtotal,
                discount=totals.discount,
                tax=totals.tax,
                total=totals.total,
            )
        )


def get_order(repo: Repository, order_id: int) -> Order:
    order = repo.get_order(order_id)
    if order is None:
        raise OrderNotFoundError(f"Order {order_id} not found.", [{"order_id": order_id}])
    return order


def list_orders(repo: Repository, *, limit: int, offset: int) -> tuple[list[Order], int]:
    return repo.list_orders(limit=limit, offset=offset)
