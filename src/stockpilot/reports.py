"""Pandas-backed inventory and turnover reports."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockpilot.models import Batch, Product, StockMovement, Warehouse


def _validate_warehouse(session: Session, warehouse_name: str | None) -> None:
    if warehouse_name is None:
        return
    warehouse_exists = session.scalar(select(Warehouse.id).where(Warehouse.name == warehouse_name))
    if warehouse_exists is None:
        raise ValueError(f"Склад не найден: {warehouse_name}")


def inventory_report(session: Session, warehouse_name: str | None = None) -> pd.DataFrame:
    """Return on-hand units and cost value by warehouse and product."""
    _validate_warehouse(session, warehouse_name)
    statement = (
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
    )
    if warehouse_name is not None:
        statement = statement.where(Warehouse.name == warehouse_name)
    rows = session.execute(statement).all()
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


def low_stock_report(session: Session, warehouse_name: str | None = None) -> pd.DataFrame:
    """Return products below their minimum across all warehouses or one selected warehouse."""
    stock = inventory_report(session, warehouse_name)
    products = session.scalars(select(Product).order_by(Product.sku)).all()
    quantities = stock.groupby("sku")["quantity"].sum().to_dict() if not stock.empty else {}
    rows = [
        {
            "warehouse": warehouse_name or "Все склады",
            "sku": product.sku,
            "product": product.name,
            "quantity": quantities.get(product.sku, 0),
            "min_stock": product.min_stock,
        }
        for product in products
        if quantities.get(product.sku, 0) < product.min_stock
    ]
    return pd.DataFrame(
        rows,
        columns=["warehouse", "sku", "product", "quantity", "min_stock"],
    )


def products_report(session: Session, search: str | None = None) -> pd.DataFrame:
    """List catalog products with a balance for each warehouse."""
    product_statement = select(Product).order_by(Product.sku)
    if search:
        product_statement = product_statement.where(
            Product.sku.ilike(f"%{search}%") | Product.name.ilike(f"%{search}%")
        )
    products = session.scalars(product_statement).all()
    warehouses = session.scalars(select(Warehouse).order_by(Warehouse.name)).all()
    stock = inventory_report(session)
    quantities = (
        {(row.warehouse, row.sku): row.quantity for row in stock.itertuples(index=False)}
        if not stock.empty
        else {}
    )
    rows = [
        {
            "sku": product.sku,
            "product": product.name,
            "warehouse": warehouse.name,
            "quantity": quantities.get((warehouse.name, product.sku), 0),
            "min_stock": product.min_stock,
        }
        for product in products
        for warehouse in warehouses
    ]
    return pd.DataFrame(
        rows,
        columns=["sku", "product", "warehouse", "quantity", "min_stock"],
    )


def history_report(session: Session, sku: str) -> pd.DataFrame:
    """List every SKU movement with the warehouse balance after that movement."""
    product = session.scalar(select(Product).where(Product.sku == sku))
    if product is None:
        raise ValueError(f"Unknown SKU: {sku}")
    movements = session.scalars(
        select(StockMovement)
        .join(Warehouse, StockMovement.warehouse_id == Warehouse.id)
        .where(StockMovement.product_id == product.id)
        .order_by(StockMovement.occurred_on, StockMovement.id)
    ).all()
    balances: dict[str, int] = {}
    rows: list[dict[str, object]] = []
    for movement in movements:
        warehouse_name = movement.warehouse.name
        balances[warehouse_name] = balances.get(warehouse_name, 0) + movement.quantity_delta
        rows.append(
            {
                "date": movement.occurred_on,
                "warehouse": warehouse_name,
                "kind": movement.kind,
                "quantity_delta": movement.quantity_delta,
                "unit_cost": movement.unit_cost,
                "balance_after": balances[warehouse_name],
                "reason": movement.reason or "",
                "shipment_id": movement.shipment_id or "",
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "date",
            "warehouse",
            "kind",
            "quantity_delta",
            "unit_cost",
            "balance_after",
            "reason",
            "shipment_id",
        ],
    )


def turnover_report(
    session: Session,
    start: date,
    end: date,
    warehouse_name: str | None = None,
) -> pd.DataFrame:
    """Calculate period COGS divided by average opening and closing stock value."""
    _validate_warehouse(session, warehouse_name)
    if start > end:
        raise ValueError("Начальная дата должна быть не позже конечной")
    products = session.scalars(select(Product).order_by(Product.sku)).all()
    rows: list[dict[str, object]] = []
    for product in products:
        statement = (
            select(StockMovement)
            .join(Warehouse, StockMovement.warehouse_id == Warehouse.id)
            .where(StockMovement.product_id == product.id)
            .order_by(StockMovement.occurred_on, StockMovement.id)
        )
        if warehouse_name is not None:
            statement = statement.where(Warehouse.name == warehouse_name)
        movements = session.scalars(statement).all()
        opening_value = Decimal("0.00")
        closing_value = Decimal("0.00")
        cogs = Decimal("0.00")
        for movement in movements:
            value_delta = Decimal(movement.quantity_delta) * movement.unit_cost
            if movement.occurred_on < start:
                opening_value += value_delta
            if movement.occurred_on <= end:
                closing_value += value_delta
            if start <= movement.occurred_on <= end and movement.kind in {"ship", "return"}:
                cogs += -value_delta
        average_inventory = (opening_value + closing_value) / 2
        ratio = cogs / average_inventory if average_inventory > 0 else Decimal("0.00")
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


def loss_report(session: Session, warehouse_name: str | None = None) -> pd.DataFrame:
    """List write-offs with reasons and their FIFO acquisition value."""
    _validate_warehouse(session, warehouse_name)
    statement = (
        select(
            StockMovement.occurred_on,
            Warehouse.name,
            Product.sku,
            Product.name,
            StockMovement.reason,
            StockMovement.quantity_delta,
            StockMovement.unit_cost,
        )
        .join(Product, StockMovement.product_id == Product.id)
        .join(Warehouse, StockMovement.warehouse_id == Warehouse.id)
        .where(StockMovement.kind == "write_off")
        .order_by(StockMovement.occurred_on, StockMovement.id)
    )
    if warehouse_name is not None:
        statement = statement.where(Warehouse.name == warehouse_name)
    rows = session.execute(statement).all()
    return pd.DataFrame(
        [
            {
                "date": occurred_on,
                "warehouse": warehouse,
                "sku": sku,
                "product": product_name,
                "reason": reason,
                "quantity": -quantity_delta,
                "amount": -quantity_delta * unit_cost,
            }
            for occurred_on, warehouse, sku, product_name, reason, quantity_delta, unit_cost in rows
        ],
        columns=["date", "warehouse", "sku", "product", "reason", "quantity", "amount"],
    )


def export_excel(
    session: Session,
    path: Path,
    start: date,
    end: date,
    warehouse_name: str | None = None,
) -> None:
    """Write inventory, low-stock, turnover, and losses reports to one workbook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        inventory_report(session, warehouse_name).to_excel(
            writer, sheet_name="Остатки", index=False
        )
        low_stock_report(session, warehouse_name).to_excel(
            writer, sheet_name="Низкий запас", index=False
        )
        turnover_report(session, start, end, warehouse_name).to_excel(
            writer, sheet_name="Оборачиваемость", index=False
        )
        loss_report(session, warehouse_name).to_excel(writer, sheet_name="Потери", index=False)
