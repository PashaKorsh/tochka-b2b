import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.modules.categories.models import Category
from backend.modules.products.models import (
    Product,
    ProductCharacteristic,
    ProductImage,
    ProductStatus,
    SKU,
    SKUCharacteristic,
    SKUImage,
)
from backend.modules.products.schemas import ProductCreate, SKUCreate

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
        # Validate at least one image (canon line 191, 233)
        if not sku_data.images or len(sku_data.images) == 0:
            raise ValueError("At least one image is required")

        if sku_data.cost_price is not None and sku_data.cost_price <= 0:
            raise ValueError("cost_price must be a positive integer (kopecks)")

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
            "date": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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
