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
    Request for POST /api/v1/fulfill (US-B2B-10).
    Same shape as unreserve — `order_id` служит ключом идемпотентности.
    """
    order_id: UUID = Field(..., description="ID заказа — ключ идемпотентности fulfill")
    items: List[InventoryItem] = Field(default_factory=list)


class FulfillResponse(BaseModel):
    ok: bool = True
