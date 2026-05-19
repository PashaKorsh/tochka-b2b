from sqlalchemy import Column, DateTime, String, JSON
from datetime import datetime

from backend.database import Base


class InventoryOperation(Base):
    """
    Idempotency log for reserve / unreserve operations (US-B2B-08).

    `operation_key` is namespaced: "reserve:{idempotency_key}" or
    "unreserve:{order_id}". A repeated request with the same key returns the
    stored `result_json` without re-executing the inventory mutation.
    """
    __tablename__ = "inventory_operations"

    operation_key = Column(String(128), primary_key=True)
    result_json = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
