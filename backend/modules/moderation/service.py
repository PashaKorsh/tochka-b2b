import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.modules.moderation.models import ProcessedModerationEvent
from backend.modules.moderation.schemas import ModerationEvent, ModerationEventType
from backend.modules.products.models import FieldReport, Product, ProductStatus
from backend.modules.products.service import ProductService

logger = logging.getLogger(__name__)


class ModerationService:
    """
    Service layer for applying Moderation decisions (US-B2B-09).
    """

    @staticmethod
    async def apply_event(db: AsyncSession, event: ModerationEvent) -> bool:
        """
        Apply a Moderation decision to a product (canon b2b-flows.md#apply-moderation,
        spec b2b/neomarket-b2b.yaml#ModerationEventRequest).

        Three paths:
        - event_type=MODERATED       → status=MODERATED, blocked=false,
                                       blocking_reason_id / moderator_comment /
                                       field_reports очищены.
        - event_type=BLOCKED soft    → status=BLOCKED, blocked=true,
                                       blocking_reason_id и moderator_comment
                                       сохранены, field_reports сохранены, каскад в B2C.
        - event_type=BLOCKED hard    → status=HARD_BLOCKED (терминальный),
                                       blocked=true, сохраняем blocking_reason_id
                                       и moderator_comment, каскад в B2C.

        Idempotent by `idempotency_key`: повторное событие — no-op,
        каскад в B2C на повторе не вызывается.

        Returns True if the event was applied, False if it was a duplicate.

        Raises:
            ValueError: Product not found.
        """
        key_str = str(event.idempotency_key)

        # Idempotency — a duplicate event is a no-op (cascade not re-fired).
        already = await db.get(ProcessedModerationEvent, key_str)
        if already is not None:
            return False

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

        if event.event_type == ModerationEventType.MODERATED:
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
            # spec: flat blocking_reason_id + separate moderator_comment.
            # Title catalogue lives in Moderation; B2B stores только id-reference.
            product.blocking_reason_id = event.blocking_reason_id
            product.moderator_comment = event.moderator_comment

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
        db.add(ProcessedModerationEvent(idempotency_key=key_str))
        await db.commit()

        # Cascade PRODUCT_BLOCKED to B2C on any block (soft or hard).
        if cascade:
            await ProductService._send_b2c_event(
                product_id=product.id,
                sku_ids=sku_ids,
                event_type="PRODUCT_BLOCKED",
                idempotency_key=key_str,
            )

        return True
