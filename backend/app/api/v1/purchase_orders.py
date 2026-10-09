import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.purchase_order import PurchaseOrderCreate, PurchaseOrderResponse
from app.services.purchase_orders import (
    DuplicatePurchaseOrderNumber,
    create_purchase_order,
    get_purchase_order,
    get_purchase_order_by_number,
    list_purchase_orders,
)

router = APIRouter(prefix="/purchase-orders", tags=["purchase-orders"])


class PurchaseOrderListResponse(BaseModel):
    items: list[dict[str, object]]
    total: int
    limit: int
    offset: int


@router.post("", status_code=status.HTTP_201_CREATED, response_model=PurchaseOrderResponse)
def create_po(
    po_in: PurchaseOrderCreate, db: Annotated[Session, Depends(get_db)]
) -> PurchaseOrderResponse:
    try:
        po = create_purchase_order(db, data=po_in)
        db.commit()
        db.refresh(po)
        return PurchaseOrderResponse.from_orm_po(po)
    except DuplicatePurchaseOrderNumber as e:
        db.rollback()
        raise HTTPException(status_code=409, detail="purchase order number already exists") from e


@router.get("", response_model=PurchaseOrderListResponse)
def list_pos(
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> PurchaseOrderListResponse:
    rows, total = list_purchase_orders(db, limit=limit, offset=offset)
    items = [
        PurchaseOrderResponse.from_orm_po(row).model_dump(exclude={"items"}, mode="json")
        for row in rows
    ]
    return PurchaseOrderListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{po_id}", response_model=PurchaseOrderResponse)
def get_po(po_id: uuid.UUID, db: Annotated[Session, Depends(get_db)]) -> PurchaseOrderResponse:
    po = get_purchase_order(db, po_id)
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    return PurchaseOrderResponse.from_orm_po(po)


@router.get("/by-number/{po_number}", response_model=PurchaseOrderResponse)
def get_po_by_number(
    po_number: str, db: Annotated[Session, Depends(get_db)]
) -> PurchaseOrderResponse:
    # This is the lookup path PO matching will use
    po = get_purchase_order_by_number(db, po_number)
    if not po:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    return PurchaseOrderResponse.from_orm_po(po)

