"""Transactional inventory operations and FIFO lot costing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from stockpilot.models import (
    Batch,
    InventoryCount,
    InventoryCountLine,
    InvoiceImport,
    Product,
    Shipment,
    ShipmentLine,
    StockMovement,
    Warehouse,
)
from stockpilot.schemas import InvoiceRow


class InventoryError(ValueError):
    """An inventory operation cannot be completed with the current stock."""


class WriteOffReason(StrEnum):
    """Supported reasons for a stock loss."""

    DEFECT = "брак"
    SPOILAGE = "порча"
    SHORTAGE = "недостача"
    MIS_SORT = "пересорт"


@dataclass(frozen=True)
class LotSlice:
    """A quantity taken from a specific costed batch."""

    quantity: int
    unit_cost: Decimal
    received_on: date


@dataclass(frozen=True)
class CountAdjustment:
    """A confirmed stock count difference for one product and warehouse."""

    sku: str
    product_name: str
    expected_quantity: int
    counted_quantity: int
    unit_cost: Decimal

    @property
    def difference(self) -> int:
        return self.counted_quantity - self.expected_quantity


class InvoiceAlreadyImportedError(ValueError):
    """An invoice number or file digest already exists in the import ledger."""

    def __init__(self, invoice_number: str, *, file_hash_match: bool = False) -> None:
        self.invoice_number = invoice_number
        self.file_hash_match = file_hash_match
        super().__init__(invoice_number)


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


def _record_receipt(
    session: Session,
    *,
    product: Product,
    warehouse: Warehouse,
    received_on: date,
    quantity: int,
    unit_cost: Decimal,
    movement_on: date | None = None,
    movement_kind: str = "receive",
    invoice_import: InvoiceImport | None = None,
    inventory_count: InventoryCount | None = None,
) -> Batch:
    batch = Batch(
        product=product,
        warehouse=warehouse,
        received_on=received_on,
        quantity_received=quantity,
        quantity_remaining=quantity,
        unit_cost=unit_cost,
        invoice_import=invoice_import,
    )
    session.add(batch)
    session.add(
        StockMovement(
            product=product,
            warehouse=warehouse,
            occurred_on=movement_on or received_on,
            kind=movement_kind,
            quantity_delta=quantity,
            unit_cost=unit_cost,
            inventory_count_id=inventory_count.id if inventory_count else None,
        )
    )
    session.flush()
    return batch


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
        batch = _record_receipt(
            session,
            product=product,
            warehouse=warehouse,
            received_on=received_on,
            quantity=quantity,
            unit_cost=unit_cost,
        )
    return batch


def _consume_fifo(
    session: Session,
    *,
    product: Product,
    warehouse: Warehouse,
    quantity: int,
) -> list[LotSlice]:
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
    consumed: list[LotSlice] = []
    for batch in batches:
        taken = min(batch.quantity_remaining, remaining)
        if taken == 0:
            continue
        batch.quantity_remaining -= taken
        consumed.append(
            LotSlice(
                quantity=taken,
                unit_cost=batch.unit_cost,
                received_on=batch.received_on,
            )
        )
        remaining -= taken
        if remaining == 0:
            break
    return consumed


def ship_with_reference(
    session: Session,
    *,
    sku: str,
    warehouse_name: str,
    shipped_on: date,
    quantity: int,
) -> Shipment:
    """Create a shipment and its costed lot lines for future customer returns."""
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
        )
        cogs = sum((lot.quantity * lot.unit_cost for lot in consumed), start=Decimal("0.00"))
        shipment = Shipment(
            product=product,
            warehouse=warehouse,
            occurred_on=shipped_on,
            quantity=quantity,
            cogs=cogs,
        )
        session.add(shipment)
        session.flush()
        for lot in consumed:
            shipment_line = ShipmentLine(
                shipment=shipment,
                quantity=lot.quantity,
                unit_cost=lot.unit_cost,
            )
            session.add(shipment_line)
            session.flush()
            session.add(
                StockMovement(
                    product=product,
                    warehouse=warehouse,
                    occurred_on=shipped_on,
                    kind="ship",
                    quantity_delta=-lot.quantity,
                    unit_cost=lot.unit_cost,
                    shipment_id=shipment.id,
                    shipment_line_id=shipment_line.id,
                )
            )
        session.flush()
    return shipment


def ship(
    session: Session,
    *,
    sku: str,
    warehouse_name: str,
    shipped_on: date,
    quantity: int,
) -> Decimal:
    """Issue oldest lots first and return their total FIFO cost."""
    shipment = ship_with_reference(
        session,
        sku=sku,
        warehouse_name=warehouse_name,
        shipped_on=shipped_on,
        quantity=quantity,
    )
    return shipment.cogs


def write_off(
    session: Session,
    *,
    sku: str,
    warehouse_name: str,
    occurred_on: date,
    quantity: int,
    reason: WriteOffReason,
    inventory_count: InventoryCount | None = None,
) -> Decimal:
    """Consume FIFO lots and record their cost as a non-sales loss."""
    try:
        reason = WriteOffReason(reason)
    except ValueError as error:
        raise InventoryError("Unsupported write-off reason") from error

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
        )
        for lot in consumed:
            session.add(
                StockMovement(
                    product=product,
                    warehouse=warehouse,
                    occurred_on=occurred_on,
                    kind="write_off",
                    quantity_delta=-lot.quantity,
                    unit_cost=lot.unit_cost,
                    reason=reason.value,
                    inventory_count_id=inventory_count.id if inventory_count else None,
                )
            )
        session.flush()
    return sum((lot.quantity * lot.unit_cost for lot in consumed), start=Decimal("0.00"))


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
        )
        for lot in consumed:
            session.add(
                StockMovement(
                    product=product,
                    warehouse=source,
                    occurred_on=transferred_on,
                    kind="transfer_out",
                    quantity_delta=-lot.quantity,
                    unit_cost=lot.unit_cost,
                )
            )
            session.flush()
            _record_receipt(
                session,
                product=product,
                warehouse=destination,
                received_on=lot.received_on,
                quantity=lot.quantity,
                unit_cost=lot.unit_cost,
                movement_on=transferred_on,
                movement_kind="transfer_in",
            )
        session.flush()
    return sum((lot.quantity * lot.unit_cost for lot in consumed), start=Decimal("0.00"))


def return_shipment(
    session: Session,
    *,
    shipment_id: int,
    returned_on: date,
    quantity: int,
) -> Decimal:
    """Return units from a referenced shipment at their original FIFO costs."""
    if quantity <= 0:
        raise InventoryError("Quantity must be positive")
    with session.begin_nested():
        shipment = session.get(Shipment, shipment_id)
        if shipment is None:
            raise InventoryError(f"Shipment {shipment_id} was not found")
        available_to_return = sum(line.quantity - line.returned_quantity for line in shipment.lines)
        if quantity > available_to_return:
            raise InventoryError(
                f"Shipment {shipment_id} has only {available_to_return} units available to return"
            )

        remaining = quantity
        return_cost = Decimal("0.00")
        for line in sorted(shipment.lines, key=lambda item: item.id):
            returned = min(line.quantity - line.returned_quantity, remaining)
            if returned <= 0:
                continue
            line.returned_quantity += returned
            return_cost += returned * line.unit_cost
            session.add(
                Batch(
                    product=shipment.product,
                    warehouse=shipment.warehouse,
                    received_on=returned_on,
                    quantity_received=returned,
                    quantity_remaining=returned,
                    unit_cost=line.unit_cost,
                )
            )
            session.add(
                StockMovement(
                    product=shipment.product,
                    warehouse=shipment.warehouse,
                    occurred_on=returned_on,
                    kind="return",
                    quantity_delta=returned,
                    unit_cost=line.unit_cost,
                    shipment_id=shipment.id,
                    shipment_line_id=line.id,
                )
            )
            remaining -= returned
            if remaining == 0:
                break
        session.flush()
    return return_cost


def apply_count_adjustments(
    session: Session,
    *,
    warehouse_name: str,
    counted_on: date,
    adjustments: list[CountAdjustment],
) -> InventoryCount:
    """Persist count results and adjust stock through FIFO receipts or shortage write-offs."""
    with session.begin_nested():
        warehouse = session.scalar(select(Warehouse).where(Warehouse.name == warehouse_name))
        if warehouse is None:
            raise InventoryError(f"Unknown warehouse: {warehouse_name}")
        count = InventoryCount(warehouse=warehouse, occurred_on=counted_on)
        session.add(count)
        session.flush()
        for adjustment in adjustments:
            product = session.scalar(select(Product).where(Product.sku == adjustment.sku))
            if product is None:
                raise InventoryError(f"Unknown product: {adjustment.sku}")
            amount = Decimal("0.00")
            if adjustment.difference > 0:
                amount = adjustment.difference * adjustment.unit_cost
                _record_receipt(
                    session,
                    product=product,
                    warehouse=warehouse,
                    received_on=counted_on,
                    quantity=adjustment.difference,
                    unit_cost=adjustment.unit_cost,
                    movement_kind="count_in",
                    inventory_count=count,
                )
            elif adjustment.difference < 0:
                amount = write_off(
                    session,
                    sku=adjustment.sku,
                    warehouse_name=warehouse_name,
                    occurred_on=counted_on,
                    quantity=-adjustment.difference,
                    reason=WriteOffReason.SHORTAGE,
                    inventory_count=count,
                )
            session.add(
                InventoryCountLine(
                    inventory_count=count,
                    product=product,
                    expected_quantity=adjustment.expected_quantity,
                    counted_quantity=adjustment.counted_quantity,
                    difference=adjustment.difference,
                    unit_cost=adjustment.unit_cost,
                    amount=amount,
                )
            )
        session.flush()
    return count


def import_invoice_row(
    session: Session,
    row: InvoiceRow,
    invoice_import: InvoiceImport,
) -> None:
    """Apply one validated invoice row and link its lot to the invoice record."""
    with session.begin_nested():
        product = _get_or_create_product(session, row.sku, row.product_name, row.min_stock)
        warehouse = _get_or_create_warehouse(session, row.warehouse)
        _record_receipt(
            session,
            product=product,
            warehouse=warehouse,
            received_on=row.date,
            quantity=row.quantity,
            unit_cost=row.unit_cost,
            invoice_import=invoice_import,
        )
