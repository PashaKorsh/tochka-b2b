from datetime import datetime
from enum import Enum
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class ModerationEventType(str, Enum):
    """Spec b2b/neomarket-b2b.yaml#ModerationEventType."""
    MODERATED = "MODERATED"
    BLOCKED = "BLOCKED"


class FieldReportInput(BaseModel):
    """
    Per-field violation detail (spec b2b/neomarket-b2b.yaml#FieldReport).
    `sku_id` is None for product-level issues.
    """
    field_name: str
    sku_id: Optional[UUID] = None
    comment: str


class ModerationEvent(BaseModel):
    """
    Inbound event from the Moderation service (US-B2B-09).

    Strict shape from spec b2b/neomarket-b2b.yaml#ModerationEventRequest:
    required: [idempotency_key, product_id, event_type, occurred_at];
    plus nullable moderator_id / moderator_comment / blocking_reason_id /
    field_reports and a default-false hard_block flag.
    """
    idempotency_key: UUID = Field(..., description="UUID — spec format: uuid")
    product_id: UUID
    event_type: ModerationEventType
    moderator_id: Optional[UUID] = None
    moderator_comment: Optional[str] = None
    blocking_reason_id: Optional[UUID] = Field(
        default=None,
        description="UUID-скаляр причины из каталога Moderation; обязательно при BLOCKED",
    )
    hard_block: bool = False
    field_reports: Optional[List[FieldReportInput]] = None
    occurred_at: datetime
