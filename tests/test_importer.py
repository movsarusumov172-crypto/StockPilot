"""CSV import keeps valid lines when neighboring lines are invalid."""

from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from stockpilot.database import create_database_engine
from stockpilot.importer import import_csv
from stockpilot.models import Batch


def test_csv_import_reports_invalid_rows_and_imports_valid_rows(tmp_path: Path) -> None:
    invoice = tmp_path / "invoice.csv"
    invoice.write_text(
        "date,warehouse,sku,product_name,quantity,unit_cost,min_stock\n"
        "2026-01-01,Main,OK-1,Good item,5,2.50,1\n"
        "not-a-date,Main,BAD-1,Bad date,4,2.50,1\n"
        "2026-01-03,Main,BAD-2,Bad quantity,0,2.50,1\n",
        encoding="utf-8",
    )
    engine = create_database_engine("sqlite://")
    with Session(engine, expire_on_commit=False) as session:
        with session.begin():
            imported, error_file = import_csv(session, invoice)
        batches = session.scalars(select(Batch)).all()

        assert imported == 1
        assert len(batches) == 1
        assert batches[0].product.sku == "OK-1"
        assert batches[0].received_on == date(2026, 1, 1)
    report = error_file.read_text(encoding="utf-8")
    assert "row_number,error" in report
    assert "BAD-1" in report
    assert "BAD-2" in report
    engine.dispose()
