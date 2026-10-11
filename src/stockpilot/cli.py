"""Russian-language Typer command-line interface."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockpilot.counting import CountPreview, apply_prepared_count, export_count, prepare_count
from stockpilot.database import create_database_engine, session_scope
from stockpilot.importer import import_csv
from stockpilot.models import Product, Warehouse
from stockpilot.reports import (
    export_excel,
    history_report,
    inventory_report,
    loss_report,
    low_stock_report,
    products_report,
    turnover_report,
)
from stockpilot.services import (
    InventoryError,
    InvoiceAlreadyImportedError,
    WriteOffReason,
    receive,
    return_shipment,
    ship_with_reference,
    transfer,
    write_off,
)

app = typer.Typer(help="Складской учёт StockPilot", no_args_is_help=True)
report_app = typer.Typer(help="Отчёты по остаткам и движению товаров", no_args_is_help=True)
count_app = typer.Typer(help="Инвентаризация склада", no_args_is_help=True)
app.add_typer(report_app, name="report")
app.add_typer(count_app, name="count")
console = Console()

DATE_FORMAT = ["%Y-%m-%d"]
COLUMN_LABELS = {
    "warehouse": "Склад",
    "sku": "Артикул",
    "product": "Товар",
    "product_name": "Товар",
    "min_stock": "Мин. запас",
    "quantity": "Количество",
    "inventory_value": "Стоимость остатка",
    "date": "Дата",
    "kind": "Операция",
    "quantity_delta": "Изменение",
    "unit_cost": "Цена",
    "balance_after": "Остаток после",
    "reason": "Причина",
    "shipment_id": "Отгрузка №",
    "cogs": "Себестоимость продаж",
    "average_inventory_value": "Средний остаток",
    "turnover_ratio": "Оборачиваемость",
    "amount": "Сумма",
    "expected_quantity": "План",
    "counted_quantity": "Факт",
    "difference": "Разница",
}


@contextmanager
def _session() -> Iterator[Session]:
    engine = create_database_engine()
    try:
        with session_scope(engine) as database_session:
            yield database_session
    finally:
        engine.dispose()


def _money(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise typer.BadParameter("Введите денежную сумму числом") from error
    if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
        raise typer.BadParameter(
            "Сумма должна быть неотрицательной, максимум два знака после запятой"
        )
    return amount


def _message(error: Exception) -> str:
    if isinstance(error, InvoiceAlreadyImportedError):
        if error.file_hash_match:
            return f"Этот файл уже импортирован как накладная {error.invoice_number}"
        return f"Накладная {error.invoice_number} уже импортирована"
    message = str(error)
    if "only" in message and "available" in message:
        available = message.rsplit("only ", 1)[-1].split(" available", 1)[0]
        return f"Недостаточно товара на складе. Доступно: {available}"
    if message.startswith("Unknown product or warehouse:"):
        return "Не найден товар или склад"
    if message.startswith("Unknown warehouse:"):
        return "Склад не найден"
    if message.startswith("Склад не найден:"):
        return message
    if message.startswith("Unknown product:") or message.startswith("Unknown SKU:"):
        return "Товар не найден"
    if message == "Начальная дата должна быть не позже конечной":
        return message
    if message.startswith("Unknown product or source warehouse:"):
        return "Не найден товар или исходный склад"
    if message.startswith("Shipment "):
        if "was not found" in message:
            return "Отгрузка не найдена"
        available = message.rsplit("only ", 1)[-1].split(" units", 1)[0]
        return f"Нельзя вернуть больше оставшегося количества: доступно {available}"
    if message == "Source and destination warehouses must differ":
        return "Исходный и целевой склады должны различаться"
    if message == "Quantity must be positive":
        return "Количество должно быть больше нуля"
    if message == "Received quantity must be positive":
        return "Количество прихода должно быть больше нуля"
    return message


def _show_table(
    frame: pd.DataFrame,
    title: str,
    *,
    csv_path: Path | None = None,
    emphasize_low_stock: bool = False,
    emphasize_difference: bool = False,
) -> None:
    if csv_path is not None:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
        typer.echo(f"CSV сохранён: {csv_path}")
        return

    table = Table(title=title, show_lines=False)
    columns = list(frame.columns)
    for column in columns:
        table.add_column(COLUMN_LABELS.get(column, column))
    for row in frame.itertuples(index=False, name=None):
        style = ""
        values = dict(zip(columns, row, strict=True))
        if emphasize_low_stock and "quantity" in values and "min_stock" in values:
            if values["quantity"] < values["min_stock"]:
                style = "red"
        if emphasize_difference and "difference" in values:
            difference = values["difference"]
            style = "red" if difference < 0 else "green" if difference > 0 else ""
        if "kind" in values and values["kind"] == "write_off":
            style = "red"
        if values.get("reason"):
            style = "red"
        formatted = [f"{value:.2f}" if isinstance(value, Decimal) else str(value) for value in row]
        table.add_row(*formatted, style=style)
    console.print(table)


@app.command("receive", help="Принять партию товара на склад.")
def receive_command(
    sku: Annotated[str, typer.Option("--sku", help="Артикул товара")],
    product_name: Annotated[str, typer.Option("--product-name", help="Название товара")],
    warehouse: Annotated[str, typer.Option(help="Склад прихода")],
    quantity: Annotated[int, typer.Option(min=1, help="Количество единиц")],
    unit_cost: Annotated[str, typer.Option(help="Закупочная цена одной единицы")],
    received_on: Annotated[
        datetime, typer.Option(help="Дата прихода: ГГГГ-ММ-ДД", formats=DATE_FORMAT)
    ],
    min_stock: Annotated[int, typer.Option(min=0, help="Минимальный запас")] = 0,
) -> None:
    """Record a supplier receipt as a new costed lot."""
    with _session() as session:
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
            raise typer.BadParameter(_message(error)) from error
    typer.echo(f"Принято: {batch.quantity_received} шт. товара {sku}, склад {warehouse}.")


@app.command("ship", help="Отгрузить товар по FIFO с номером для возврата.")
def ship_command(
    sku: Annotated[str, typer.Option("--sku", help="Артикул товара")],
    warehouse: Annotated[str, typer.Option(help="Склад отгрузки")],
    quantity: Annotated[int, typer.Option(min=1, help="Количество единиц")],
    shipped_on: Annotated[
        datetime, typer.Option(help="Дата отгрузки: ГГГГ-ММ-ДД", formats=DATE_FORMAT)
    ],
) -> None:
    """Ship FIFO stock and keep the costed shipment lines for returns."""
    with _session() as session:
        try:
            with session.begin():
                shipment = ship_with_reference(
                    session,
                    sku=sku,
                    warehouse_name=warehouse,
                    shipped_on=shipped_on.date(),
                    quantity=quantity,
                )
        except InventoryError as error:
            raise typer.BadParameter(_message(error)) from error
    typer.echo(
        f"Отгружено {quantity} шт. {sku}. Себестоимость: {shipment.cogs:.2f}. "
        f"Номер отгрузки: {shipment.id}."
    )


@app.command("write-off", help="Списать товар с указанием причины.")
def write_off_command(
    sku: Annotated[str, typer.Option("--sku", help="Артикул товара")],
    warehouse: Annotated[str, typer.Option(help="Склад")],
    quantity: Annotated[int, typer.Option(min=1, help="Количество единиц")],
    reason: Annotated[WriteOffReason, typer.Option(help="Причина списания")],
    occurred_on: Annotated[datetime, typer.Option(help="Дата: ГГГГ-ММ-ДД", formats=DATE_FORMAT)],
) -> None:
    """Write off FIFO stock and record the selected reason."""
    with _session() as session:
        try:
            with session.begin():
                amount = write_off(
                    session,
                    sku=sku,
                    warehouse_name=warehouse,
                    occurred_on=occurred_on.date(),
                    quantity=quantity,
                    reason=reason,
                )
        except InventoryError as error:
            raise typer.BadParameter(_message(error)) from error
    typer.echo(f"Списано {quantity} шт. {sku}. Потери по себестоимости: {amount:.2f}.")


@app.command("transfer", help="Переместить партию между складами.")
def transfer_command(
    sku: Annotated[str, typer.Option("--sku", help="Артикул товара")],
    source: Annotated[str, typer.Option(help="Исходный склад")],
    destination: Annotated[str, typer.Option(help="Целевой склад")],
    quantity: Annotated[int, typer.Option(min=1, help="Количество единиц")],
    transferred_on: Annotated[
        datetime, typer.Option(help="Дата перемещения: ГГГГ-ММ-ДД", formats=DATE_FORMAT)
    ],
) -> None:
    """Move FIFO lots between warehouses while retaining their original costs."""
    with _session() as session:
        try:
            with session.begin():
                amount = transfer(
                    session,
                    sku=sku,
                    source_name=source,
                    destination_name=destination,
                    transferred_on=transferred_on.date(),
                    quantity=quantity,
                )
        except InventoryError as error:
            raise typer.BadParameter(_message(error)) from error
    typer.echo(f"Перемещено {quantity} шт. {sku}. Стоимость партии: {amount:.2f}.")


@app.command("return", help="Оформить возврат со ссылкой на отгрузку.")
def return_command(
    shipment_id: Annotated[int, typer.Option("--shipment-id", min=1, help="Номер отгрузки")],
    quantity: Annotated[int, typer.Option(min=1, help="Количество возвращаемых единиц")],
    returned_on: Annotated[
        datetime, typer.Option(help="Дата возврата: ГГГГ-ММ-ДД", formats=DATE_FORMAT)
    ],
) -> None:
    """Restore returned units at their original shipment costs."""
    with _session() as session:
        try:
            with session.begin():
                amount = return_shipment(
                    session,
                    shipment_id=shipment_id,
                    returned_on=returned_on.date(),
                    quantity=quantity,
                )
        except InventoryError as error:
            raise typer.BadParameter(_message(error)) from error
    typer.echo(f"Возвращено {quantity} шт. Себестоимость возврата: {amount:.2f}.")


@app.command("import", help="Импортировать накладную из CSV.")
def import_command(file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """Import valid invoice lines and save invalid lines in a neighboring file."""
    with _session() as session:
        try:
            with session.begin():
                imported, error_path = import_csv(session, file)
        except (ValueError, OSError) as error:
            raise typer.BadParameter(_message(error)) from error
    typer.echo(f"Импортировано строк: {imported}.")
    if error_path.exists():
        typer.echo(f"Ошибочные строки: {error_path}")


@app.command("products", help="Показать каталог и остатки по складам.")
def products_command(
    search: Annotated[
        str | None, typer.Option("--search", help="Поиск по артикулу или названию")
    ] = None,
) -> None:
    """List catalog products, their warehouse balances, and optional search matches."""
    with _session() as session:
        _show_table(products_report(session, search), "Товары", emphasize_low_stock=True)


@app.command("history", help="Показать историю движений по артикулу.")
def history_command(sku: Annotated[str, typer.Argument(help="Артикул товара")]) -> None:
    """Show dated movements and the warehouse balance after each movement."""
    with _session() as session:
        try:
            _show_table(history_report(session, sku), f"История товара {sku}")
        except ValueError as error:
            raise typer.BadParameter(_message(error)) from error


@app.command("warehouses", help="Показать список складов.")
def warehouses_command() -> None:
    """List registered warehouses."""
    with _session() as session:
        warehouses = session.scalars(select(Warehouse).order_by(Warehouse.name)).all()
        table = Table(title="Склады")
        table.add_column("№")
        table.add_column("Название")
        for warehouse in warehouses:
            table.add_row(str(warehouse.id), warehouse.name)
        console.print(table)


@app.command("demo", help="Заполнить базу демонстрационными данными.")
def demo_command() -> None:
    """Populate the database with a small, repeat-safe set of sample operations."""
    today = date.today()
    with _session() as session:
        with session.begin():
            demo_exists = (
                session.scalar(select(Product.id).where(Product.sku == "DEMO-COF")) is not None
            )
            if not demo_exists:
                receive(
                    session,
                    sku="DEMO-COF",
                    product_name="Кофе арабика",
                    warehouse_name="Главный",
                    received_on=today - timedelta(days=7),
                    quantity=20,
                    unit_cost=Decimal("8.50"),
                    min_stock=5,
                )
                receive(
                    session,
                    sku="DEMO-COF",
                    product_name="Кофе арабика",
                    warehouse_name="Главный",
                    received_on=today - timedelta(days=3),
                    quantity=10,
                    unit_cost=Decimal("9.25"),
                    min_stock=5,
                )
                receive(
                    session,
                    sku="DEMO-TEA",
                    product_name="Чёрный чай",
                    warehouse_name="Главный",
                    received_on=today - timedelta(days=5),
                    quantity=14,
                    unit_cost=Decimal("4.20"),
                    min_stock=4,
                )
                transfer(
                    session,
                    sku="DEMO-COF",
                    source_name="Главный",
                    destination_name="Магазин",
                    transferred_on=today - timedelta(days=2),
                    quantity=6,
                )
                write_off(
                    session,
                    sku="DEMO-TEA",
                    warehouse_name="Главный",
                    occurred_on=today - timedelta(days=1),
                    quantity=1,
                    reason=WriteOffReason.SPOILAGE,
                )
    if demo_exists:
        typer.echo("Демо-данные уже есть; повторное заполнение пропущено.")
        return
    typer.echo("Демо-данные добавлены. Используйте stock products и stock report inventory.")


@count_app.command("export", help="Выгрузить ожидаемые остатки в CSV.")
def count_export_command(
    warehouse: Annotated[str, typer.Option(help="Склад для пересчёта")],
    output: Annotated[Path, typer.Option(help="Путь к CSV-файлу")],
) -> None:
    """Export expected stock quantities for a physical count."""
    with _session() as session:
        try:
            export_count(session, warehouse, output)
        except (InventoryError, OSError) as error:
            raise typer.BadParameter(_message(error)) from error
    typer.echo(f"Файл пересчёта сохранён: {output}")


@count_app.command("apply", help="Показать разницы и провести подтверждённый пересчёт.")
def count_apply_command(
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    yes: Annotated[bool, typer.Option("--yes", help="Подтвердить без вопроса")] = False,
) -> None:
    """Preview stock count differences and apply adjustments after confirmation."""
    with _session() as session:
        try:
            with session.begin():
                warehouse, previews = prepare_count(session, file)
                _show_count_preview(previews, warehouse)
                if not yes and not typer.confirm(
                    "Применить результаты инвентаризации?", default=False
                ):
                    typer.echo("Инвентаризация отменена.")
                    return
                apply_prepared_count(session, warehouse, previews, date.today())
        except (InventoryError, ValueError, OSError) as error:
            raise typer.BadParameter(_message(error)) from error
    typer.echo("Инвентаризация проведена.")


def _show_count_preview(previews: list[CountPreview], warehouse: str) -> None:
    rows = [
        {
            "warehouse": warehouse,
            "sku": preview.adjustment.sku,
            "product": preview.adjustment.product_name,
            "expected_quantity": preview.adjustment.expected_quantity,
            "counted_quantity": preview.adjustment.counted_quantity,
            "difference": preview.adjustment.difference,
            "amount": preview.amount,
        }
        for preview in previews
    ]
    _show_table(pd.DataFrame(rows), "Результат инвентаризации", emphasize_difference=True)


def _report_output(
    frame: pd.DataFrame, title: str, csv_path: Path | None, *, low_stock: bool = False
) -> None:
    _show_table(frame, title, csv_path=csv_path, emphasize_low_stock=low_stock)


@report_app.command("inventory", help="Показать остатки по складам.")
def inventory_command(
    warehouse: Annotated[str | None, typer.Option(help="Ограничить одним складом")] = None,
    csv_path: Annotated[
        Path | None, typer.Option("--csv", help="Сохранить результат в CSV")
    ] = None,
) -> None:
    """Show current quantities and cost values by warehouse."""
    with _session() as session:
        try:
            _report_output(
                inventory_report(session, warehouse),
                "Остатки",
                csv_path,
                low_stock=True,
            )
        except ValueError as error:
            raise typer.BadParameter(_message(error)) from error


@report_app.command("lowstock", help="Показать товары ниже минимального запаса.")
def lowstock_command(
    warehouse: Annotated[str | None, typer.Option(help="Проверить один склад")] = None,
    csv_path: Annotated[
        Path | None, typer.Option("--csv", help="Сохранить результат в CSV")
    ] = None,
) -> None:
    """List products whose balance is below the configured minimum."""
    with _session() as session:
        try:
            _report_output(
                low_stock_report(session, warehouse),
                "Низкий запас",
                csv_path,
                low_stock=True,
            )
        except ValueError as error:
            raise typer.BadParameter(_message(error)) from error


@report_app.command("turnover", help="Показать оборачиваемость за период.")
def turnover_command(
    start: Annotated[
        datetime, typer.Option("--from", help="Начальная дата: ГГГГ-ММ-ДД", formats=DATE_FORMAT)
    ],
    end: Annotated[
        datetime, typer.Option("--to", help="Конечная дата: ГГГГ-ММ-ДД", formats=DATE_FORMAT)
    ],
    warehouse: Annotated[str | None, typer.Option(help="Ограничить одним складом")] = None,
    csv_path: Annotated[
        Path | None, typer.Option("--csv", help="Сохранить результат в CSV")
    ] = None,
) -> None:
    """Calculate COGS and inventory turnover for a date range."""
    with _session() as session:
        try:
            frame = turnover_report(session, start.date(), end.date(), warehouse)
        except ValueError as error:
            raise typer.BadParameter(_message(error)) from error
        _report_output(frame, "Оборачиваемость", csv_path)


@report_app.command("losses", help="Показать списания отдельно от продаж.")
def losses_command(
    warehouse: Annotated[str | None, typer.Option(help="Ограничить одним складом")] = None,
    csv_path: Annotated[
        Path | None, typer.Option("--csv", help="Сохранить результат в CSV")
    ] = None,
) -> None:
    """List non-sales write-offs and their FIFO costs."""
    with _session() as session:
        try:
            _report_output(loss_report(session, warehouse), "Потери", csv_path)
        except ValueError as error:
            raise typer.BadParameter(_message(error)) from error


@report_app.command("excel", help="Экспортировать отчёты в Excel.")
def excel_command(
    output: Annotated[Path, typer.Option(help="Путь к Excel-файлу")],
    start: Annotated[
        datetime, typer.Option("--from", help="Начальная дата: ГГГГ-ММ-ДД", formats=DATE_FORMAT)
    ],
    end: Annotated[
        datetime, typer.Option("--to", help="Конечная дата: ГГГГ-ММ-ДД", formats=DATE_FORMAT)
    ],
    warehouse: Annotated[str | None, typer.Option(help="Ограничить одним складом")] = None,
) -> None:
    """Export current reports and the losses sheet to an Excel workbook."""
    with _session() as session:
        try:
            export_excel(session, output, start.date(), end.date(), warehouse)
        except (ValueError, OSError) as error:
            raise typer.BadParameter(_message(error)) from error
    typer.echo(f"Отчёты сохранены: {output}")


if __name__ == "__main__":
    app()
