import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.modules.categories.models import Category
from backend.modules.products.models import (
    BlockingReason,
    FieldReport,
    Product,
    ProductCharacteristic,
    ProductImage,
    ProductStatus,
    SKU,
    SKUCharacteristic,
    SKUImage,
)
from backend.modules.products.schemas import (
    ProductCreate,
    ProductUpdate,
    SKUCreate,
    SKUUpdate,
)

logger = logging.getLogger(__name__)


class ProductService:
    """
    Service layer for Product operations.
    Implements business logic for US-B2B-01: Create Product.
    """

    @staticmethod
    def _generate_slug(title: str, product_id: UUID) -> str:
        """
        Build a URL slug for a product.

        spec b2b/neomarket-b2b.yaml#ProductResponse requires `slug` as a
        non-nullable string, so it is derived from the title at creation time.
        A short product-id suffix keeps it unique even for identical titles.
        """
        base = re.sub(r"[^\w]+", "-", title.strip().lower(), flags=re.UNICODE).strip("-")
        suffix = str(product_id).split("-")[0]
        return f"{base}-{suffix}" if base else suffix

    @staticmethod
    async def create_product(
        db: AsyncSession,
        product_data: ProductCreate,
        seller_id: UUID
    ) -> Product:
        """
        Create a new product with status CREATED.

        Business rules from canon b2b-flows.md#create-product:
        1. Product created with status=CREATED
        2. seller_id taken from JWT (passed as parameter)
        3. Product NOT sent to moderation (no SKU yet)
        4. skus=[] initially
        5. deleted=False, blocked=False by default

        Args:
            db: Database session
            product_data: Validated product data from request
            seller_id: Seller ID from JWT claims

        Returns:
            Created Product instance with all relationships loaded

        Raises:
            ValueError: If category_id does not exist
        """
        # Validate category exists (canon requirement: invalid_category_id_returns_400)
        category = await db.get(Category, product_data.category_id)
        if not category:
            raise ValueError("Category not found")

        # Create product with CREATED status (canon: товар создается со статусом CREATED).
        # The id is generated up-front so the non-nullable slug can be built before INSERT.
        product_id = uuid4()
        product = Product(
            id=product_id,
            seller_id=seller_id,
            category_id=product_data.category_id,
            title=product_data.title,
            slug=ProductService._generate_slug(product_data.title, product_id),
            description=product_data.description,
            status=ProductStatus.CREATED,
            deleted=False,
            blocked=False,
            blocking_reason_id=None,
            moderator_comment=None
        )
        db.add(product)
        await db.flush()

        # Create images
        for img_data in product_data.images:
            image = ProductImage(
                product_id=product.id,
                url=img_data.url,
                ordering=img_data.ordering
            )
            db.add(image)

        # Create characteristics
        for char_data in product_data.characteristics:
            characteristic = ProductCharacteristic(
                product_id=product.id,
                name=char_data.name,
                value=char_data.value
            )
            db.add(characteristic)

        await db.commit()

        # Reload with all relationships for response
        result = await db.execute(
            select(Product)
            .options(
                selectinload(Product.images),
                selectinload(Product.characteristics),
                selectinload(Product.skus).selectinload(SKU.images),
                selectinload(Product.skus).selectinload(SKU.characteristics),
                selectinload(Product.category)
            )
            .where(Product.id == product.id)
        )
        return result.scalar_one()

    @staticmethod
    async def get_product_by_id(
        db: AsyncSession,
        product_id: UUID,
        seller_id: Optional[UUID] = None
    ) -> Optional[Product]:
        """
        Get product by ID with ownership check.

        Args:
            db: Database session
            product_id: Product UUID
            seller_id: If provided, check ownership (IDOR prevention)

        Returns:
            Product instance or None if not found or not owned
        """
        query = select(Product).options(
            selectinload(Product.images),
            selectinload(Product.characteristics),
            selectinload(Product.skus),
            selectinload(Product.category)
        ).where(Product.id == product_id)

        if seller_id:
            query = query.where(Product.seller_id == seller_id)

        result = await db.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_product_for_view(
        db: AsyncSession,
        product_id: UUID,
        *,
        seller_id: Optional[UUID] = None,
    ) -> Optional[Product]:
        """
        Load a product for GET /api/v1/products/{id} (US-B2B-05) with every
        relationship the response needs.

        If `seller_id` is given (seller-cabinet view) ownership is enforced:
        a product owned by another seller is treated as not found (returns None
        → 404, not 403) so the existence of competitors' products is not leaked.
        `seller_id=None` is the cross-service view (X-Service-Key) — no check.
        """
        result = await db.execute(
            select(Product)
            .options(
                selectinload(Product.images),
                selectinload(Product.characteristics),
                selectinload(Product.skus).selectinload(SKU.images),
                selectinload(Product.skus).selectinload(SKU.characteristics),
                selectinload(Product.category),
            )
            .where(Product.id == product_id)
        )
        product = result.scalar_one_or_none()
        if product is None:
            return None
        if seller_id is not None and product.seller_id != seller_id:
            return None  # IDOR: hide existence — 404, not 403
        return product

    @staticmethod
    async def get_blocking_feedback(
        db: AsyncSession,
        product: Product,
    ) -> tuple[Optional[dict], list]:
        """
        Load moderation feedback for a product (US-B2B-05).

        Returns (blocking_reason, field_reports):
        - blocking_reason — dict {id, title, comment} or None if the product has
          no blocking_reason_id;
        - field_reports — list of FieldReport rows (empty if none).
        """
        blocking_reason = None
        if product.blocking_reason_id is not None:
            reason = await db.get(BlockingReason, product.blocking_reason_id)
            if reason is not None:
                blocking_reason = {
                    "id": reason.id,
                    "title": reason.title,
                    "comment": product.moderator_comment,
                }
        fr_result = await db.execute(
            select(FieldReport).where(FieldReport.product_id == product.id)
        )
        field_reports = list(fr_result.scalars().all())
        return blocking_reason, field_reports

    @staticmethod
    async def create_sku(
        db: AsyncSession,
        sku_data: SKUCreate,
        seller_id: UUID
    ) -> SKU:
        """
        Create a new SKU for a product.

        Business rules from canon b2b-flows.md#add-sku:
        1. Validate product exists and belongs to seller (IDOR prevention)
        2. Validate product is not HARD_BLOCKED (403 FORBIDDEN)
        3. Validate at least one image is provided (400 INVALID_REQUEST)
        4. If this is the first SKU for product with status CREATED:
           - Change product status: CREATED → ON_MODERATION
           - Send CREATED event to Moderation service
        5. If product already has SKUs - just add SKU, no status change, no event

        Args:
            db: Database session
            sku_data: Validated SKU data from request
            seller_id: Seller ID from JWT claims

        Returns:
            Created SKU instance with all relationships loaded

        Raises:
            ValueError: If product not found, not owned, HARD_BLOCKED, or missing image
        """
        # Field validation — canon b2b-flows.md#add-sku требует 400 INVALID_REQUEST
        # с конкретным текстом (а не 422). Делаем на уровне сервиса.
        if not sku_data.name or not sku_data.name.strip():
            raise ValueError("name is required")
        if sku_data.price <= 0:
            raise ValueError("price must be a positive integer (kopecks)")
        if sku_data.cost_price is not None and sku_data.cost_price <= 0:
            raise ValueError("cost_price must be a positive integer (kopecks)")
        # At least one image required (canon line 191, 233)
        if not sku_data.images or len(sku_data.images) == 0:
            raise ValueError("At least one image is required")

        # Get product with FOR UPDATE lock to prevent race conditions
        result = await db.execute(
            select(Product)
            .options(selectinload(Product.skus))
            .where(Product.id == sku_data.product_id)
            .with_for_update()
        )
        product = result.scalar_one_or_none()

        # Validate product exists
        if not product:
            raise ValueError("Product not found")

        # Validate ownership (IDOR prevention)
        if product.seller_id != seller_id:
            raise ValueError("NOT_OWNER: Product does not belong to the authenticated seller")

        # Validate not HARD_BLOCKED
        if product.status == ProductStatus.HARD_BLOCKED:
            raise ValueError("HARD_BLOCKED: Cannot add SKU to hard-blocked product")

        # Check if this is the first SKU (canon: by наличие SKU, not только по статусу)
        is_first_sku = len(product.skus) == 0
        should_transition = is_first_sku and product.status == ProductStatus.CREATED
        idempotency_key = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"neomarket:b2b:product_created:{product.id}",
            )
        )

        # Create SKU
        sku = SKU(
            product_id=sku_data.product_id,
            name=sku_data.name,
            price=sku_data.price,
            cost_price=sku_data.cost_price,
            discount=sku_data.discount,
            stock_quantity=sku_data.stock_quantity,
            reserved_quantity=0,
            article=sku_data.article,
        )
        db.add(sku)
        await db.flush()

        # Create SKU images
        for img_data in sku_data.images:
            image = SKUImage(
                sku_id=sku.id,
                url=img_data.url,
                ordering=img_data.ordering
            )
            db.add(image)

        # Create SKU characteristics
        for char_data in sku_data.characteristics:
            characteristic = SKUCharacteristic(
                sku_id=sku.id,
                name=char_data.name,
                value=char_data.value
            )
            db.add(characteristic)

        # Transition product status if first SKU
        if should_transition:
            product.status = ProductStatus.ON_MODERATION
            db.add(product)

        await db.commit()

        # После успешной фиксации в БД — уведомление Moderation (избегаем «событие без товара»)
        if should_transition:
            await ProductService._send_moderation_event(
                product_id=product.id,
                seller_id=seller_id,
                event_type="CREATED",
                idempotency_key=idempotency_key,
            )

        # Reload with all relationships for response
        result = await db.execute(
            select(SKU)
            .options(
                selectinload(SKU.images),
                selectinload(SKU.characteristics)
            )
            .where(SKU.id == sku.id)
        )
        return result.scalar_one()

    @staticmethod
    async def update_product(
        db: AsyncSession,
        product_id: UUID,
        update_data: ProductUpdate,
        seller_id: UUID,
    ) -> Product:
        """
        Edit a product (US-B2B-03, PATCH /api/v1/products/{product_id}).

        Business rules from canon b2b-flows.md#edit-product:
        1. Ownership check — product must belong to seller_id from JWT (else 403).
        2. HARD_BLOCKED products cannot be edited (403).
        3. Partial update — only provided fields are changed; `slug` stays stable.
        4. Status transition: MODERATED / BLOCKED → ON_MODERATION + событие EDITED.
           CREATED / ON_MODERATION — статус не меняется, событие не отправляется.

        Raises:
            ValueError: not found / NOT_OWNER / HARD_BLOCKED / Category not found.
        """
        # Lock the product row — serialises concurrent edits and status transitions.
        result = await db.execute(
            select(Product).where(Product.id == product_id).with_for_update()
        )
        product = result.scalar_one_or_none()

        if not product:
            raise ValueError("Product not found")
        if product.seller_id != seller_id:
            raise ValueError("NOT_OWNER: Product does not belong to the authenticated seller")
        if product.status == ProductStatus.HARD_BLOCKED:
            raise ValueError("HARD_BLOCKED: Cannot edit a hard-blocked product")

        fields_set = update_data.model_fields_set

        if "category_id" in fields_set and update_data.category_id is not None:
            category = await db.get(Category, update_data.category_id)
            if not category:
                raise ValueError("Category not found")
            product.category_id = update_data.category_id
        if "title" in fields_set and update_data.title is not None:
            product.title = update_data.title
        if "description" in fields_set and update_data.description is not None:
            product.description = update_data.description
        if "characteristics" in fields_set and update_data.characteristics is not None:
            # Replace the whole characteristics set (canon: name/value pairs).
            existing = await db.execute(
                select(ProductCharacteristic).where(
                    ProductCharacteristic.product_id == product.id
                )
            )
            for char in existing.scalars().all():
                await db.delete(char)
            await db.flush()
            for char_data in update_data.characteristics:
                db.add(ProductCharacteristic(
                    product_id=product.id,
                    name=char_data.name,
                    value=char_data.value,
                ))

        # Status transition (canon b2b-flows.md#edit-product).
        should_emit = product.status in (ProductStatus.MODERATED, ProductStatus.BLOCKED)
        if should_emit:
            product.status = ProductStatus.ON_MODERATION

        await db.commit()

        # After commit — notify Moderation. EDITED uses a fresh per-edit idempotency_key
        # (unlike the stable CREATED key): every edit is a distinct re-moderation event.
        if should_emit:
            await ProductService._send_moderation_event(
                product_id=product.id,
                seller_id=seller_id,
                event_type="EDITED",
                idempotency_key=str(uuid4()),
            )

        result = await db.execute(
            select(Product)
            .options(
                selectinload(Product.images),
                selectinload(Product.characteristics),
                selectinload(Product.skus).selectinload(SKU.images),
                selectinload(Product.skus).selectinload(SKU.characteristics),
                selectinload(Product.category),
            )
            .where(Product.id == product.id)
        )
        return result.scalar_one()

    @staticmethod
    async def update_sku(
        db: AsyncSession,
        sku_id: UUID,
        update_data: SKUUpdate,
        seller_id: UUID,
    ) -> SKU:
        """
        Edit a SKU (US-B2B-03, PATCH /api/v1/skus/{sku_id}).

        Business rules from canon b2b-flows.md#edit-product:
        1. Ownership check via the parent product (seller_id from JWT, else 403).
        2. HARD_BLOCKED parent product → editing forbidden (403).
        3. Partial update; `product_id` is immutable; `reserved_quantity` is NOT
           touched — active reserves are preserved (B2B does not cancel reserves).
        4. Editing a SKU also returns the parent product to moderation:
           MODERATED / BLOCKED → ON_MODERATION + событие EDITED.

        Raises:
            ValueError: SKU not found / NOT_OWNER / HARD_BLOCKED / invalid field.
        """
        fields_set = update_data.model_fields_set

        # Field validation — canon: invalid data → 400 (mirrors B2B-2 rules).
        if "name" in fields_set and update_data.name is not None:
            if not update_data.name.strip():
                raise ValueError("name is required")
        if "price" in fields_set and update_data.price is not None:
            if update_data.price <= 0:
                raise ValueError("price must be a positive integer (kopecks)")
        if "cost_price" in fields_set and update_data.cost_price is not None:
            if update_data.cost_price <= 0:
                raise ValueError("cost_price must be a positive integer (kopecks)")

        result = await db.execute(select(SKU).where(SKU.id == sku_id))
        sku = result.scalar_one_or_none()
        if not sku:
            raise ValueError("SKU not found")

        # Lock the parent product — ownership, status check and transition.
        result = await db.execute(
            select(Product).where(Product.id == sku.product_id).with_for_update()
        )
        product = result.scalar_one_or_none()
        if not product:
            raise ValueError("Product not found")
        if product.seller_id != seller_id:
            raise ValueError("NOT_OWNER: Product does not belong to the authenticated seller")
        if product.status == ProductStatus.HARD_BLOCKED:
            raise ValueError("HARD_BLOCKED: Cannot edit a hard-blocked product")

        if "name" in fields_set and update_data.name is not None:
            sku.name = update_data.name
        if "price" in fields_set and update_data.price is not None:
            sku.price = update_data.price
        if "discount" in fields_set and update_data.discount is not None:
            sku.discount = update_data.discount
        if "cost_price" in fields_set and update_data.cost_price is not None:
            sku.cost_price = update_data.cost_price
        if "article" in fields_set:
            # article is nullable — allow both setting and clearing.
            sku.article = update_data.article
        if "characteristics" in fields_set and update_data.characteristics is not None:
            existing = await db.execute(
                select(SKUCharacteristic).where(SKUCharacteristic.sku_id == sku.id)
            )
            for char in existing.scalars().all():
                await db.delete(char)
            await db.flush()
            for char_data in update_data.characteristics:
                db.add(SKUCharacteristic(
                    sku_id=sku.id,
                    name=char_data.name,
                    value=char_data.value,
                ))

        # reserved_quantity intentionally untouched — active reserves are preserved.

        should_emit = product.status in (ProductStatus.MODERATED, ProductStatus.BLOCKED)
        if should_emit:
            product.status = ProductStatus.ON_MODERATION

        await db.commit()

        if should_emit:
            await ProductService._send_moderation_event(
                product_id=product.id,
                seller_id=seller_id,
                event_type="EDITED",
                idempotency_key=str(uuid4()),
            )

        result = await db.execute(
            select(SKU)
            .options(
                selectinload(SKU.images),
                selectinload(SKU.characteristics),
            )
            .where(SKU.id == sku.id)
        )
        return result.scalar_one()

    @staticmethod
    async def delete_product(
        db: AsyncSession,
        product_id: UUID,
        seller_id: UUID,
    ) -> None:
        """
        Soft-delete a product (US-B2B-04, DELETE /api/v1/products/{product_id}).

        Business rules from canon b2b-flows.md#delete-product:
        1. Ownership check — product must belong to seller_id from JWT (else 403).
        2. Repeated delete of an already-deleted product → 400.
        3. Soft delete — set deleted = true; данные физически не удаляются
           (история заказов, ссылки, аналитика сохраняются).
        4. Two cascade events, fire-and-forget after commit:
           - DELETED → Moderation
           - PRODUCT_DELETED → B2C (с sku_ids для пометки корзин/избранного)

        Raises:
            ValueError: Product not found / NOT_OWNER / Product already deleted.
        """
        # Lock the product row to serialise concurrent delete attempts.
        result = await db.execute(
            select(Product)
            .options(selectinload(Product.skus))
            .where(Product.id == product_id)
            .with_for_update()
        )
        product = result.scalar_one_or_none()

        if not product:
            raise ValueError("Product not found")
        if product.seller_id != seller_id:
            raise ValueError("NOT_OWNER: Product does not belong to the authenticated seller")
        if product.deleted:
            raise ValueError("Product already deleted")

        sku_ids = [str(sku.id) for sku in product.skus]
        product.deleted = True
        await db.commit()

        # Cascade events. Stable per-product idempotency keys → retry/duplicate-safe.
        await ProductService._send_moderation_event(
            product_id=product_id,
            seller_id=seller_id,
            event_type="DELETED",
            idempotency_key=str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"neomarket:b2b:product_deleted:{product_id}"
            )),
        )
        await ProductService._send_b2c_event(
            product_id=product_id,
            sku_ids=sku_ids,
            event_type="PRODUCT_DELETED",
            idempotency_key=str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"neomarket:b2b:product_deleted_b2c:{product_id}"
            )),
        )

    @staticmethod
    async def list_seller_products(
        db: AsyncSession,
        seller_id: UUID,
        limit: int = 20,
        offset: int = 0,
        status: Optional[ProductStatus] = None,
        include_deleted: bool = False,
    ) -> tuple[list[Product], int]:
        """
        List the seller's own products (US-B2B-04, GET /api/v1/products).

        Soft-deleted products are excluded by default (include_deleted=False) —
        удалённый товар не виден в стандартном списке продавца. Returns the page
        of products together with the total count for pagination metadata.
        """
        filters = [Product.seller_id == seller_id]
        if not include_deleted:
            filters.append(Product.deleted.is_(False))
        if status is not None:
            filters.append(Product.status == status)

        total_result = await db.execute(
            select(func.count()).select_from(Product).where(*filters)
        )
        total_count = total_result.scalar_one()

        result = await db.execute(
            select(Product)
            .options(
                selectinload(Product.skus),
                selectinload(Product.images),
            )
            .where(*filters)
            .order_by(Product.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all()), total_count

    @staticmethod
    async def _send_b2c_event(
        product_id: UUID,
        sku_ids: list,
        event_type: str,
        idempotency_key: str,
    ) -> None:
        """
        Send a cascade event to the B2C service.

        Event format from canon b2b-flows.md#delete-product:
        POST {b2c_url}/api/v1/events/product
        X-Service-Key: {b2b_to_b2c_key}
        {
          "idempotency_key": "uuid",
          "event": "PRODUCT_DELETED",
          "product_id": "uuid",
          "sku_ids": ["uuid", ...],
          "date": "2026-03-16T09:00:00.000Z"
        }

        B2C использует sku_ids, чтобы пометить cart_items / wishlist_items
        недоступными. Доставка: синхронный POST после commit, fire-and-forget —
        при недоступности B2C ошибка логируется, ответ продавцу не ломается.
        """
        import os

        b2c_url = os.getenv("B2C_URL", "http://b2c:8000")
        service_key = os.getenv("B2B_TO_B2C_KEY", "dev-b2b-to-b2c-key")

        event_payload = {
            "idempotency_key": idempotency_key,
            "event": event_type,
            "product_id": str(product_id),
            "sku_ids": [str(sku_id) for sku_id in sku_ids],
            "date": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{b2c_url}/api/v1/events/product",
                    json=event_payload,
                    headers={"X-Service-Key": service_key},
                )
                response.raise_for_status()
        except Exception as e:
            logger.warning("Failed to send B2C event: %s", e, exc_info=True)

    @staticmethod
    async def _send_moderation_event(
        product_id: UUID,
        seller_id: UUID,
        event_type: str,
        idempotency_key: str,
    ) -> None:
        """
        Send event to Moderation service.

        Event format from canon b2b-flows.md#add-sku (lines 242-252):
        POST {moderation_url}/api/v1/events/product
        X-Service-Key: {b2b_to_mod_key}
        {
          "idempotency_key": "uuid",
          "product_id": "uuid",
          "seller_id": "uuid",
          "event": "CREATED",
          "date": "2026-03-15T14:30:00.000Z"
        }

        Доставка: синхронный HTTP POST из обработчика после commit (первая итерация).
        При ошибке сети/5xx запись в БД уже сохранена — логируем и не роняем ответ продавцу
        (альтернативы: outbox, отдельный воркер; см. ADR в PR).

        Args:
            product_id: Product UUID
            seller_id: Seller UUID
            event_type: "CREATED" or "EDITED"
            idempotency_key: стабильный ключ дедупликации для пары (product, первый CREATED)
        """
        import os

        moderation_url = os.getenv("MODERATION_URL", "http://moderation:8000")
        service_key = os.getenv("B2B_TO_MOD_KEY", "dev-b2b-to-mod-key")

        event_payload = {
            "idempotency_key": idempotency_key,
            "product_id": str(product_id),
            "seller_id": str(seller_id),
            "event": event_type,
            "date": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{moderation_url}/api/v1/events/product",
                    json=event_payload,
                    headers={"X-Service-Key": service_key},
                )
                response.raise_for_status()
        except Exception as e:
            logger.warning("Failed to send moderation event: %s", e, exc_info=True)
