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
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoice_imports.id"), nullable=True)
    product: Mapped[Product] = relationship()
    warehouse: Mapped[Warehouse] = relationship(back_populates="batches")
    invoice_import: Mapped[InvoiceImport | None] = relationship()


class InvoiceImport(Base):
    """A successfully imported invoice, protected by number and file digest."""

    __tablename__ = "invoice_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_number: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    file_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


class Shipment(Base):
    """An outbound shipment and its original costed lot lines."""

    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), index=True)
    occurred_on: Mapped[date] = mapped_column(Date)
    quantity: Mapped[int] = mapped_column(Integer)
    cogs: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    product: Mapped[Product] = relationship()
    warehouse: Mapped[Warehouse] = relationship()
    lines: Mapped[list[ShipmentLine]] = relationship(back_populates="shipment")


class ShipmentLine(Base):
    """The quantity and acquisition cost taken from one lot by a shipment."""

    __tablename__ = "shipment_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    returned_quantity: Mapped[int] = mapped_column(Integer, default=0)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    shipment: Mapped[Shipment] = relationship(back_populates="lines")


class InventoryCount(Base):
    """A committed physical stock count for one warehouse."""

    __tablename__ = "inventory_counts"

    id: Mapped[int] = mapped_column(primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), index=True)
    occurred_on: Mapped[date] = mapped_column(Date)
    warehouse: Mapped[Warehouse] = relationship()
    lines: Mapped[list[InventoryCountLine]] = relationship(back_populates="inventory_count")


class InventoryCountLine(Base):
    """Expected and counted units with the value of an applied adjustment."""

    __tablename__ = "inventory_count_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    inventory_count_id: Mapped[int] = mapped_column(ForeignKey("inventory_counts.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    expected_quantity: Mapped[int] = mapped_column(Integer)
    counted_quantity: Mapped[int] = mapped_column(Integer)
    difference: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    inventory_count: Mapped[InventoryCount] = relationship(back_populates="lines")
    product: Mapped[Product] = relationship()


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
    reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    shipment_id: Mapped[int | None] = mapped_column(ForeignKey("shipments.id"), nullable=True)
    shipment_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("shipment_lines.id"), nullable=True
    )
    inventory_count_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_counts.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
    product: Mapped[Product] = relationship()
    warehouse: Mapped[Warehouse] = relationship()
