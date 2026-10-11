"""Command-line interaction behavior for count confirmation."""

import csv
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

import stockpilot.cli as cli
from stockpilot.counting import export_count
from stockpilot.database import create_database_engine
from stockpilot.models import Batch, Product
from stockpilot.services import receive


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_database_engine("sqlite://")
    with Session(engine, expire_on_commit=False) as database_session:
        yield database_session
    engine.dispose()


def test_count_apply_requires_confirmation_unless_yes_is_set(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with session.begin():
        receive(
            session,
            sku="CLI-1",
            product_name="CLI product",
            warehouse_name="Main",
            received_on=date(2026, 1, 1),
            quantity=3,
            unit_cost=Decimal("2.00"),
        )
    count_file = tmp_path / "count.csv"
    export_count(session, "Main", count_file)
    session.rollback()
    with count_file.open("r", encoding="utf-8-sig", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))
        fieldnames = rows[0].keys()
    rows[0]["counted_quantity"] = "2"
    with count_file.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    @contextmanager
    def use_test_session() -> Iterator[Session]:
        yield session

    monkeypatch.setattr(cli, "_session", use_test_session)
    runner = CliRunner()
    canceled = runner.invoke(cli.app, ["count", "apply", str(count_file)], input="n\n")
    assert canceled.exit_code == 0
    assert "отменена" in canceled.stdout.lower()
    assert _balance(session) == 3
    session.rollback()

    applied = runner.invoke(cli.app, ["count", "apply", str(count_file), "--yes"])
    assert applied.exit_code == 0
    assert _balance(session) == 2


def test_demo_command_does_not_duplicate_sample_data(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def use_test_session() -> Iterator[Session]:
        yield session

    monkeypatch.setattr(cli, "_session", use_test_session)
    runner = CliRunner()
    first_run = runner.invoke(cli.app, ["demo"])
    assert first_run.exit_code == 0
    initial_products = len(session.scalars(select(Product.id)).all())
    initial_batches = len(session.scalars(select(Batch.id)).all())
    session.rollback()

    second_run = runner.invoke(cli.app, ["demo"])

    assert second_run.exit_code == 0
    assert "уже есть" in second_run.stdout.lower()
    assert len(session.scalars(select(Product.id)).all()) == initial_products
    assert len(session.scalars(select(Batch.id)).all()) == initial_batches


def _balance(session: Session) -> int:
    return sum(session.scalars(select(Batch.quantity_remaining)).all())
