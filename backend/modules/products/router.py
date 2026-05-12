from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from backend.database import get_db
from backend.modules.products.schemas import (
    ProductCreate, ProductResponse, ErrorResponse
)
from backend.modules.products.service import ProductService
from backend.core.auth import get_current_seller
from backend.modules.auth.models import Seller


router = APIRouter(prefix="/api/v1", tags=["Products"])


@router.post(
    "/products",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Product created successfully"},
        400: {"model": ErrorResponse, "description": "Invalid request (validation error, category not found)"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        422: {"description": "Validation Error"}
    },
    summary="Создать товар (US-B2B-01)",
    description="""
    Создание карточки товара продавцом.

    Бизнес-логика (canon b2b-flows.md#create-product):
    - Товар создается со статусом CREATED
    - seller_id берется из JWT claims (не из body)
    - На модерацию НЕ отправляется (нужен хотя бы один SKU)
    - skus=[] изначально
    - deleted=False, blocked=False по умолчанию

    Валидация:
    - title: обязательное, 1-255 символов
    - category_id: обязательное, должна существовать
    - description: опциональное (anyOf [string, null] в openapi)
    - images: опциональное, default=[] (не min 1 как у других команд)

    Соответствие OpenAPI:
    - Request: ProductCreate (openapi.yaml:1598-1628)
    - Response: ProductResponse (openapi.yaml:1740-1800)
    - category_id возвращается плоским (не nested object)
    """
)
async def create_product(
    product_data: ProductCreate,
    db: AsyncSession = Depends(get_db),
    current_seller: Seller = Depends(get_current_seller)
) -> ProductResponse:
    """
    POST /api/v1/products - Create product (US-B2B-01).

    Canon test scenarios:
    - create_product_returns_201_with_created_status
    - seller_id_taken_from_jwt
    - missing_category_returns_400
    - invalid_category_id_returns_400
    """
    try:
        product = await ProductService.create_product(
            db=db,
            product_data=product_data,
            seller_id=current_seller.id
        )
        return ProductResponse.model_validate(product)
    except ValueError as e:
        if "Category not found" in str(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_REQUEST", "message": "Category not found"}
            )
        raise
