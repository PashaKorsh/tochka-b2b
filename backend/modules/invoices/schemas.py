from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List
from uuid import UUID
from datetime import datetime

from backend.modules.invoices.models import InvoiceStatus


class InvoiceItemCreate(BaseModel):
    """
    One invoice line. Spec b2b/neomarket-b2b.yaml#InvoiceItemCreate.
    `quantity` проверяется в сервисе → 400 (канон: "quantity must be > 0").
    """
    sku_id: UUID = Field(..., description="ID SKU")
    quantity: int = Field(..., description="Количество (>0, проверка 400 в сервисе)")


class InvoiceCreate(BaseModel):
    """
    Request schema for POST /api/v1/invoices (US-B2B-06).
    Spec b2b/neomarket-b2b.yaml#InvoiceCreate. Пустой `items` → 400 в сервисе
    (канон: "At least one item is required").
    """
    items: List[InvoiceItemCreate] = Field(default_factory=list, description="Позиции накладной")


class InvoiceItemResponse(BaseModel):
    """
    Invoice line in the response. `accepted_quantity` is null until the
    warehouse accepts the invoice (acceptance is a separate flow).
    """
    id: UUID
    sku_id: UUID
    quantity: int
    accepted_quantity: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class InvoiceResponse(BaseModel):
    """
    Response schema for POST /api/v1/invoices (201), spec#InvoiceResponse.
    Накладная создаётся в статусе PENDING (канон b2b-flows.md#create-invoice).
    """
    id: UUID
    seller_id: UUID
    status: InvoiceStatus
    items: List[InvoiceItemResponse]
    created_at: datetime
    updated_at: datetime
    accepted_at: Optional[datetime] = None
    accepted_by: Optional[UUID] = None

    model_config = ConfigDict(from_attributes=True)
