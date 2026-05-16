from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from backend.database import get_db
from backend.modules.products.schemas import (
    ProductCreate, ProductResponse, ErrorResponse, SKUCreate, SKUResponse
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
            # Raised as HTTPException so the global handler emits the unified
            # {"code": ..., "message": ...} error body (see backend/main.py).
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_REQUEST", "message": "Category not found"},
            )
        raise


@router.post(
    "/skus",
    response_model=SKUResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "SKU created successfully"},
        400: {"model": ErrorResponse, "description": "Invalid request (validation error, missing image)"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Product is HARD_BLOCKED or not owned by seller"},
        404: {"model": ErrorResponse, "description": "Product not found"},
        422: {"description": "Validation Error"}
    },
    summary="Создать SKU (US-B2B-02)",
    description="""
    Создание варианта товара (SKU) продавцом.

    Бизнес-логика (canon b2b-flows.md#add-sku):
    - Если это первый SKU для товара со статусом CREATED:
      * Статус товара меняется: CREATED → ON_MODERATION
      * Отправляется событие CREATED в Moderation с X-Service-Key и idempotency_key
    - Если товар уже имеет SKU - SKU просто добавляется, статус не меняется, события не отправляются
    - Товар в статусе HARD_BLOCKED нельзя редактировать → 403

    Валидация (canon b2b-flows.md#add-sku → 400 INVALID_REQUEST):
    - product_id: обязательное, должен существовать и принадлежать текущему seller
    - name: обязательное, 1-255 символов
    - price: обязательное, > 0 (копейки)
    - cost_price: опциональное, nullable; если передано — > 0 (копейки)
    - discount: опциональное, >= 0 (копейки), default=0
    - stock_quantity: опциональное, default=0
    - article: опциональное, nullable
    - images: минимум 1 изображение обязательно
    - characteristics: опциональное

    Соответствие spec (b2b/neomarket-b2b.yaml, репозиторий neomarket-protocols):
    - Path:     POST /api/v1/skus
    - Request:  SKUCreate
    - Response: SKUResponse (seller-view: cost_price, active_quantity, reserved_quantity)
    """
)
async def create_sku(
    sku_data: SKUCreate,
    db: AsyncSession = Depends(get_db),
    current_seller: Seller = Depends(get_current_seller)
) -> SKUResponse:
    """
    POST /api/v1/skus - Create SKU (US-B2B-02).

    Canon test scenarios:
    - first_sku_transitions_product_to_on_moderation
    - first_sku_emits_created_event_to_moderation
    - second_sku_no_state_change
    - add_sku_to_hard_blocked_returns_403
    - missing_image_returns_400
    """
    from fastapi.responses import JSONResponse

    try:
        sku = await ProductService.create_sku(
            db=db,
            sku_data=sku_data,
            seller_id=current_seller.id
        )
        return SKUResponse.model_validate(sku)
    except ValueError as e:
        error_msg = str(e)
        if "Product not found" in error_msg:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"code": "NOT_FOUND", "message": "Product not found"}
            )
        elif "does not belong" in error_msg or "NOT_OWNER" in error_msg:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"code": "NOT_OWNER", "message": "Product does not belong to the authenticated seller"}
            )
        elif "HARD_BLOCKED" in error_msg:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"code": "FORBIDDEN", "message": "Cannot add SKU to hard-blocked product"}
            )
        elif "image is required" in error_msg or "At least one image" in error_msg:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"code": "INVALID_REQUEST", "message": "image is required"}
            )
        elif "name is required" in error_msg:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"code": "INVALID_REQUEST", "message": "name is required"},
            )
        # cost_price проверяется ДО price: строка "cost_price must be..." содержит
        # подстроку "price must be...", иначе сработает не та ветка.
        elif "cost_price must be a positive integer" in error_msg:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "code": "INVALID_REQUEST",
                    "message": "cost_price must be a positive integer (kopecks)",
                },
            )
        elif "price must be a positive integer" in error_msg:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "code": "INVALID_REQUEST",
                    "message": "price must be a positive integer (kopecks)",
                },
            )
        raise
