from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field
from typing import List
from uuid import UUID


class InventoryItem(BaseModel):
    """One reserve/unreserve line. `quantity` проверяется в сервисе."""
    sku_id: UUID
    quantity: int


# ───────────────────── Requests ─────────────────────

class ReserveRequest(BaseModel):
    """
    Request for POST /api/v1/inventory/reserve (US-B2B-08).

    Matches spec b2b/openapi.yaml#ReserveRequest:
    `idempotency_key` и `order_id` обязательны — без `order_id` невозможно
    связать резерв с заказом для последующего unreserve/fulfill.
    """
    idempotency_key: str = Field(..., min_length=1, description="UUID-строка, генерирует B2C")
    order_id: UUID = Field(..., description="ID заказа B2C, для которого выполняется резерв")
    items: List[InventoryItem] = Field(..., min_length=1, description="Минимум 1 позиция")


class UnreserveRequest(BaseModel):
    """
    Request for POST /api/v1/inventory/unreserve (US-B2B-08).
    spec b2b/openapi.yaml#InventoryOrderRequest; `order_id` служит ключом
    идемпотентности компенсирующей операции.
    """
    order_id: UUID = Field(..., description="ID заказа — ключ идемпотентности unreserve")
    items: List[InventoryItem] = Field(..., min_length=1, description="Минимум 1 позиция")


class FulfillRequest(BaseModel):
    """
    Request for POST /api/v1/inventory/fulfill (US-B2B-10).

    Matches spec b2b/openapi.yaml#InventoryOrderRequest:
    `items` required, ≥1. `order_id` doubles as the idempotency key for the
    compensating fulfill operation.
    """
    order_id: UUID = Field(..., description="ID заказа — ключ идемпотентности fulfill")
    items: List[InventoryItem] = Field(..., min_length=1, description="Минимум 1 позиция")


# ───────────────────── Responses ─────────────────────

class ReserveStatus(str, Enum):
    """Spec b2b/openapi.yaml#ReserveResponse.status — единственное значение."""
    RESERVED = "RESERVED"


class ReserveResponse(BaseModel):
    """
    Success response for POST /api/v1/inventory/reserve.
    Strict shape from spec b2b/openapi.yaml#ReserveResponse:
    {order_id, status: RESERVED, reserved_at}.
    """
    order_id: UUID
    status: ReserveStatus = ReserveStatus.RESERVED
    reserved_at: datetime


class InventoryOrderStatus(str, Enum):
    """
    Status of an InventoryOrder operation (spec b2b/openapi.yaml):
    UNRESERVED — после успешного unreserve;
    FULFILLED  — после успешного fulfill.
    """
    UNRESERVED = "UNRESERVED"
    FULFILLED = "FULFILLED"


class InventoryOrderResponse(BaseModel):
    """
    Response for inventory order operations (spec InventoryOrderResponse):
    `order_id`, `status` (UNRESERVED|FULFILLED), `processed_at`.
    Used for both /unreserve and /fulfill.
    """
    order_id: UUID
    status: InventoryOrderStatus
    processed_at: datetime


# Aliases — kept for backward compatibility with router imports / OpenAPI docs.
UnreserveResponse = InventoryOrderResponse
FulfillResponse = InventoryOrderResponse
