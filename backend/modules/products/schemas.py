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


class ProductCharacteristicResponse(BaseModel):
    id: UUID
    name: str
    value: str

    model_config = ConfigDict(from_attributes=True)


class ProductCreate(BaseModel):
    """
    Request schema for POST /api/v1/products.
    Based on canon b2b-flows.md#create-product and OpenAPI b2b/openapi.yaml ProductCreate.

    Key differences from other teams' implementations (fixing their errors):
    1. title and category_id are REQUIRED (not optional) - canon requires them
    2. description is OPTIONAL (anyOf [string, null] in openapi.yaml:1608-1611)
    3. images default to [] (not required min 1) - openapi.yaml:1612-1617 has default=[]
    4. All fields match openapi.yaml ProductCreate schema exactly
    """
    category_id: UUID = Field(..., description="ID категории из справочника")
    title: str = Field(..., min_length=1, max_length=255, description="Название товара")
    description: Optional[str] = Field(None, max_length=5000, description="Описание товара")
    images: List[ProductImageCreate] = Field(default_factory=list, description="Массив изображений")
    characteristics: List[ProductCharacteristicCreate] = Field(default_factory=list, description="Характеристики товара")


class CategoryRef(BaseModel):
    id: UUID
    name: str

    model_config = ConfigDict(from_attributes=True)


class SKUShortResponse(BaseModel):
    """
    Short SKU response for ProductResponse.skus[].
    Based on openapi.yaml SKUShortResponse (lines 2127-2154).
    Uses stock_quantity (not active_quantity) to match openapi.
    """
    id: UUID
    name: str
    price: int
    stock_quantity: int
    article: Optional[str]

    model_config = ConfigDict(from_attributes=True)


class ProductResponse(BaseModel):
    """
    Response schema for POST /api/v1/products (201) and GET /api/v1/products/{id}.
    Based on canon b2b-flows.md#create-product response and openapi.yaml ProductResponse.

    Key fields to match canon (fixing other teams' errors):
    1. deleted: bool - required by canon (b2b-flows.md:90), missing in openapi
    2. blocked: bool - required by canon (b2b-flows.md:91), missing in openapi
    3. category_id: UUID - openapi uses flat category_id (openapi.yaml:1750-1752), not nested object
    4. skus: [] - empty array on creation (no SKU yet)
    5. status: CREATED by default

    Note: We return category_id (flat) to match openapi, even though canon shows nested category object.
    This is the safest choice - returning nested would be a breaking change for clients expecting flat.
    """
    id: UUID
    seller_id: UUID
    category_id: UUID
    title: str
    description: Optional[str]
    status: ProductStatus
    deleted: bool
    blocked: bool
    images: List[ProductImageResponse]
    characteristics: List[ProductCharacteristicResponse]
    skus: List[SKUShortResponse]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ErrorResponse(BaseModel):
    """
    Error response schema matching canon b2b-flows.md error format.
    Used for 400, 403, 404 responses.
    """
    code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable error message")
