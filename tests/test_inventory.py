"""Key inventory operation behavior."""

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from stockpilot.database import create_database_engine
from stockpilot.services import InventoryError, receive, ship, transfer


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_database_engine("sqlite://")
    with Session(engine, expire_on_commit=False) as database_session:
        yield database_session
    engine.dispose()


def test_ship_consumes_oldest_batches_and_calculates_fifo_cost(session: Session) -> None:
    with session.begin():
        receive(
            session,
            sku="COF-001",
            product_name="Coffee",
            warehouse_name="Main",
            received_on=date(2026, 1, 1),
            quantity=5,
            unit_cost=Decimal("2.00"),
        )
        receive(
            session,
            sku="COF-001",
            product_name="Coffee",
            warehouse_name="Main",
            received_on=date(2026, 1, 2),
            quantity=4,
            unit_cost=Decimal("3.00"),
        )
    with session.begin():
        cost = ship(
            session,
            sku="COF-001",
            warehouse_name="Main",
            shipped_on=date(2026, 1, 3),
            quantity=7,
        )

    assert cost == Decimal("16.00")  # 5 x 2.00 + 2 x 3.00


def test_ship_rejects_quantity_above_available_stock_without_partial_issue(
    session: Session,
) -> None:
    with session.begin():
        receive(
            session,
            sku="TEA-001",
            product_name="Tea",
            warehouse_name="Main",
            received_on=date(2026, 1, 1),
            quantity=3,
            unit_cost=Decimal("4.00"),
        )

    with pytest.raises(InventoryError, match="only 3 available"):
        with session.begin():
            ship(
                session,
                sku="TEA-001",
                warehouse_name="Main",
                shipped_on=date(2026, 1, 2),
                quantity=4,
            )

    with session.begin():
        cost = ship(
            session,
            sku="TEA-001",
            warehouse_name="Main",
            shipped_on=date(2026, 1, 3),
            quantity=3,
        )
    assert cost == Decimal("12.00")


def test_transfer_keeps_batch_age_for_fifo_at_destination(session: Session) -> None:
    with session.begin():
        receive(
            session,
            sku="SUG-001",
            product_name="Sugar",
            warehouse_name="Main",
            received_on=date(2026, 1, 1),
            quantity=2,
            unit_cost=Decimal("1.00"),
        )
        receive(
            session,
            sku="SUG-001",
            product_name="Sugar",
            warehouse_name="North",
            received_on=date(2026, 1, 5),
            quantity=2,
            unit_cost=Decimal("5.00"),
        )
    with session.begin():
        transfer(
            session,
            sku="SUG-001",
            source_name="Main",
            destination_name="North",
            transferred_on=date(2026, 1, 10),
            quantity=2,
        )
    with session.begin():
        cost = ship(
            session,
            sku="SUG-001",
            warehouse_name="North",
            shipped_on=date(2026, 1, 11),
            quantity=2,
        )

    # The transferred lot was received first, so it ships first.
    assert cost == Decimal("2.00")
