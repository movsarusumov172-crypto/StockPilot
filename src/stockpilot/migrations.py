"""Small, explicit SQLite schema migrations."""

from __future__ import annotations

from sqlalchemy import Engine, inspect, text

from stockpilot.models import Base

CURRENT_SCHEMA_VERSION = 2


def migrate_database(engine: Engine) -> None:
    """Upgrade an empty or pre-migration StockPilot database to the current schema."""
    tables = set(inspect(engine).get_table_names())
    if "schema_version" not in tables:
        initial_version = 1 if {"products", "warehouses", "batches"}.issubset(tables) else 0
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE schema_version (version INTEGER NOT NULL PRIMARY KEY)")
            )
            connection.execute(
                text("INSERT INTO schema_version (version) VALUES (:version)"),
                {"version": initial_version},
            )

    with engine.connect() as connection:
        version = connection.execute(text("SELECT version FROM schema_version")).scalar_one()

    if version > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {version} is newer than supported version "
            f"{CURRENT_SCHEMA_VERSION}"
        )

    if version == 0:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE schema_version SET version = :version"),
                {"version": CURRENT_SCHEMA_VERSION},
            )
        return

    if version == 1:
        _upgrade_1_to_2(engine)


def _upgrade_1_to_2(engine: Engine) -> None:
    """Add invoice provenance, shipment links, and count records to the v1 schema."""
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        existing_batch_columns = {
            column["name"] for column in inspect(connection).get_columns("batches")
        }
        if "invoice_id" not in existing_batch_columns:
            connection.execute(
                text(
                    "ALTER TABLE batches ADD COLUMN invoice_id INTEGER "
                    "REFERENCES invoice_imports(id)"
                )
            )

        existing_movement_columns = {
            column["name"] for column in inspect(connection).get_columns("stock_movements")
        }
        additions = {
            "reason": "VARCHAR(40)",
            "shipment_id": "INTEGER REFERENCES shipments(id)",
            "shipment_line_id": "INTEGER REFERENCES shipment_lines(id)",
            "inventory_count_id": "INTEGER REFERENCES inventory_counts(id)",
        }
        for column_name, column_definition in additions.items():
            if column_name not in existing_movement_columns:
                connection.execute(
                    text(
                        f"ALTER TABLE stock_movements ADD COLUMN {column_name} {column_definition}"
                    )
                )
        connection.execute(
            text("UPDATE schema_version SET version = :version"),
            {"version": CURRENT_SCHEMA_VERSION},
        )
