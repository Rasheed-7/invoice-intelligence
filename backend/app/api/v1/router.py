from fastapi import APIRouter

from app.api.v1 import documents, health, invoices, policies, purchase_orders, reviews

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(documents.router)
api_router.include_router(invoices.router)
api_router.include_router(policies.router)
api_router.include_router(purchase_orders.router)
api_router.include_router(reviews.router)