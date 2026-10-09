import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.enums import PurchaseOrderStatus
from app.models.purchase_order import PurchaseOrder, PurchaseOrderItem
from app.schemas.purchase_order import PurchaseOrderCreate


class DuplicatePurchaseOrderNumber(Exception):
    """Raised when po_number already exists."""


def create_purchase_order(db: Session, *, data: PurchaseOrderCreate) -> PurchaseOrder:
    stmt = select(1).where(PurchaseOrder.po_number == data.po_number)
    if db.execute(stmt).scalar() is not None:
        raise DuplicatePurchaseOrderNumber()

    po = PurchaseOrder(
        po_number=data.po_number,
        vendor_name=data.vendor_name,
        issued_date=data.issued_date,
        currency=data.currency,
        total_amount=data.total_amount,
        status=PurchaseOrderStatus.OPEN,
        notes=data.notes,
    )
    db.add(po)
    db.flush()

    for item_data in data.items:
        po_item = PurchaseOrderItem(
            purchase_order_id=po.id,
            position=item_data.position,
            description=item_data.description,
            quantity=item_data.quantity,
            unit_price=item_data.unit_price,
            line_total=item_data.line_total,
            tax_rate=item_data.tax_rate,
        )
        db.add(po_item)
    
    return po


def get_purchase_order(db: Session, po_id: uuid.UUID) -> PurchaseOrder | None:
    stmt = (
        select(PurchaseOrder)
        .options(selectinload(PurchaseOrder.items))
        .where(PurchaseOrder.id == po_id)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_purchase_order_by_number(db: Session, po_number: str) -> PurchaseOrder | None:
    stmt = (
        select(PurchaseOrder)
        .options(selectinload(PurchaseOrder.items))
        .where(PurchaseOrder.po_number == po_number)
    )
    return db.execute(stmt).scalar_one_or_none()


def list_purchase_orders(
    db: Session, *, limit: int = 50, offset: int = 0
) -> tuple[list[PurchaseOrder], int]:
    total_stmt = select(func.count()).select_from(PurchaseOrder)
    total = db.execute(total_stmt).scalar() or 0
    
    stmt = (
        select(PurchaseOrder)
        .options(selectinload(PurchaseOrder.items))
        .order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(stmt).scalars().all()
    return list(rows), total

