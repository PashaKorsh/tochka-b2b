from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from uuid import UUID
from typing import Optional

from backend.modules.products.models import (
    Product, ProductImage, ProductCharacteristic, ProductStatus
)
from backend.modules.products.schemas import ProductCreate
from backend.modules.categories.models import Category


class ProductService:
    """
    Service layer for Product operations.
    Implements business logic for US-B2B-01: Create Product.
    """

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

        # Create product with CREATED status (canon: товар создается со статусом CREATED)
        product = Product(
            seller_id=seller_id,
            category_id=product_data.category_id,
            title=product_data.title,
            description=product_data.description,
            status=ProductStatus.CREATED,
            deleted=False,
            blocked=False
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
                selectinload(Product.skus),
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
