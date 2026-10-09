import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.enums import PurchaseOrderStatus
from app.models.purchase_order import PurchaseOrder


class PurchaseOrderItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None
    tax_rate: Decimal | None = None


class PurchaseOrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    po_number: str
    vendor_name: str
    issued_date: date | None = None
    currency: str | None = None
    total_amount: Decimal | None = None
    notes: str | None = None
    items: list[PurchaseOrderItemCreate] = []


class PurchaseOrderItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    position: int
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None
    tax_rate: Decimal | None = None


class PurchaseOrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    po_number: str
    vendor_name: str
    issued_date: date | None = None
    currency: str | None = None
    total_amount: Decimal | None = None
    status: PurchaseOrderStatus
    notes: str | None = None
    created_at: datetime
    updated_at: datetime
    items: list[PurchaseOrderItemResponse]

    @classmethod
    def from_orm_po(cls, po: PurchaseOrder) -> "PurchaseOrderResponse":
        return cls.model_validate(po)

