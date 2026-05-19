from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID


class ModerationBlockingReason(BaseModel):
    """Blocking reason carried by a BLOCKED moderation event (canon: {id, title, comment})."""
    id: UUID
    title: str
    comment: Optional[str] = None


class FieldReportInput(BaseModel):
    """Per-field violation detail. `sku_id` is None for product-level issues."""
    field_name: str
    sku_id: Optional[UUID] = None
    comment: str


class ModerationEvent(BaseModel):
    """
    Inbound event from the Moderation service (US-B2B-09).

    Shape from canon b2b-flows.md#apply-moderation:
    - status: MODERATED | BLOCKED;
    - hard_block: actual only for BLOCKED (default false);
    - blocking_reason / field_reports: present for BLOCKED.
    """
    idempotency_key: str = Field(..., min_length=1)
    product_id: UUID
    status: str = Field(..., description="MODERATED | BLOCKED")
    hard_block: bool = False
    blocking_reason: Optional[ModerationBlockingReason] = None
    field_reports: Optional[List[FieldReportInput]] = None
