import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.modules.moderation.models import ProcessedModerationEvent
from backend.modules.moderation.schemas import ModerationEvent
from backend.modules.products.models import (
    BlockingReason, FieldReport, Product, ProductStatus, SKU,
)
from backend.modules.products.service import ProductService

logger = logging.getLogger(__name__)


class ModerationService:
    """
    Service layer for applying Moderation decisions (US-B2B-09).
    """

    @staticmethod
    async def apply_event(db: AsyncSession, event: ModerationEvent) -> bool:
        """
        Apply a Moderation decision to a product (canon b2b-flows.md#apply-moderation).

        Three paths:
        - MODERATED            → status=MODERATED, blocked=false, blocking_reason
                                 и field_reports очищены.
        - BLOCKED hard_block=false → status=BLOCKED, blocked=true, сохраняем
                                 blocking_reason и field_reports, каскад в B2C.
        - BLOCKED hard_block=true  → status=HARD_BLOCKED (терминальный), blocked=true,
                                 сохраняем только blocking_reason, каскад в B2C.

        Idempotent by `idempotency_key`: повторное событие — no-op.

        Returns True if the event was applied, False if it was a duplicate.

        Raises:
            ValueError: invalid status / Product not found.
        """
        # Idempotency — a duplicate event is a no-op.
        already = await db.get(ProcessedModerationEvent, event.idempotency_key)
        if already is not None:
            return False

        if event.status not in ("MODERATED", "BLOCKED"):
            raise ValueError("invalid status")

        # Lock the product row — serialises concurrent moderation events.
        result = await db.execute(
            select(Product)
            .options(selectinload(Product.skus))
            .where(Product.id == event.product_id)
            .with_for_update()
        )
        product = result.scalar_one_or_none()
        if product is None:
            raise ValueError("Product not found")

        # Always clear stale field reports — replaced/removed per the new decision.
        existing_reports = await db.execute(
            select(FieldReport).where(FieldReport.product_id == product.id)
        )
        for report in existing_reports.scalars().all():
            await db.delete(report)
        await db.flush()

        cascade = False

        if event.status == "MODERATED":
            product.status = ProductStatus.MODERATED
            product.blocked = False
            product.blocking_reason_id = None
            product.moderator_comment = None
        else:  # BLOCKED
            cascade = True
            product.status = (
                ProductStatus.HARD_BLOCKED if event.hard_block else ProductStatus.BLOCKED
            )
            product.blocked = True

            if event.blocking_reason is not None:
                # Upsert the blocking-reason catalogue row (id + title from the event).
                reason = await db.get(BlockingReason, event.blocking_reason.id)
                if reason is None:
                    db.add(BlockingReason(
                        id=event.blocking_reason.id,
                        title=event.blocking_reason.title,
                    ))
                else:
                    reason.title = event.blocking_reason.title
                product.blocking_reason_id = event.blocking_reason.id
                product.moderator_comment = event.blocking_reason.comment

            # Soft block persists field reports; hard block keeps blocking_reason only.
            if not event.hard_block and event.field_reports:
                for report in event.field_reports:
                    db.add(FieldReport(
                        product_id=product.id,
                        field_name=report.field_name,
                        sku_id=report.sku_id,
                        comment=report.comment,
                    ))

        sku_ids = [str(sku.id) for sku in product.skus]
        db.add(ProcessedModerationEvent(idempotency_key=event.idempotency_key))
        await db.commit()

        # Cascade PRODUCT_BLOCKED to B2C on any block (soft or hard).
        if cascade:
            await ProductService._send_b2c_event(
                product_id=product.id,
                sku_ids=sku_ids,
                event_type="PRODUCT_BLOCKED",
                idempotency_key=event.idempotency_key,
            )

        return True
