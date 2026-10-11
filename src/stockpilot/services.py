"""Inventory operations with transactional FIFO issue and warehouse transfer."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from stockpilot.models import Batch, Product, StockMovement, Warehouse
from stockpilot.schemas import InvoiceRow


class InventoryError(ValueError):
    """An inventory operation cannot be completed with the current stock."""


def _get_or_create_warehouse(session: Session, name: str) -> Warehouse:
    warehouse = session.scalar(select(Warehouse).where(Warehouse.name == name))
    if warehouse is None:
        warehouse = Warehouse(name=name)
        session.add(warehouse)
        session.flush()
    return warehouse


def _get_or_create_product(session: Session, sku: str, name: str, min_stock: int) -> Product:
    product = session.scalar(select(Product).where(Product.sku == sku))
    if product is None:
        product = Product(sku=sku, name=name, min_stock=min_stock)
        session.add(product)
        session.flush()
    else:
        product.name = name
        product.min_stock = min_stock
    return product


def receive(
    session: Session,
    *,
    sku: str,
    product_name: str,
    warehouse_name: str,
    received_on: date,
    quantity: int,
    unit_cost: Decimal,
    min_stock: int = 0,
) -> Batch:
    """Receive a lot and record its inventory value."""
    if quantity <= 0:
        raise InventoryError("Received quantity must be positive")
    if unit_cost < 0 or min_stock < 0:
        raise InventoryError("Unit cost and minimum stock cannot be negative")
    with session.begin_nested():
        product = _get_or_create_product(session, sku, product_name, min_stock)
        warehouse = _get_or_create_warehouse(session, warehouse_name)
        batch = Batch(
            product=product,
            warehouse=warehouse,
            received_on=received_on,
            quantity_received=quantity,
            quantity_remaining=quantity,
            unit_cost=unit_cost,
        )
        session.add(batch)
        session.add(
            StockMovement(
                product=product,
                warehouse=warehouse,
                occurred_on=received_on,
                kind="receive",
                quantity_delta=quantity,
                unit_cost=unit_cost,
            )
        )
        session.flush()
    return batch


def _consume_fifo(
    session: Session,
    *,
    product: Product,
    warehouse: Warehouse,
    quantity: int,
    occurred_on: date,
    movement_kind: str,
) -> list[tuple[int, Decimal, date]]:
    batches = session.scalars(
        select(Batch)
        .where(
            Batch.product_id == product.id,
            Batch.warehouse_id == warehouse.id,
            Batch.quantity_remaining > 0,
        )
        .order_by(Batch.received_on, Batch.id)
    ).all()
    available = sum(batch.quantity_remaining for batch in batches)
    if quantity <= 0:
        raise InventoryError("Quantity must be positive")
    if quantity > available:
        raise InventoryError(
            f"Cannot issue {quantity} units of {product.sku} from {warehouse.name}; "
            f"only {available} available"
        )

    remaining = quantity
    consumed: list[tuple[int, Decimal, date]] = []
    for batch in batches:
        taken = min(batch.quantity_remaining, remaining)
        if taken == 0:
            continue
        batch.quantity_remaining -= taken
        consumed.append((taken, batch.unit_cost, batch.received_on))
        session.add(
            StockMovement(
                product=product,
                warehouse=warehouse,
                occurred_on=occurred_on,
                kind=movement_kind,
                quantity_delta=-taken,
                unit_cost=batch.unit_cost,
            )
        )
        remaining -= taken
        if remaining == 0:
            break
    return consumed


def ship(
    session: Session,
    *,
    sku: str,
    warehouse_name: str,
    shipped_on: date,
    quantity: int,
) -> Decimal:
    """Issue oldest available lots and return their total FIFO cost."""
    with session.begin_nested():
        product = session.scalar(select(Product).where(Product.sku == sku))
        warehouse = session.scalar(select(Warehouse).where(Warehouse.name == warehouse_name))
        if product is None or warehouse is None:
            raise InventoryError(f"Unknown product or warehouse: {sku}, {warehouse_name}")
        consumed = _consume_fifo(
            session,
            product=product,
            warehouse=warehouse,
            quantity=quantity,
            occurred_on=shipped_on,
            movement_kind="ship",
        )
        session.flush()
    return sum((amount * cost for amount, cost, _ in consumed), start=Decimal("0.00"))


def transfer(
    session: Session,
    *,
    sku: str,
    source_name: str,
    destination_name: str,
    transferred_on: date,
    quantity: int,
) -> Decimal:
    """Move FIFO lots to another warehouse without changing their unit costs."""
    if source_name == destination_name:
        raise InventoryError("Source and destination warehouses must differ")
    with session.begin_nested():
        product = session.scalar(select(Product).where(Product.sku == sku))
        source = session.scalar(select(Warehouse).where(Warehouse.name == source_name))
        destination = _get_or_create_warehouse(session, destination_name)
        if product is None or source is None:
            raise InventoryError(f"Unknown product or source warehouse: {sku}, {source_name}")
        consumed = _consume_fifo(
            session,
            product=product,
            warehouse=source,
            quantity=quantity,
            occurred_on=transferred_on,
            movement_kind="transfer_out",
        )
        # Moved lots keep their original receipt date so FIFO order survives the transfer.
        for moved_quantity, unit_cost, received_on in consumed:
            session.add(
                Batch(
                    product=product,
                    warehouse=destination,
                    received_on=received_on,
                    quantity_received=moved_quantity,
                    quantity_remaining=moved_quantity,
                    unit_cost=unit_cost,
                )
            )
            session.add(
                StockMovement(
                    product=product,
                    warehouse=destination,
                    occurred_on=transferred_on,
                    kind="transfer_in",
                    quantity_delta=moved_quantity,
                    unit_cost=unit_cost,
                )
            )
        session.flush()
    return sum((amount * cost for amount, cost, _ in consumed), start=Decimal("0.00"))


def import_invoice_row(session: Session, row: InvoiceRow) -> None:
    """Apply one validated invoice row."""
    receive(
        session,
        sku=row.sku,
        product_name=row.product_name,
        warehouse_name=row.warehouse,
        received_on=row.date,
        quantity=row.quantity,
        unit_cost=row.unit_cost,
        min_stock=row.min_stock,
    )
