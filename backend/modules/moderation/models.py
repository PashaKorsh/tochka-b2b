from sqlalchemy import Column, DateTime, String
from datetime import datetime

from backend.database import Base


class ProcessedModerationEvent(Base):
    """
    Idempotency log for inbound Moderation events (US-B2B-09).

    A moderation event is processed at most once: `idempotency_key` is the
    primary key, so a duplicate event is detected and turned into a no-op.
    """
    __tablename__ = "processed_moderation_events"

    idempotency_key = Column(String(128), primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
