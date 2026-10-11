"""SQLAlchemy models for catalog items, warehouses, batches, and stock movements."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for application tables."""


class Product(Base):
    """A stock keeping unit and its reorder threshold."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    min_stock: Mapped[int] = mapped_column(Integer, default=0)


class Warehouse(Base):
    """A named warehouse location."""

    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    batches: Mapped[list[Batch]] = relationship(back_populates="warehouse")


class Batch(Base):
    """A received or transferred lot whose remaining units retain their cost."""

    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), index=True)
    received_on: Mapped[date] = mapped_column(Date)
    quantity_received: Mapped[int] = mapped_column(Integer)
    quantity_remaining: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    product: Mapped[Product] = relationship()
    warehouse: Mapped[Warehouse] = relationship(back_populates="batches")


class StockMovement(Base):
    """A ledger entry used to report stock and calculate turnover."""

    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), index=True)
    occurred_on: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(20))
    quantity_delta: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
    product: Mapped[Product] = relationship()
    warehouse: Mapped[Warehouse] = relationship()
