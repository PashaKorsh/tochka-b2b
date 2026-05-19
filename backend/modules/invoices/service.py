from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.modules.invoices.models import Invoice, InvoiceItem, InvoiceStatus
from backend.modules.invoices.schemas import InvoiceCreate
from backend.modules.products.models import Product, ProductStatus, SKU


class InvoiceService:
    """
    Service layer for Invoice operations.
    Implements business logic for US-B2B-06: Create Invoice.
    """

    @staticmethod
    async def create_invoice(
        db: AsyncSession,
        invoice_data: InvoiceCreate,
        seller_id: UUID,
    ) -> Invoice:
        """
        Create a stock-delivery invoice (US-B2B-06, POST /api/v1/invoices).

        Business rules from canon b2b-flows.md#create-invoice. Все проверки —
        ДО создания накладной (canon: "validation occurs before invoice creation"):
        1. items не пуст (иначе 400).
        2. quantity каждой позиции > 0 (иначе 400).
        3. каждый sku_id существует (иначе 404).
        4. родительский товар каждого SKU принадлежит seller_id из JWT (иначе 403).
        5. родительский товар каждого SKU в статусе MODERATED (иначе 400) —
           накладная оформляется только на одобренные товары.

        Raises:
            ValueError: empty items / quantity / SKU not found / NOT_OWNER /
                        non-MODERATED product.
        """
        if not invoice_data.items:
            raise ValueError("At least one item is required")

        for item in invoice_data.items:
            if item.quantity <= 0:
                raise ValueError("quantity must be > 0")

        # Validate every SKU before creating anything — all-or-nothing.
        for item in invoice_data.items:
            sku = await db.get(SKU, item.sku_id)
            if sku is None:
                raise ValueError("SKU not found")
            product = await db.get(Product, sku.product_id)
            if product is None:
                raise ValueError("SKU not found")
            if product.seller_id != seller_id:
                raise ValueError(
                    "NOT_OWNER: One or more SKUs do not belong to the authenticated seller"
                )
            if product.status != ProductStatus.MODERATED:
                raise ValueError("Invoice can only be created for MODERATED products")

        invoice = Invoice(seller_id=seller_id, status=InvoiceStatus.PENDING)
        db.add(invoice)
        await db.flush()

        for item in invoice_data.items:
            db.add(InvoiceItem(
                invoice_id=invoice.id,
                sku_id=item.sku_id,
                quantity=item.quantity,
                accepted_quantity=None,
            ))

        await db.commit()

        result = await db.execute(
            select(Invoice)
            .options(selectinload(Invoice.items))
            .where(Invoice.id == invoice.id)
        )
        return result.scalar_one()
