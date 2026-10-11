"""CSV import with row-level validation and an adjacent error report."""

from __future__ import annotations

import csv
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from stockpilot.schemas import InvoiceRow
from stockpilot.services import import_invoice_row


def import_csv(session: Session, file_path: Path) -> tuple[int, Path]:
    """Import valid rows and write invalid rows with reasons to ``*.errors.csv``."""
    error_path = file_path.with_name(f"{file_path.name}.errors.csv")
    imported = 0
    errors: list[dict[str, str | int]] = []

    with file_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        required_columns = set(InvoiceRow.model_fields)
        if reader.fieldnames is None or not required_columns.issubset(reader.fieldnames):
            raise ValueError(f"CSV must include columns: {', '.join(sorted(required_columns))}")

        for row_number, raw_row in enumerate(reader, start=2):
            try:
                row = InvoiceRow.model_validate(raw_row)
                with session.begin_nested():
                    import_invoice_row(session, row)
                    session.flush()
                imported += 1
            except (ValidationError, ValueError) as error:
                errors.append(
                    {
                        "row_number": row_number,
                        "error": str(error),
                        **{key: value or "" for key, value in raw_row.items() if key is not None},
                    }
                )

    if errors:
        fieldnames = ["row_number", "error", *(reader.fieldnames or [])]
        with error_path.open("w", encoding="utf-8", newline="") as error_file:
            writer = csv.DictWriter(error_file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(errors)
    elif error_path.exists():
        error_path.unlink()

    return imported, error_path
