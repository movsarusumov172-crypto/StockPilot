"""CSV import with row-level validation and an adjacent error report."""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from stockpilot.models import InvoiceImport
from stockpilot.schemas import InvoiceRow
from stockpilot.services import InvoiceAlreadyImportedError, import_invoice_row

FIELD_LABELS = {
    "invoice_number": "номер накладной",
    "date": "дата",
    "warehouse": "склад",
    "sku": "артикул",
    "product_name": "название товара",
    "quantity": "количество",
    "unit_cost": "закупочная цена",
    "min_stock": "минимальный запас",
}


def import_csv(session: Session, file_path: Path) -> tuple[int, Path]:
    """Import valid rows and write invalid rows with reasons to ``*.errors.csv``."""
    error_path = file_path.with_name(f"{file_path.name}.errors.csv")
    imported = 0
    errors: list[dict[str, str | int]] = []
    file_content = file_path.read_bytes()
    file_hash = hashlib.sha256(file_content).hexdigest()

    reader = csv.DictReader(io.StringIO(file_content.decode("utf-8-sig"), newline=""))
    required_columns = set(InvoiceRow.model_fields)
    if reader.fieldnames is None or not required_columns.issubset(reader.fieldnames):
        raise ValueError("В CSV обязательны столбцы: " + ", ".join(sorted(required_columns)))
    raw_rows = list(reader)
    invoice_numbers = {
        (row.get("invoice_number") or "").strip()
        for row in raw_rows
        if (row.get("invoice_number") or "").strip()
    }
    if len(invoice_numbers) != 1:
        raise ValueError("В файле должен быть указан один общий номер накладной")
    invoice_number = invoice_numbers.pop()
    existing_invoice = session.scalar(
        select(InvoiceImport).where(
            or_(
                InvoiceImport.invoice_number == invoice_number,
                InvoiceImport.file_hash == file_hash,
            )
        )
    )
    if existing_invoice is not None:
        raise InvoiceAlreadyImportedError(
            existing_invoice.invoice_number,
            file_hash_match=existing_invoice.invoice_number != invoice_number,
        )

    invoice_import: InvoiceImport | None = None
    for row_number, raw_row in enumerate(raw_rows, start=2):
        try:
            row = InvoiceRow.model_validate(raw_row)
            if row.invoice_number != invoice_number:
                raise ValueError("указан другой номер накладной")
            if invoice_import is None:
                invoice_import = InvoiceImport(
                    invoice_number=invoice_number,
                    file_hash=file_hash,
                )
                session.add(invoice_import)
                session.flush()
            import_invoice_row(session, row, invoice_import)
            imported += 1
        except ValidationError as error:
            detail = error.errors()[0]
            field_name = str(detail["loc"][0]) if detail["loc"] else "строка"
            errors.append(
                {
                    "row_number": row_number,
                    "error": f"{FIELD_LABELS.get(field_name, field_name)}: некорректное значение",
                    **{key: value or "" for key, value in raw_row.items() if key is not None},
                }
            )
        except ValueError as error:
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
