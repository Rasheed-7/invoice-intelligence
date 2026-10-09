import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.purchase_order import PurchaseOrder


def test_create_purchase_order_with_items(client: TestClient) -> None:
    payload = {
        "po_number": "PO-12345",
        "vendor_name": "Acme Tools",
        "issued_date": "2023-11-01",
        "currency": "USD",
        "total_amount": "150.00",
        "notes": "Urgent delivery",
        "items": [
            {
                "position": 1,
                "description": "Hammer",
                "quantity": "5.0000",
                "unit_price": "10.0000",
                "line_total": "50.0000",
                "tax_rate": "0.2000",
            },
            {
                "position": 2,
                "description": "Saw",
                "quantity": "2.0000",
                "unit_price": "50.0000",
                "line_total": "100.0000",
                "tax_rate": "0.2000",
            },
        ],
    }
    resp = client.post("/api/v1/purchase-orders", json=payload)
    assert resp.status_code == 201
    
    data = resp.json()
    assert data["po_number"] == "PO-12345"
    assert data["vendor_name"] == "Acme Tools"
    assert data["issued_date"] == "2023-11-01"
    assert data["currency"] == "USD"
    assert data["total_amount"] == "150.0000"
    assert data["notes"] == "Urgent delivery"
    assert data["status"] == "open"
    
    items = data["items"]
    assert len(items) == 2
    assert items[0]["position"] == 1
    assert items[0]["description"] == "Hammer"
    assert items[0]["quantity"] == "5.0000"
    assert items[0]["unit_price"] == "10.0000"
    assert items[0]["line_total"] == "50.0000"
    assert items[0]["tax_rate"] == "0.2000"

    assert items[1]["position"] == 2
    assert items[1]["description"] == "Saw"
    assert items[1]["quantity"] == "2.0000"
    assert items[1]["unit_price"] == "50.0000"
    assert items[1]["line_total"] == "100.0000"
    assert items[1]["tax_rate"] == "0.2000"


def test_get_purchase_order_by_id(client: TestClient) -> None:
    payload = {
        "po_number": "PO-BY-ID",
        "vendor_name": "Get Vendor",
        "items": [{"position": 1, "description": "Item 1"}],
    }
    create_resp = client.post("/api/v1/purchase-orders", json=payload)
    created_id = create_resp.json()["id"]

    get_resp = client.get(f"/api/v1/purchase-orders/{created_id}")
    assert get_resp.status_code == 200

    data = get_resp.json()
    assert data["id"] == created_id
    assert data["po_number"] == "PO-BY-ID"
    assert data["vendor_name"] == "Get Vendor"
    assert len(data["items"]) == 1
    assert data["items"][0]["description"] == "Item 1"


def test_get_purchase_order_by_number(client: TestClient) -> None:
    payload = {
        "po_number": "PO-BY-NUM",
        "vendor_name": "Get Vendor 2",
        "items": [{"position": 1, "description": "Item 1"}],
    }
    create_resp = client.post("/api/v1/purchase-orders", json=payload)
    assert create_resp.status_code == 201

    get_resp = client.get("/api/v1/purchase-orders/by-number/PO-BY-NUM")
    assert get_resp.status_code == 200

    data = get_resp.json()
    assert data["po_number"] == "PO-BY-NUM"
    assert data["vendor_name"] == "Get Vendor 2"
    assert len(data["items"]) == 1


def test_create_purchase_order_duplicate_number_returns_409(client: TestClient) -> None:
    payload = {
        "po_number": "PO-DUP",
        "vendor_name": "Vendor A",
        "items": [],
    }
    resp1 = client.post("/api/v1/purchase-orders", json=payload)
    assert resp1.status_code == 201

    resp2 = client.post("/api/v1/purchase-orders", json=payload)
    assert resp2.status_code == 409
    assert resp2.json()["detail"] == "purchase order number already exists"


def test_create_purchase_order_rollback_on_duplicate(client: TestClient, db_session) -> None:
    payload_1 = {
        "po_number": "PO-ROLLBACK",
        "vendor_name": "Vendor A",
        "items": [{"position": 1, "description": "Original"}],
    }
    resp1 = client.post("/api/v1/purchase-orders", json=payload_1)
    assert resp1.status_code == 201

    payload_2 = {
        "po_number": "PO-ROLLBACK",
        "vendor_name": "Vendor B",
        "items": [{"position": 1, "description": "Duplicate attempt"}],
    }
    resp2 = client.post("/api/v1/purchase-orders", json=payload_2)
    assert resp2.status_code == 409

    result = db_session.execute(
        select(PurchaseOrder).where(PurchaseOrder.po_number == "PO-ROLLBACK")
    )
    pos = result.scalars().all()
    assert len(pos) == 1
    assert len(pos[0].items) == 1
    assert pos[0].items[0].description == "Original"


def test_get_purchase_order_not_found(client: TestClient) -> None:
    random_id = str(uuid.uuid4())
    get_resp = client.get(f"/api/v1/purchase-orders/{random_id}")
    assert get_resp.status_code == 404


def test_get_purchase_order_by_number_not_found(client: TestClient) -> None:
    get_resp = client.get("/api/v1/purchase-orders/by-number/MISSING-PO-999")
    assert get_resp.status_code == 404


def test_list_purchase_orders_empty(client: TestClient) -> None:
    get_resp = client.get("/api/v1/purchase-orders")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["items"] == []
    assert data["total"] == 0


def test_list_purchase_orders_returns_created(client: TestClient) -> None:
    for i in range(2):
        client.post(
            "/api/v1/purchase-orders",
            json={"po_number": f"PO-LIST-RETURN-{i}", "vendor_name": "list"},
        )

    get_resp = client.get("/api/v1/purchase-orders")
    assert get_resp.status_code == 200
    data = get_resp.json()

    pos = [
        po for po in data["items"] if po["po_number"].startswith("PO-LIST-RETURN-")
    ]
    assert len(pos) == 2
    assert pos[0]["po_number"] == "PO-LIST-RETURN-1"
    assert pos[1]["po_number"] == "PO-LIST-RETURN-0"


def test_list_purchase_orders_pagination(client: TestClient) -> None:
    for i in range(3):
        client.post(
            "/api/v1/purchase-orders",
            json={"po_number": f"PO-LIST-PAGE-{i}", "vendor_name": "page"},
        )

    resp1 = client.get("/api/v1/purchase-orders?limit=2&offset=0")
    data1 = resp1.json()
    assert len(data1["items"]) == 2
    assert data1["total"] == 3

    resp2 = client.get("/api/v1/purchase-orders?limit=2&offset=2")
    data2 = resp2.json()
    assert len(data2["items"]) == 1
    assert data2["total"] == 3


def test_create_purchase_order_rejects_extra_fields(client: TestClient) -> None:
    payload = {
        "po_number": "PO-EXTRA",
        "vendor_name": "V",
        "unknown_field": "disallowed",
    }
    resp = client.post("/api/v1/purchase-orders", json=payload)
    assert resp.status_code == 422


def test_create_purchase_order_rejects_extra_item_fields(client: TestClient) -> None:
    payload = {
        "po_number": "PO-EXTRA-ITEM",
        "vendor_name": "V",
        "items": [{"position": 1, "unknown_item_field": "bad"}],
    }
    resp = client.post("/api/v1/purchase-orders", json=payload)
    assert resp.status_code == 422


def test_create_purchase_order_default_status_is_open(client: TestClient) -> None:
    payload = {
        "po_number": "PO-STATUS",
        "vendor_name": "V",
    }
    resp = client.post("/api/v1/purchase-orders", json=payload)
    assert resp.status_code == 201
    assert resp.json()["status"] == "open"
