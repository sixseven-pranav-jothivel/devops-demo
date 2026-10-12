"""Seed catalogue (ids 1..10 in this order)."""

from __future__ import annotations

from decimal import Decimal

from app.models.schemas import ProductCreate

SEED_PRODUCTS: tuple[ProductCreate, ...] = (
    ProductCreate(
        sku="WID-001",
        name="Standard Widget",
        description="General purpose widget.",
        price=Decimal("9.99"),
        quantity_in_stock=500,
        category="Widgets",
    ),
    ProductCreate(
        sku="WID-002",
        name="Premium Widget",
        description="Hardened widget for demanding jobs.",
        price=Decimal("24.50"),
        quantity_in_stock=200,
        category="Widgets",
    ),
    ProductCreate(
        sku="GAD-001",
        name="Smart Gadget",
        description="Connected gadget with a companion app.",
        price=Decimal("129.99"),
        quantity_in_stock=40,
        category="Gadgets",
    ),
    ProductCreate(
        sku="GAD-002",
        name="Gadget Pro",
        description="Professional grade gadget.",
        price=Decimal("349.00"),
        quantity_in_stock=15,
        category="Gadgets",
    ),
    ProductCreate(
        sku="CAB-001",
        name="USB-C Cable 1m",
        description="One metre USB-C charging cable.",
        price=Decimal("7.25"),
        quantity_in_stock=800,
        category="Accessories",
    ),
    ProductCreate(
        sku="CAB-002",
        name="HDMI Cable 2m",
        description="Two metre HDMI cable.",
        price=Decimal("12.80"),
        quantity_in_stock=3,
        category="Accessories",
    ),
    ProductCreate(
        sku="TOL-001",
        name="Precision Screwdriver Set",
        description="Set of precision screwdrivers.",
        price=Decimal("34.95"),
        quantity_in_stock=60,
        category="Tools",
    ),
    ProductCreate(
        sku="TOL-002",
        name="Cordless Drill",
        description="18V cordless drill.",
        price=Decimal("89.00"),
        quantity_in_stock=0,
        category="Tools",
    ),
    ProductCreate(
        sku="PKG-001",
        name="Shipping Box (25 pack)",
        description="Corrugated shipping boxes.",
        price=Decimal("18.40"),
        quantity_in_stock=120,
        category="Packaging",
    ),
    ProductCreate(
        sku="OLD-001",
        name="Discontinued Gizmo",
        description="No longer sold.",
        price=Decimal("5.00"),
        quantity_in_stock=25,
        category="Gadgets",
        active=False,
    ),
)
