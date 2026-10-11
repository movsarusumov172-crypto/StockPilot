"""Validated input records."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InvoiceRow(BaseModel):
    """A single received invoice line from CSV."""

    model_config = ConfigDict(str_strip_whitespace=True)

    invoice_number: str = Field(min_length=1, max_length=100)
    date: date
    warehouse: str = Field(min_length=1, max_length=120)
    sku: str = Field(min_length=1, max_length=80)
    product_name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(gt=0)
    unit_cost: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    min_stock: int = Field(default=0, ge=0)

    @field_validator("sku")
    @classmethod
    def sku_must_not_contain_spaces(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError("SKU must not contain whitespace")
        return value


class InventoryCountRow(BaseModel):
    """One warehouse product row exported for a physical count."""

    model_config = ConfigDict(str_strip_whitespace=True)

    warehouse: str = Field(min_length=1, max_length=120)
    sku: str = Field(min_length=1, max_length=80)
    product_name: str = Field(min_length=1, max_length=200)
    expected_quantity: int = Field(ge=0)
    counted_quantity: int = Field(ge=0)
    unit_cost: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
