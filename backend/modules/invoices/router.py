from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.core.auth import get_current_seller
from backend.modules.auth.models import Seller
from backend.modules.products.schemas import ErrorResponse
from backend.modules.invoices.schemas import InvoiceCreate, InvoiceResponse
from backend.modules.invoices.service import InvoiceService


router = APIRouter(prefix="/api/v1", tags=["Invoices"])


@router.post(
    "/invoices",
    response_model=InvoiceResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Invoice created (status PENDING)"},
        400: {"model": ErrorResponse, "description": "Empty items / bad quantity / non-MODERATED SKU"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "SKU belongs to another seller"},
        404: {"model": ErrorResponse, "description": "SKU not found"},
        422: {"description": "Validation Error"},
    },
    summary="Создать накладную (US-B2B-06)",
    description="""
    Создание накладной на поставку остатков продавцом.

    Бизнес-логика (canon b2b-flows.md#create-invoice):
    - seller_id из JWT; SKU чужого продавца → 403
    - items не пуст → иначе 400
    - quantity каждой позиции > 0 → иначе 400
    - SKU существует → иначе 404
    - родительский товар SKU в статусе MODERATED → иначе 400
    - накладная создаётся в статусе PENDING; accepted_quantity=null
      (заполняется при приёмке — отдельный flow)

    Соответствие spec (b2b/neomarket-b2b.yaml, neomarket-protocols):
    - Path:     POST /api/v1/invoices
    - Request:  InvoiceCreate
    - Response: InvoiceResponse
    """,
)
async def create_invoice(
    invoice_data: InvoiceCreate,
    db: AsyncSession = Depends(get_db),
    current_seller: Seller = Depends(get_current_seller),
) -> InvoiceResponse:
    """
    POST /api/v1/invoices - Create invoice (US-B2B-06).

    Canon test scenarios:
    - create_invoice_with_moderated_sku_returns_201
    - empty_items_returns_400
    - non_moderated_sku_returns_400
    - others_sku_returns_403
    """
    try:
        invoice = await InvoiceService.create_invoice(
            db=db,
            invoice_data=invoice_data,
            seller_id=current_seller.id,
        )
        return InvoiceResponse.model_validate(invoice)
    except ValueError as e:
        error_msg = str(e)
        if "SKU not found" in error_msg:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"code": "NOT_FOUND", "message": "SKU not found"},
            )
        if "NOT_OWNER" in error_msg or "do not belong" in error_msg:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "code": "NOT_OWNER",
                    "message": "One or more SKUs do not belong to the authenticated seller",
                },
            )
        if "At least one item" in error_msg:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"code": "INVALID_REQUEST", "message": "At least one item is required"},
            )
        if "MODERATED" in error_msg:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "code": "INVALID_REQUEST",
                    "message": "Invoice can only be created for MODERATED products",
                },
            )
        if "quantity must be" in error_msg:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"code": "INVALID_REQUEST", "message": "quantity must be > 0"},
            )
        raise
