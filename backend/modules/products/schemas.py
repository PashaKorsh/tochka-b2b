from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List
from uuid import UUID
from datetime import datetime
from backend.modules.products.models import ProductStatus


class ProductImageCreate(BaseModel):
    url: str = Field(..., min_length=1, max_length=2000)
    ordering: int = Field(default=0, ge=0)


class ProductImageResponse(BaseModel):
    id: UUID
    url: str
    ordering: int

    model_config = ConfigDict(from_attributes=True)


class ProductCharacteristicCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    value: str = Field(..., min_length=1, max_length=2000)


class CharacteristicResponse(BaseModel):
    """
    Matches spec CharacteristicResponse ({id, name, value}).
    Used for both product and SKU characteristics.
    """
    id: UUID
    name: str
    value: str

    model_config = ConfigDict(from_attributes=True)


class ProductCreate(BaseModel):
    """
    Request schema for POST /api/v1/products.

    Aligned with merged spec b2b/neomarket-b2b.yaml#ProductCreate and
    canon b2b-flows.md (B2B Product Creation Flow):
    - title, description, category_id are REQUIRED
    - description: required, 1-5000 chars (canon: "1-5000 characters, mandatory")
    - images: REQUIRED, at least 1 (canon: "Minimum 1 image required" — without an
      image the card never reaches the B2C catalog). This is a deliberate tightening
      over the spec's `default: []`, per the canon business invariant.
    - characteristics: optional
    """
    category_id: UUID = Field(..., description="ID категории из справочника")
    title: str = Field(..., min_length=1, max_length=255, description="Название товара")
    description: str = Field(..., min_length=1, max_length=5000, description="Описание товара")
    images: List[ProductImageCreate] = Field(
        ..., min_length=1, description="Массив изображений, минимум 1"
    )
    characteristics: List[ProductCharacteristicCreate] = Field(
        default_factory=list, description="Характеристики товара"
    )


class SKUImageResponse(BaseModel):
    id: UUID
    url: str
    ordering: int

    model_config = ConfigDict(from_attributes=True)


class SKUResponse(BaseModel):
    """
    Full seller-view SKU, matching spec b2b/neomarket-b2b.yaml#SKUResponse.
    Includes cost_price and reserved_quantity (hidden only in the public B2C view).
    `active_quantity` is a computed property on the SKU model (stock - reserved).
    """
    id: UUID
    product_id: UUID
    name: str
    price: int
    discount: int
    cost_price: Optional[int]
    stock_quantity: int
    active_quantity: int
    reserved_quantity: int
    article: Optional[str]
    images: List[SKUImageResponse]
    characteristics: List[CharacteristicResponse]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProductResponse(BaseModel):
    """
    Response schema for POST /api/v1/products (201) and GET /api/v1/products/{id}
    seller-view, matching spec b2b/neomarket-b2b.yaml#ProductResponse.

    Required by spec: slug, blocking_reason_id (nullable), moderator_comment (nullable).
    `blocked` is an extra field kept from the canon (b2b-flows.md) — unknown extra
    fields are ignored by spec-conformant clients, so it does not break the contract.
    """
    id: UUID
    seller_id: UUID
    category_id: UUID
    title: str
    slug: str
    description: str
    status: ProductStatus
    deleted: bool
    blocked: bool
    blocking_reason_id: Optional[UUID]
    moderator_comment: Optional[str]
    images: List[ProductImageResponse]
    characteristics: List[CharacteristicResponse]
    skus: List[SKUResponse]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ErrorResponse(BaseModel):
    """
    Error response schema matching spec b2b/neomarket-b2b.yaml#Error
    and canon b2b-flows.md error format. Used for 4xx responses.
    """
    code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable error message")
    details: Optional[dict] = Field(None, description="Optional structured error context")


class SKUImageCreate(BaseModel):
    url: str = Field(..., min_length=1, max_length=2000)
    ordering: int = Field(default=0, ge=0)


class SKUCharacteristicCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    value: str = Field(..., min_length=1, max_length=2000)


class SKUCreate(BaseModel):
    """
    Request schema for POST /api/v1/skus (US-B2B-02).
    Matches spec b2b/neomarket-b2b.yaml#SKUCreate (required: product_id, name, price;
    optional: stock_quantity default 0, article nullable, cost_price nullable,
    discount default 0, images/characteristics default []).

    Канон b2b-flows.md#add-sku требует для name/price/cost_price/image ошибку 400
    с конкретным сообщением (не 422). Поэтому `name`/`price` не валидируются здесь
    жёстко (min_length / gt), а проверяются в ProductService.create_sku, которая
    бросает ValueError → router отдаёт 400 INVALID_REQUEST.
    """
    product_id: UUID = Field(..., description="ID товара")
    name: str = Field(..., max_length=255, description="Название варианта (1-255, проверка 400 в сервисе)")
    price: int = Field(..., description="Цена продажи в копейках (>0, проверка 400 в сервисе)")
    stock_quantity: int = Field(default=0, ge=0, description="Остаток (spec default 0)")
    article: Optional[str] = Field(None, description="Артикул (spec nullable)")
    cost_price: Optional[int] = Field(
        default=None,
        description="Себестоимость в копейках (spec nullable); если задана — >0 (канон)",
    )
    discount: int = Field(default=0, ge=0, description="Абсолютная скидка в копейках")
    images: List[SKUImageCreate] = Field(default_factory=list, description="Изображения SKU")
    characteristics: List[SKUCharacteristicCreate] = Field(default_factory=list, description="Характеристики SKU")


class ProductUpdate(BaseModel):
    """
    Request schema for PATCH /api/v1/products/{product_id} (US-B2B-03).

    Spec b2b/neomarket-b2b.yaml#ProductUpdate — partial update, все поля опциональны:
    передаются только изменяемые. `images` редактируются отдельными эндпоинтами
    (POST/PATCH/DELETE /products/{id}/images), поэтому в ProductUpdate их нет.
    `slug` неизменяем (стабильный идентификатор карточки).
    """
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = Field(None, min_length=1, max_length=5000)
    category_id: Optional[UUID] = None
    characteristics: Optional[List[ProductCharacteristicCreate]] = None


class SKUUpdate(BaseModel):
    """
    Request schema for PATCH /api/v1/skus/{sku_id} (US-B2B-03).

    Spec b2b/neomarket-b2b.yaml#SKUUpdate — partial update, все поля опциональны.
    `product_id` неизменяем (canon b2b-flows.md#edit-product). `reserved_quantity`
    не редактируется — активные резервы сохраняются. name/price/cost_price
    проверяются в сервисе → 400 INVALID_REQUEST (как в US-B2B-02).
    """
    name: Optional[str] = Field(None, max_length=255)
    price: Optional[int] = None
    discount: Optional[int] = Field(None, ge=0)
    cost_price: Optional[int] = None
    article: Optional[str] = None
    characteristics: Optional[List[SKUCharacteristicCreate]] = None


class ProductShortResponse(BaseModel):
    """
    Compact product card for the seller's product list (US-B2B-04 / US-B2B-11).
    Matches spec b2b/neomarket-b2b.yaml#ProductShortResponse plus the seller-
    cabinet aggregates from canon b2b-flows.md#list-products.

    `min_price` — минимальная цена среди SKU (None, если SKU нет).
    `cover_image` — URL первого изображения по `ordering` (None, если изображений нет).
    `skus_count` — число вариантов товара.
    `total_active_quantity` — суммарно доступно к продаже (Σ active_quantity SKU).
    """
    id: UUID
    title: str
    slug: str
    status: ProductStatus
    category_id: UUID
    deleted: bool
    created_at: datetime
    min_price: Optional[int] = None
    cover_image: Optional[str] = None
    skus_count: int = 0
    total_active_quantity: int = 0

    model_config = ConfigDict(from_attributes=True)


class ProductPaginatedResponse(BaseModel):
    """
    Paginated seller product list, spec b2b/neomarket-b2b.yaml#ProductPaginatedResponse.
    """
    items: List[ProductShortResponse]
    total_count: int
    limit: int
    offset: int


# ───────────────────── US-B2B-05: view product card ─────────────────────

class BlockingReasonResponse(BaseModel):
    """
    Moderation blocking reason shown to the seller (canon b2b-flows.md#view-product).
    `id`/`title` — из справочника BlockingReason; `comment` — moderator_comment товара.
    """
    id: UUID
    title: str
    comment: Optional[str] = None


class FieldReportResponse(BaseModel):
    """
    Per-field moderation remark (canon b2b-flows.md#view-product).
    `sku_id` is None for product-level issues.
    """
    field_name: str
    sku_id: Optional[UUID] = None
    comment: str

    model_config = ConfigDict(from_attributes=True)


class ProductDetailResponse(ProductResponse):
    """
    Seller-view response for GET /api/v1/products/{id} (US-B2B-05).

    Расширяет ProductResponse модерационной обратной связью: при статусе BLOCKED
    продавец видит `blocking_reason` (причина) и `field_reports` (точечные
    замечания по полям). Для остальных статусов — blocking_reason=null,
    field_reports=[]. Канон требует этих полей; в spec ProductResponse их пока
    нет — кандидат на PR в neomarket-protocols.
    """
    blocking_reason: Optional[BlockingReasonResponse] = None
    field_reports: List[FieldReportResponse] = Field(default_factory=list)


class SKUPublicResponse(BaseModel):
    """
    Public B2C-view SKU, spec b2b/neomarket-b2b.yaml#SKUPublicResponse.
    БЕЗ cost_price и reserved_quantity — чувствительные поля только для продавца.
    """
    id: UUID
    product_id: UUID
    name: str
    price: int
    discount: int
    stock_quantity: int
    active_quantity: int
    article: Optional[str]
    images: List[SKUImageResponse]
    characteristics: List[CharacteristicResponse]

    model_config = ConfigDict(from_attributes=True)


class ProductPublicResponse(BaseModel):
    """
    Public B2C-view product card, spec b2b/neomarket-b2b.yaml#ProductPublicResponse.
    Возвращается при межсервисном вызове (X-Service-Key): без cost_price /
    reserved_quantity и без модерационной обратной связи.
    """
    id: UUID
    seller_id: UUID
    category_id: UUID
    title: str
    slug: str
    description: str
    status: ProductStatus
    images: List[ProductImageResponse]
    characteristics: List[CharacteristicResponse]
    skus: List[SKUPublicResponse]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProductPublicPaginatedResponse(BaseModel):
    """
    Paginated B2C catalog response (US-B2B-07).
    Items are public product cards — без cost_price / reserved_quantity.
    """
    items: List[ProductPublicResponse]
    total_count: int
    limit: int
    offset: int
