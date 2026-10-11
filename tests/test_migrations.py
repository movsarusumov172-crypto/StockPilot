"""Upgrade behavior for databases created before schema versioning."""

from sqlalchemy import create_engine, inspect, text

from stockpilot.migrations import CURRENT_SCHEMA_VERSION, migrate_database


def test_legacy_database_is_migrated_without_losing_stock() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE products "
            "(id INTEGER PRIMARY KEY, sku VARCHAR(80) NOT NULL, "
            "name VARCHAR(200) NOT NULL, min_stock INTEGER NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE warehouses (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE batches "
            "(id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, warehouse_id INTEGER NOT NULL, "
            "received_on DATE NOT NULL, quantity_received INTEGER NOT NULL, "
            "quantity_remaining INTEGER NOT NULL, unit_cost NUMERIC(12, 2) NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE stock_movements "
            "(id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, warehouse_id INTEGER NOT NULL, "
            "occurred_on DATE NOT NULL, kind VARCHAR(20) NOT NULL, "
            "quantity_delta INTEGER NOT NULL, unit_cost NUMERIC(12, 2) NOT NULL, "
            "created_at DATETIME NOT NULL)"
        )
        connection.execute(text("INSERT INTO products VALUES (1, 'OLD-1', 'Legacy item', 2)"))
        connection.execute(text("INSERT INTO warehouses VALUES (1, 'Main')"))
        connection.execute(
            text("INSERT INTO batches VALUES (1, 1, 1, :received_on, 5, 3, 4.25)"),
            {"received_on": "2026-01-01"},
        )
        connection.execute(
            text(
                "INSERT INTO stock_movements VALUES "
                "(1, 1, 1, :occurred_on, 'receive', 5, 4.25, '2026-01-01 00:00:00')"
            ),
            {"occurred_on": "2026-01-01"},
        )

    migrate_database(engine)

    inspector = inspect(engine)
    assert "invoice_imports" in inspector.get_table_names()
    assert "inventory_counts" in inspector.get_table_names()
    assert "reason" in {column["name"] for column in inspector.get_columns("stock_movements")}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version FROM schema_version")).scalar_one() == (
            CURRENT_SCHEMA_VERSION
        )
        assert (
            connection.execute(
                text("SELECT quantity_remaining FROM batches WHERE id = 1")
            ).scalar_one()
            == 3
        )
        assert connection.execute(text("SELECT sku FROM products WHERE id = 1")).scalar_one() == (
            "OLD-1"
        )
    engine.dispose()
