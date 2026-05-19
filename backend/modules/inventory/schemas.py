from pydantic import BaseModel, Field
from typing import List
from uuid import UUID


class InventoryItem(BaseModel):
    """One reserve/unreserve line. `quantity` проверяется в сервисе."""
    sku_id: UUID
    quantity: int


class ReserveRequest(BaseModel):
    """
    Request for POST /api/v1/reserve (US-B2B-08).
    `idempotency_key` генерирует клиент (B2C) — защита от двойного резерва.
    """
    idempotency_key: str = Field(..., min_length=1, description="UUID-строка, генерирует B2C")
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
