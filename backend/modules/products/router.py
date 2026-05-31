import os

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID
from typing import List, Optional

from backend.database import get_db
from backend.modules.products.models import ProductStatus
from backend.modules.products.schemas import (
    ProductCreate, ProductResponse, ErrorResponse, SKUCreate, SKUResponse,
    ProductUpdate, SKUUpdate, ProductShortResponse, ProductPaginatedResponse,
    ProductDetailResponse, ProductPublicResponse, BlockingReasonResponse,
    FieldReportResponse, ProductPublicPaginatedResponse, ProductPublicShortResponse,
    BatchPublicProductsRequest,
)
from backend.modules.inventory.router import require_service_key
from backend.modules.products.service import ProductService
from backend.core.auth import get_current_seller, SECRET_KEY, ALGORITHM
from backend.modules.auth.models import Seller


router = APIRouter(prefix="/api/v1", tags=["Products"])


# ───────────────────── Caller resolution (US-B2B-05 / US-B2B-07) ─────────────────────

class _ProductViewer:
    """Resolved caller of a product endpoint: seller-cabinet or cross-service."""

    def __init__(self, mode: str, seller_id: Optional[UUID] = None):
        self.mode = mode  # "seller" | "service"
        self.seller_id = seller_id


_optional_bearer = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHORIZED", "message": "Could not validate credentials"},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_product_viewer(
    x_service_key: Optional[str] = Header(None, alias="X-Service-Key"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
    db: AsyncSession = Depends(get_db),
) -> _ProductViewer:
    """
    Resolve who is calling a product endpoint:
    - X-Service-Key header → cross-service mode (B2C catalog / Moderation);
    - Bearer JWT → seller-cabinet mode (seller_id из JWT claims);
    - neither → 401.

    One dependency → the endpoint picks the response schema by `mode`, so
    sensitive fields (cost_price, reserved_quantity) are dropped by the schema
    itself, not by ad-hoc logic that could be forgotten in a new mode.
    """
    if x_service_key:
        expected = os.getenv("SERVICE_API_KEY", "dev-service-key")
        if x_service_key != expected:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "UNAUTHORIZED", "message": "Invalid service key"},
            )
        return _ProductViewer(mode="service")

    if credentials is None:
        raise _unauthorized()
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if sub is None:
            raise ValueError("missing sub")
        seller_id = UUID(sub)
    except (JWTError, ValueError):
        raise _unauthorized()

    seller = await db.get(Seller, seller_id)
    if seller is None:
        raise _unauthorized()
    return _ProductViewer(mode="seller", seller_id=seller.id)


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


def _edit_error_response(error_msg: str) -> Optional[JSONResponse]:
    """
    Map a ProductService edit ValueError to a unified {code, message} response
    (US-B2B-03). Returns None if the error is not a known business error.

    cost_price проверяется ДО price: "cost_price must be..." содержит подстроку
    "price must be...".
    """
    if "Category not found" in error_msg:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"code": "INVALID_REQUEST", "message": "Category not found"},
        )
    if "Product already deleted" in error_msg:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"code": "INVALID_REQUEST", "message": "Product already deleted"},
        )
    if "Product not found" in error_msg or "SKU not found" in error_msg:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"code": "NOT_FOUND", "message": error_msg},
        )
    if "NOT_OWNER" in error_msg or "does not belong" in error_msg:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "code": "NOT_OWNER",
                "message": "Product does not belong to the authenticated seller",
            },
        )
    if "HARD_BLOCKED" in error_msg:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"code": "FORBIDDEN", "message": "Cannot edit a hard-blocked product"},
        )
    if "cost_price must be a positive integer" in error_msg:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "code": "INVALID_REQUEST",
                "message": "cost_price must be a positive integer (kopecks)",
            },
        )
    if "price must be a positive integer" in error_msg:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "code": "INVALID_REQUEST",
                "message": "price must be a positive integer (kopecks)",
            },
        )
    if "name is required" in error_msg:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"code": "INVALID_REQUEST", "message": "name is required"},
        )
    return None


@router.patch(
    "/products/{product_id}",
    response_model=ProductResponse,
    responses={
        200: {"description": "Product updated"},
        400: {"model": ErrorResponse, "description": "Invalid request (validation error, category not found)"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "HARD_BLOCKED or not owned by seller"},
        404: {"model": ErrorResponse, "description": "Product not found"},
        422: {"description": "Validation Error"},
    },
    summary="Редактировать товар (US-B2B-03)",
    description="""
    Частичное редактирование карточки товара продавцом.

    Бизнес-логика (canon b2b-flows.md#edit-product):
    - seller_id берётся из JWT; чужой товар → 403 NOT_OWNER
    - HARD_BLOCKED редактировать нельзя → 403 FORBIDDEN
    - Передаются только изменяемые поля (PATCH-семантика); slug неизменяем
    - Статус MODERATED/BLOCKED → ON_MODERATION + событие EDITED в Moderation
    - Статус CREATED/ON_MODERATION — без смены статуса и без события

    Соответствие spec (b2b/neomarket-b2b.yaml, neomarket-protocols):
    - Path:     PATCH /api/v1/products/{product_id}
    - Request:  ProductUpdate
    - Response: ProductResponse
    """,
)
async def update_product(
    product_id: UUID,
    update_data: ProductUpdate,
    db: AsyncSession = Depends(get_db),
    current_seller: Seller = Depends(get_current_seller),
) -> ProductResponse:
    """
    PATCH /api/v1/products/{product_id} - Edit product (US-B2B-03).

    Canon test scenarios:
    - edit_moderated_product_returns_to_on_moderation
    - edit_blocked_product_returns_to_on_moderation
    - edit_hard_blocked_returns_403
    - edit_others_product_returns_403
    """
    try:
        product = await ProductService.update_product(
            db=db,
            product_id=product_id,
            update_data=update_data,
            seller_id=current_seller.id,
        )
        return ProductResponse.model_validate(product)
    except ValueError as e:
        response = _edit_error_response(str(e))
        if response is not None:
            return response
        raise


@router.patch(
    "/skus/{sku_id}",
    response_model=SKUResponse,
    responses={
        200: {"description": "SKU updated"},
        400: {"model": ErrorResponse, "description": "Invalid request (validation error)"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "HARD_BLOCKED or not owned by seller"},
        404: {"model": ErrorResponse, "description": "SKU not found"},
        422: {"description": "Validation Error"},
    },
    summary="Редактировать SKU (US-B2B-03)",
    description="""
    Частичное редактирование варианта товара (SKU) продавцом.

    Бизнес-логика (canon b2b-flows.md#edit-product):
    - seller_id из JWT; чужой товар → 403 NOT_OWNER
    - HARD_BLOCKED родительский товар → 403 FORBIDDEN
    - product_id неизменяем; reserved_quantity не меняется — резервы сохраняются
    - Редактирование SKU возвращает родительский товар в ON_MODERATION
      (если он был MODERATED/BLOCKED) + событие EDITED

    Соответствие spec (b2b/neomarket-b2b.yaml, neomarket-protocols):
    - Path:     PATCH /api/v1/skus/{sku_id}
    - Request:  SKUUpdate
    - Response: SKUResponse
    """,
)
async def update_sku(
    sku_id: UUID,
    update_data: SKUUpdate,
    db: AsyncSession = Depends(get_db),
    current_seller: Seller = Depends(get_current_seller),
) -> SKUResponse:
    """
    PATCH /api/v1/skus/{sku_id} - Edit SKU (US-B2B-03).

    Canon test scenarios:
    - reserves_preserved_after_sku_edit
    - edit_hard_blocked_returns_403
    """
    try:
        sku = await ProductService.update_sku(
            db=db,
            sku_id=sku_id,
            update_data=update_data,
            seller_id=current_seller.id,
        )
        return SKUResponse.model_validate(sku)
    except ValueError as e:
        response = _edit_error_response(str(e))
        if response is not None:
            return response
        raise


def _to_short_response(product) -> ProductShortResponse:
    """
    Build a ProductShortResponse from a Product (US-B2B-04 / US-B2B-11 list view).

    Derived from the SKUs / images already loaded via selectinload:
    - min_price        — минимальная цена SKU;
    - cover_image      — первое изображение по ordering;
    - skus_count       — число SKU;
    - total_active_quantity — Σ (stock_quantity − reserved_quantity) по SKU.
    """
    skus = product.skus
    prices = [sku.price for sku in skus]
    total_active = sum(sku.stock_quantity - sku.reserved_quantity for sku in skus)
    cover_image = None
    if product.images:
        cover_image = min(product.images, key=lambda img: img.ordering).url
    return ProductShortResponse(
        id=product.id,
        title=product.title,
        slug=product.slug,
        status=product.status,
        category_id=product.category_id,
        deleted=product.deleted,
        created_at=product.created_at,
        min_price=min(prices) if prices else None,
        cover_image=cover_image,
        skus_count=len(skus),
        total_active_quantity=total_active,
    )


def _to_public_short_response(product) -> ProductPublicShortResponse:
    """
    Build a ProductPublicShortResponse for the B2C catalog (US-B2B-07).
    Catalog visibility guarantees ≥1 SKU with active stock, so `min_price`
    is always defined.
    """
    cover_image = None
    if product.images:
        cover_image = min(product.images, key=lambda img: img.ordering).url
    return ProductPublicShortResponse(
        id=product.id,
        title=product.title,
        slug=product.slug,
        status=product.status,
        category_id=product.category_id,
        min_price=min(sku.price for sku in product.skus),
        cover_image=cover_image,
        created_at=product.created_at,
    )


@router.get(
    "/products",
    response_model=ProductPaginatedResponse,
    responses={
        200: {"description": "Seller product list"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
    },
    summary="Список своих товаров (US-B2B-04 / US-B2B-11)",
    description="""
    Список товаров текущего продавца (seller_id из JWT). IDOR-безопасен: любые
    попытки переопределить seller_id через query игнорируются.

    Фильтры: status, include_deleted, search (ILIKE по title).
    Поля карточки: skus_count, total_active_quantity, min_price, cover_image.

    Публичный B2C-каталог — на отдельном пути `GET /api/v1/public/products`
    (US-B2B-07), здесь намеренно нет mode-switch по заголовку, чтобы спека
    `neomarket-b2b.yaml` соблюдалась точно.
    """,
)
async def list_products(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status_filter: Optional[ProductStatus] = Query(None, alias="status"),
    include_deleted: bool = Query(False),
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_seller: Seller = Depends(get_current_seller),
) -> ProductPaginatedResponse:
    """GET /api/v1/products - Seller product list (US-B2B-04 / US-B2B-11)."""
    products, total_count = await ProductService.list_seller_products(
        db=db,
        seller_id=current_seller.id,
        limit=limit,
        offset=offset,
        status=status_filter,
        include_deleted=include_deleted,
        search=search,
    )
    return ProductPaginatedResponse(
        items=[_to_short_response(product) for product in products],
        total_count=total_count,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/public/products",
    response_model=ProductPublicPaginatedResponse,
    responses={
        200: {"description": "B2C catalog page"},
        401: {"model": ErrorResponse, "description": "Unauthorized — invalid X-Service-Key"},
    },
    summary="B2C-каталог (US-B2B-07)",
    description="""
    Публичный каталог для B2C-сервиса (вызывается по X-Service-Key).

    Бизнес-логика (canon b2b-flows.md#catalog-for-b2c):
    - видимость: status=MODERATED, deleted=false, есть SKU с active_quantity>0;
    - фильтры: category, search, sort (price_asc|price_desc|date_desc);
    - ответ ProductPublicPaginatedResponse → items ProductPublicShortResponse
      (БЕЗ cost_price / reserved_quantity).

    Соответствие spec (b2b/openapi.yaml, neomarket-protocols):
    - Path:     GET /api/v1/public/products
    - Response: ProductPublicPaginatedResponse / ProductPublicShortResponse

    Батч-запрос по списку product_ids — на отдельном эндпоинте
    `POST /api/v1/public/products/batch` (spec#batchPublicProducts).
    """,
)
async def list_public_catalog(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    category_id: Optional[UUID] = Query(None, alias="category"),
    search: Optional[str] = Query(None),
    sort: str = Query("date_desc"),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_service_key),
) -> ProductPublicPaginatedResponse:
    """
    GET /api/v1/public/products - B2C catalog (US-B2B-07).

    Canon test scenarios:
    - catalog_returns_moderated_in_stock_products
    - catalog_excludes_hard_blocked
    - catalog_missing_service_key_returns_401
    - catalog_response_has_no_cost_price
    """
    products, total_count = await ProductService.list_catalog_products(
        db,
        limit=limit,
        offset=offset,
        category_id=category_id,
        search=search,
        sort=sort,
    )
    return ProductPublicPaginatedResponse(
        items=[_to_public_short_response(product) for product in products],
        total_count=total_count,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/public/products/batch",
    response_model=List[ProductPublicResponse],
    responses={
        200: {"description": "Visible subset of the requested product_ids (no 404 for hidden)"},
        401: {"model": ErrorResponse, "description": "Unauthorized — invalid X-Service-Key"},
        422: {"description": "Validation Error"},
    },
    summary="B2C batch — карточки по списку product_ids (US-B2B-07)",
    description="""
    Batch-получение публичных карточек товара по списку product_ids.
    Возвращает массив только для существующих и публикуемых товаров;
    скрытые товары (HARD_BLOCKED, deleted, без active SKU) просто отсутствуют
    в ответе — B2C трактует их как unavailable, без 404.

    Соответствие spec (b2b/openapi.yaml#batchPublicProducts, neomarket-protocols):
    - Path:     POST /api/v1/public/products/batch
    - Request:  {product_ids: [UUID, ...]}, maxItems=100
    - Response: List[ProductPublicResponse] — полные публичные карточки
      (без cost_price / reserved_quantity, items в skus — SKUPublicResponse).
    """,
)
async def batch_public_catalog(
    request: BatchPublicProductsRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_service_key),
) -> List[ProductPublicResponse]:
    """
    POST /api/v1/public/products/batch - B2C batch (US-B2B-07).

    Canon test scenario:
    - batch_ids_returns_visible_subset
    """
    if not request.product_ids:
        return []

    products, _total = await ProductService.list_catalog_products(
        db,
        limit=len(request.product_ids),
        offset=0,
        ids=request.product_ids,
    )
    return [ProductPublicResponse.model_validate(product) for product in products]


@router.delete(
    "/products/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Product soft-deleted"},
        400: {"model": ErrorResponse, "description": "Product already deleted"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Not owned by seller"},
        404: {"model": ErrorResponse, "description": "Product not found"},
    },
    summary="Удалить товар (US-B2B-04)",
    description="""
    Мягкое удаление карточки товара продавцом.

    Бизнес-логика (canon b2b-flows.md#delete-product):
    - seller_id из JWT; чужой товар → 403 NOT_OWNER
    - Повторное удаление уже удалённого товара → 400
    - Soft delete: deleted=true, данные физически не удаляются
    - Два каскадных события (fire-and-forget после commit):
      * DELETED → Moderation
      * PRODUCT_DELETED → B2C (с sku_ids — B2C помечает корзины/избранное)

    Соответствие spec (b2b/neomarket-b2b.yaml, neomarket-protocols):
    - Path:     DELETE /api/v1/products/{product_id}
    - Success:  204 No Content
    """,
)
async def delete_product(
    product_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_seller: Seller = Depends(get_current_seller),
):
    """
    DELETE /api/v1/products/{product_id} - Soft-delete product (US-B2B-04).

    Canon test scenarios:
    - delete_sets_deleted_true
    - delete_emits_event_to_moderation
    - delete_emits_product_deleted_to_b2c
    - delete_already_deleted_returns_400
    - delete_others_product_returns_403
    """
    try:
        await ProductService.delete_product(
            db=db,
            product_id=product_id,
            seller_id=current_seller.id,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except ValueError as e:
        response = _edit_error_response(str(e))
        if response is not None:
            return response
        raise


# ───────────────────── US-B2B-05: view product card ─────────────────────


@router.get(
    "/products/{product_id}",
    response_model=None,
    responses={
        200: {"description": "Product card (seller-view or public B2C-view)"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "Product not found"},
    },
    summary="Карточка товара (US-B2B-05)",
    description="""
    Получение карточки товара.

    Бизнес-логика (canon b2b-flows.md#view-product):
    - Bearer JWT → seller-режим: полный payload (cost_price, reserved_quantity),
      а при статусе BLOCKED — blocking_reason и field_reports с замечаниями.
    - X-Service-Key → межсервисный режим: ProductPublicResponse без cost_price /
      reserved_quantity и без модерационной обратной связи.
    - Чужой товар → 404 (не 403): не раскрываем факт существования.
    - Несуществующий товар → 404.

    Соответствие spec (b2b/neomarket-b2b.yaml, neomarket-protocols):
    - Path:     GET /api/v1/products/{product_id}
    - Response: ProductResponse (seller) | ProductPublicResponse (X-Service-Key)
    """,
)
async def get_product(
    product_id: UUID,
    viewer: _ProductViewer = Depends(get_product_viewer),
    db: AsyncSession = Depends(get_db),
):
    """
    GET /api/v1/products/{product_id} - View product card (US-B2B-05).

    Canon test scenarios:
    - get_moderated_product_returns_full_payload
    - get_blocked_product_returns_blocking_reason_and_field_reports
    - get_others_product_returns_404
    - get_nonexistent_returns_404
    """
    not_found = JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"code": "NOT_FOUND", "message": "Product not found"},
    )

    if viewer.mode == "service":
        product = await ProductService.get_product_for_view(db, product_id, seller_id=None)
        if product is None:
            return not_found
        return ProductPublicResponse.model_validate(product)

    # Seller-cabinet view — ownership enforced inside the service (404, not 403).
    product = await ProductService.get_product_for_view(
        db, product_id, seller_id=viewer.seller_id
    )
    if product is None:
        return not_found

    blocking_reason, field_reports = await ProductService.get_blocking_feedback(db, product)
    base = ProductResponse.model_validate(product)
    return ProductDetailResponse(
        **base.model_dump(),
        blocking_reason=(
            BlockingReasonResponse(**blocking_reason) if blocking_reason is not None else None
        ),
        field_reports=[FieldReportResponse.model_validate(report) for report in field_reports],
    )


@router.delete(
    "/skus/{sku_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "SKU deleted"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "HARD_BLOCKED or not owned by seller"},
        404: {"model": ErrorResponse, "description": "SKU not found"},
        409: {"model": ErrorResponse, "description": "SKU has active reserves"},
    },
    summary="Удалить SKU (US-B2B-12)",
    description="""
    Удаление варианта товара продавцом.

    Guardrails (canon b2b-flows.md#delete-sku, порядок важен):
    1. SKU существует → иначе 404
    2. Родитель принадлежит продавцу (JWT) → иначе 403 NOT_OWNER
    3. Родитель НЕ HARD_BLOCKED → иначе 403 FORBIDDEN
    4. reserved_quantity == 0 → иначе 409 (есть активные резервы)

    Каскадные эффекты:
    - Последний SKU + товар ON_MODERATION → товар → CREATED + событие DELETED в Moderation
    - Иначе, если родитель MODERATED и у удалённого SKU был active>0 →
      событие SKU_OUT_OF_STOCK в B2C

    Соответствие spec (b2b/neomarket-b2b.yaml, neomarket-protocols):
    - Path:     DELETE /api/v1/skus/{sku_id}
    - Success:  204 No Content
    """,
)
async def delete_sku(
    sku_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_seller: Seller = Depends(get_current_seller),
):
    """
    DELETE /api/v1/skus/{sku_id} - Delete a SKU (US-B2B-12).

    Canon test scenarios:
    - delete_sku_succeeds
    - delete_sku_with_active_reserves_returns_409
    - last_sku_on_moderation_transitions_product_to_created
    - delete_sku_hard_blocked_product_returns_403
    - sku_out_of_stock_event_on_moderated_product
    """
    try:
        await ProductService.delete_sku(
            db=db,
            sku_id=sku_id,
            seller_id=current_seller.id,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except ValueError as e:
        error_msg = str(e)
        if "SKU has active reserves" in error_msg:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"code": "CONFLICT", "message": "SKU has active reserves"},
            )
        response = _edit_error_response(error_msg)
        if response is not None:
            return response
        raise
