from sqlalchemy import Column, DateTime, ForeignKey, Integer, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid
import enum

from backend.database import Base


class InvoiceStatus(str, enum.Enum):
    """
    Invoice lifecycle, matching spec b2b/neomarket-b2b.yaml#InvoiceStatus:
    [CREATED, PARTIALLY_ACCEPTED, ACCEPTED, CANCELLED]. На создании — CREATED;
    остальные значения наступают при приёмке накладной складом (отдельный flow).
    """
    CREATED = "CREATED"
    PARTIALLY_ACCEPTED = "PARTIALLY_ACCEPTED"
    ACCEPTED = "ACCEPTED"
    CANCELLED = "CANCELLED"


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seller_id = Column(UUID(as_uuid=True), ForeignKey("sellers.id"), nullable=False)
    status = Column(SQLEnum(InvoiceStatus), default=InvoiceStatus.CREATED, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    # Populated when the warehouse accepts the invoice (separate acceptance flow).
    accepted_at = Column(DateTime, nullable=True)
    accepted_by = Column(UUID(as_uuid=True), nullable=True)

    items = relationship(
        "InvoiceItem", back_populates="invoice", cascade="all, delete-orphan"
    )


class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_id = Column(UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False)
    sku_id = Column(UUID(as_uuid=True), ForeignKey("skus.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    # None until the invoice is accepted (acceptance is a separate flow).
    accepted_quantity = Column(Integer, nullable=True)

    invoice = relationship("Invoice", back_populates="items")
