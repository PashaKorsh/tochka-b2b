from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field
from typing import List
from uuid import UUID


class InventoryItem(BaseModel):
    """One reserve/unreserve line. `quantity` проверяется в сервисе."""
    sku_id: UUID
    quantity: int


class ReserveRequest(BaseModel):
    """
    Request for POST /api/v1/inventory/reserve (US-B2B-08).

    Matches spec b2b/neomarket-b2b.yaml#ReserveRequest:
    `idempotency_key` и `order_id` обязательны — без `order_id` невозможно
    связать резерв с заказом для последующего unreserve/fulfill.
    """
    idempotency_key: str = Field(..., min_length=1, description="UUID-строка, генерирует B2C")
    order_id: UUID = Field(..., description="ID заказа B2C, для которого выполняется резерв")
    items: List[InventoryItem] = Field(default_factory=list)


class UnreserveRequest(BaseModel):
    """
    Request for POST /api/v1/unreserve (US-B2B-08).
    `order_id` служит ключом идемпотентности компенсирующей операции.
    """
    order_id: UUID = Field(..., description="ID заказа — ключ идемпотентности unreserve")
    items: List[InventoryItem] = Field(default_factory=list)


# Response bodies are assembled as plain dicts in the service (stored in the
# idempotency log as JSON) — see InventoryService. Schemas below document the
# contract for OpenAPI.

class ReservedItem(BaseModel):
    sku_id: UUID
    reserved_quantity: int
    remaining_stock: int


class ReserveResponse(BaseModel):
    reserved: bool = True
    order_id: UUID
    items: List[ReservedItem]


class FailedItem(BaseModel):
    sku_id: UUID
    requested: int
    available: int
    reason: str


class ReserveConflictResponse(BaseModel):
    reserved: bool = False
    failed_items: List[FailedItem]


class UnreserveResponse(BaseModel):
    ok: bool = True


class FulfillRequest(BaseModel):
    """
    Request for POST /api/v1/inventory/fulfill (US-B2B-10).

    Matches spec b2b/neomarket-b2b.yaml#InventoryOrderRequest:
    `items` is required with at least one entry. `order_id` doubles as the
    idempotency key for the compensating fulfill operation.
    """
    order_id: UUID = Field(..., description="ID заказа — ключ идемпотентности fulfill")
    items: List[InventoryItem] = Field(..., min_length=1, description="Минимум 1 позиция")


class InventoryOrderStatus(str, Enum):
    """
    Status of an InventoryOrder operation (spec b2b/neomarket-b2b.yaml):
    UNRESERVED — после успешного unreserve;
    FULFILLED  — после успешного fulfill.
    """
    UNRESERVED = "UNRESERVED"
    FULFILLED = "FULFILLED"


class InventoryOrderResponse(BaseModel):
    """
    Response for inventory order operations (spec InventoryOrderResponse):
    `order_id`, `status` (UNRESERVED|FULFILLED), `processed_at`.
    """
    order_id: UUID
    status: InventoryOrderStatus
    processed_at: datetime


# Kept for backward compatibility with imports / OpenAPI docs of unreserve.
FulfillResponse = InventoryOrderResponse
