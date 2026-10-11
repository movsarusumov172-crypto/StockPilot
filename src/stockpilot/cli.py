"""Typer command-line interface."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer

from stockpilot.database import create_database_engine, session_scope
from stockpilot.importer import import_csv
from stockpilot.reports import export_excel, inventory_report, low_stock_report, turnover_report
from stockpilot.services import InventoryError, receive, ship, transfer

app = typer.Typer(help="Warehouse inventory with FIFO costing.")
report_app = typer.Typer(help="Inventory and turnover reports.")
app.add_typer(report_app, name="report")

# Typer has no ``date`` type, so dates are parsed as datetimes and truncated.
DATE_FORMAT = ["%Y-%m-%d"]


def _money(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise typer.BadParameter("Enter a valid decimal amount") from error
    if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
        raise typer.BadParameter("Amount must be non-negative with at most two decimal places")
    return amount


@app.command("receive")
def receive_command(
    sku: Annotated[str, typer.Option(help="Product SKU")],
    product_name: Annotated[str, typer.Option(help="Product name")],
    warehouse: Annotated[str, typer.Option(help="Destination warehouse")],
    quantity: Annotated[int, typer.Option(min=1)],
    unit_cost: Annotated[str, typer.Option(help="Purchase cost per unit")],
    received_on: Annotated[
        datetime, typer.Option(help="Receipt date (YYYY-MM-DD)", formats=DATE_FORMAT)
    ],
    min_stock: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """Receive a product batch."""
    engine = create_database_engine()
    with session_scope(engine) as session:
        try:
            with session.begin():
                batch = receive(
                    session,
                    sku=sku,
                    product_name=product_name,
                    warehouse_name=warehouse,
                    received_on=received_on.date(),
                    quantity=quantity,
                    unit_cost=_money(unit_cost),
                    min_stock=min_stock,
                )
        except (InventoryError, ValueError) as error:
            raise typer.BadParameter(str(error)) from error
    typer.echo(f"Received {batch.quantity_received} x {sku} at {warehouse}.")


@app.command("ship")
def ship_command(
    sku: Annotated[str, typer.Option(help="Product SKU")],
    warehouse: Annotated[str, typer.Option(help="Source warehouse")],
    quantity: Annotated[int, typer.Option(min=1)],
    shipped_on: Annotated[
        datetime, typer.Option(help="Shipment date (YYYY-MM-DD)", formats=DATE_FORMAT)
    ],
) -> None:
    """Ship units using FIFO and print their cost of goods sold."""
    engine = create_database_engine()
    with session_scope(engine) as session:
        try:
            with session.begin():
                cost = ship(
                    session,
                    sku=sku,
                    warehouse_name=warehouse,
                    shipped_on=shipped_on.date(),
                    quantity=quantity,
                )
        except InventoryError as error:
            raise typer.BadParameter(str(error)) from error
    typer.echo(f"Shipped {quantity} x {sku}; FIFO cost: {cost:.2f}.")


@app.command("transfer")
def transfer_command(
    sku: Annotated[str, typer.Option(help="Product SKU")],
    source: Annotated[str, typer.Option(help="Source warehouse")],
    destination: Annotated[str, typer.Option(help="Destination warehouse")],
    quantity: Annotated[int, typer.Option(min=1)],
    transferred_on: Annotated[
        datetime, typer.Option(help="Transfer date (YYYY-MM-DD)", formats=DATE_FORMAT)
    ],
) -> None:
    """Transfer units between warehouses while preserving batch costs."""
    engine = create_database_engine()
    with session_scope(engine) as session:
        try:
            with session.begin():
                cost = transfer(
                    session,
                    sku=sku,
                    source_name=source,
                    destination_name=destination,
                    transferred_on=transferred_on.date(),
                    quantity=quantity,
                )
        except InventoryError as error:
            raise typer.BadParameter(str(error)) from error
    typer.echo(f"Transferred {quantity} x {sku}; transferred value: {cost:.2f}.")


@app.command("import")
def import_command(file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """Import invoice CSV rows; invalid rows are saved beside the input file."""
    engine = create_database_engine()
    with session_scope(engine) as session:
        try:
            with session.begin():
                imported, error_path = import_csv(session, file)
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error
    typer.echo(f"Imported {imported} rows.")
    if error_path.exists():
        typer.echo(f"Invalid rows: {error_path}")


@report_app.command("inventory")
def inventory_command() -> None:
    """Show remaining stock by warehouse."""
    engine = create_database_engine()
    with session_scope(engine) as session:
        typer.echo(inventory_report(session).to_string(index=False))


@report_app.command("lowstock")
def lowstock_command() -> None:
    """Show products below their minimum stock level."""
    engine = create_database_engine()
    with session_scope(engine) as session:
        typer.echo(low_stock_report(session).to_string(index=False))


@report_app.command("turnover")
def turnover_command(
    start: Annotated[
        datetime, typer.Option("--from", help="Start date (YYYY-MM-DD)", formats=DATE_FORMAT)
    ],
    end: Annotated[
        datetime, typer.Option("--to", help="End date (YYYY-MM-DD)", formats=DATE_FORMAT)
    ],
) -> None:
    """Show cost of goods sold and turnover ratio for a date range."""
    engine = create_database_engine()
    with session_scope(engine) as session:
        typer.echo(turnover_report(session, start.date(), end.date()).to_string(index=False))


@report_app.command("excel")
def excel_command(
    output: Annotated[Path, typer.Option(help="Workbook output path")],
    start: Annotated[
        datetime, typer.Option("--from", help="Start date (YYYY-MM-DD)", formats=DATE_FORMAT)
    ],
    end: Annotated[
        datetime, typer.Option("--to", help="End date (YYYY-MM-DD)", formats=DATE_FORMAT)
    ],
) -> None:
    """Export inventory, low-stock, and turnover sheets to Excel."""
    engine = create_database_engine()
    with session_scope(engine) as session:
        export_excel(session, output, start.date(), end.date())
    typer.echo(f"Exported reports to {output}.")


if __name__ == "__main__":
    app()
