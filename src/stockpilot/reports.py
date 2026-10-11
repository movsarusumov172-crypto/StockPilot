"""Pandas-backed inventory and turnover reports."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockpilot.models import Batch, Product, StockMovement, Warehouse


def inventory_report(session: Session) -> pd.DataFrame:
    """Return on-hand units and cost value by warehouse and product."""
    rows = session.execute(
        select(
            Warehouse.name,
            Product.sku,
            Product.name,
            Product.min_stock,
            Batch.quantity_remaining,
            Batch.unit_cost,
        )
        .join(Batch, Batch.warehouse_id == Warehouse.id)
        .join(Product, Batch.product_id == Product.id)
        .where(Batch.quantity_remaining > 0)
    ).all()
    frame = pd.DataFrame(
        [
            {
                "warehouse": warehouse,
                "sku": sku,
                "product": product_name,
                "min_stock": min_stock,
                "quantity": quantity,
                "inventory_value": Decimal(quantity) * cost,
            }
            for warehouse, sku, product_name, min_stock, quantity, cost in rows
        ],
        columns=["warehouse", "sku", "product", "min_stock", "quantity", "inventory_value"],
    )
    if frame.empty:
        return frame
    return frame.groupby(["warehouse", "sku", "product", "min_stock"], as_index=False).agg(
        quantity=("quantity", "sum"), inventory_value=("inventory_value", "sum")
    )


def low_stock_report(session: Session) -> pd.DataFrame:
    """Return products below the minimum across all warehouses."""
    stock = inventory_report(session)
    products = session.scalars(select(Product).order_by(Product.sku)).all()
    quantities = stock.groupby("sku")["quantity"].sum().to_dict() if not stock.empty else {}
    rows = [
        {
            "sku": product.sku,
            "product": product.name,
            "quantity": quantities.get(product.sku, 0),
            "min_stock": product.min_stock,
        }
        for product in products
        if quantities.get(product.sku, 0) < product.min_stock
    ]
    return pd.DataFrame(rows, columns=["sku", "product", "quantity", "min_stock"])


def turnover_report(session: Session, start: date, end: date) -> pd.DataFrame:
    """Calculate period COGS divided by average opening and closing stock value."""
    if start > end:
        raise ValueError("Start date must be on or before end date")
    products = session.scalars(select(Product).order_by(Product.sku)).all()
    rows: list[dict[str, object]] = []
    for product in products:
        movements = session.scalars(
            select(StockMovement)
            .where(StockMovement.product_id == product.id)
            .order_by(StockMovement.occurred_on, StockMovement.id)
        ).all()
        opening_value = Decimal("0.00")
        closing_value = Decimal("0.00")
        cogs = Decimal("0.00")
        for movement in movements:
            value_delta = Decimal(movement.quantity_delta) * movement.unit_cost
            if movement.occurred_on < start:
                opening_value += value_delta
            if movement.occurred_on <= end:
                closing_value += value_delta
            if start <= movement.occurred_on <= end and movement.kind == "ship":
                cogs += -value_delta
        average_inventory = (opening_value + closing_value) / 2
        ratio = float(cogs / average_inventory) if average_inventory > 0 else 0.0
        rows.append(
            {
                "sku": product.sku,
                "product": product.name,
                "cogs": cogs,
                "average_inventory_value": average_inventory,
                "turnover_ratio": ratio,
            }
        )
    return pd.DataFrame(
        rows,
        columns=["sku", "product", "cogs", "average_inventory_value", "turnover_ratio"],
    )


def export_excel(session: Session, path: Path, start: date, end: date) -> None:
    """Write inventory, low-stock, and turnover reports to one workbook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        inventory_report(session).to_excel(writer, sheet_name="Inventory", index=False)
        low_stock_report(session).to_excel(writer, sheet_name="Low stock", index=False)
        turnover_report(session, start, end).to_excel(writer, sheet_name="Turnover", index=False)
