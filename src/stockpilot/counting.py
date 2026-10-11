"""CSV export, validation, preview, and application of physical inventory counts."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockpilot.models import Batch, Product, StockMovement, Warehouse
from stockpilot.schemas import InventoryCountRow
from stockpilot.services import CountAdjustment, InventoryError, apply_count_adjustments

COUNT_FIELDS = [
    "warehouse",
    "sku",
    "product_name",
    "expected_quantity",
    "counted_quantity",
    "unit_cost",
]
COUNT_FIELD_LABELS = {
    "warehouse": "склад",
    "sku": "артикул",
    "product_name": "название товара",
    "expected_quantity": "ожидаемое количество",
    "counted_quantity": "фактическое количество",
    "unit_cost": "цена за единицу",
}


@dataclass(frozen=True)
class CountPreview:
    """A validated count line and its money impact."""

    adjustment: CountAdjustment
    amount: Decimal


def export_count(session: Session, warehouse_name: str, output: Path) -> None:
    """Export expected quantities and acquisition cost hints for one warehouse."""
    warehouse = session.scalar(select(Warehouse).where(Warehouse.name == warehouse_name))
    if warehouse is None:
        raise InventoryError(f"Unknown warehouse: {warehouse_name}")

    products = session.scalars(select(Product).order_by(Product.sku)).all()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=COUNT_FIELDS)
        writer.writeheader()
        for product in products:
            batches = session.scalars(
                select(Batch)
                .where(
                    Batch.product_id == product.id,
                    Batch.warehouse_id == warehouse.id,
                    Batch.quantity_remaining > 0,
                )
                .order_by(Batch.received_on, Batch.id)
            ).all()
            expected_quantity = sum(batch.quantity_remaining for batch in batches)
            if expected_quantity:
                inventory_value = sum(
                    (batch.quantity_remaining * batch.unit_cost for batch in batches),
                    start=Decimal("0.00"),
                )
                unit_cost = (inventory_value / expected_quantity).quantize(Decimal("0.01"))
            else:
                last_cost = session.scalar(
                    select(StockMovement.unit_cost)
                    .where(
                        StockMovement.product_id == product.id,
                        StockMovement.quantity_delta > 0,
                    )
                    .order_by(StockMovement.occurred_on.desc(), StockMovement.id.desc())
                    .limit(1)
                )
                unit_cost = last_cost or Decimal("0.00")
            writer.writerow(
                {
                    "warehouse": warehouse.name,
                    "sku": product.sku,
                    "product_name": product.name,
                    "expected_quantity": expected_quantity,
                    "counted_quantity": "",
                    "unit_cost": f"{unit_cost:.2f}",
                }
            )


def _fifo_cost(session: Session, product: Product, warehouse: Warehouse, quantity: int) -> Decimal:
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
    if quantity > available:
        raise InventoryError(f"Stock changed during count; only {available} units remain")
    remaining = quantity
    total = Decimal("0.00")
    for batch in batches:
        taken = min(batch.quantity_remaining, remaining)
        total += taken * batch.unit_cost
        remaining -= taken
        if remaining == 0:
            break
    return total


def prepare_count(session: Session, input_path: Path) -> tuple[str, list[CountPreview]]:
    """Validate a count file and compare it with the current database snapshot."""
    try:
        with input_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            if reader.fieldnames is None or not set(COUNT_FIELDS).issubset(reader.fieldnames):
                raise ValueError("В CSV пересчёта отсутствуют обязательные столбцы")
            rows = [InventoryCountRow.model_validate(row) for row in reader]
    except ValidationError as error:
        detail = error.errors()[0]
        field_name = str(detail["loc"][0]) if detail["loc"] else "строка"
        field_name = COUNT_FIELD_LABELS.get(field_name, field_name)
        raise ValueError(f"Ошибка в CSV пересчёта, поле {field_name}") from error

    if not rows:
        raise ValueError("Файл пересчёта не содержит строк")
    warehouses = {row.warehouse for row in rows}
    if len(warehouses) != 1:
        raise ValueError("Один файл пересчёта должен относиться к одному складу")
    warehouse_name = rows[0].warehouse
    warehouse = session.scalar(select(Warehouse).where(Warehouse.name == warehouse_name))
    if warehouse is None:
        raise InventoryError(f"Unknown warehouse: {warehouse_name}")

    seen_skus: set[str] = set()
    previews: list[CountPreview] = []
    for row in rows:
        if row.sku in seen_skus:
            raise ValueError(f"Артикул {row.sku} повторяется в файле пересчёта")
        seen_skus.add(row.sku)
        product = session.scalar(select(Product).where(Product.sku == row.sku))
        if product is None:
            raise InventoryError(f"Unknown product: {row.sku}")
        current_quantity = sum(
            session.scalars(
                select(Batch.quantity_remaining).where(
                    Batch.product_id == product.id,
                    Batch.warehouse_id == warehouse.id,
                    Batch.quantity_remaining > 0,
                )
            ).all()
        )
        if current_quantity != row.expected_quantity:
            raise InventoryError(
                f"Остаток {row.sku} изменился после выгрузки: "
                f"в файле {row.expected_quantity}, сейчас {current_quantity}"
            )

        adjustment = CountAdjustment(
            sku=row.sku,
            product_name=product.name,
            expected_quantity=row.expected_quantity,
            counted_quantity=row.counted_quantity,
            unit_cost=row.unit_cost,
        )
        if adjustment.difference < 0:
            amount = _fifo_cost(session, product, warehouse, -adjustment.difference)
        else:
            amount = adjustment.difference * adjustment.unit_cost
        previews.append(CountPreview(adjustment=adjustment, amount=amount))
    catalog_skus = set(session.scalars(select(Product.sku)).all())
    missing_skus = catalog_skus - seen_skus
    if missing_skus:
        missing_list = ", ".join(sorted(missing_skus))
        raise ValueError(f"В файле пересчёта отсутствуют товары: {missing_list}")
    return warehouse_name, previews


def apply_prepared_count(
    session: Session,
    warehouse_name: str,
    previews: list[CountPreview],
    counted_on: date,
) -> list[CountPreview]:
    """Apply count adjustments after the caller has shown and confirmed the preview."""
    adjustments = [preview.adjustment for preview in previews]
    apply_count_adjustments(
        session,
        warehouse_name=warehouse_name,
        counted_on=counted_on,
        adjustments=adjustments,
    )
    return previews
