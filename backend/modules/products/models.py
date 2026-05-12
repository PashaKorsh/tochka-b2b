from sqlalchemy import Column, String, DateTime, ForeignKey, Integer, Boolean, Enum as SQLEnum, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid
import enum

from backend.database import Base


class ProductStatus(str, enum.Enum):
    CREATED = "CREATED"
    ON_MODERATION = "ON_MODERATION"
    MODERATED = "MODERATED"
    BLOCKED = "BLOCKED"
    HARD_BLOCKED = "HARD_BLOCKED"


class Product(Base):
    __tablename__ = "products"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seller_id = Column(UUID(as_uuid=True), ForeignKey("sellers.id"), nullable=False)
    category_id = Column(UUID(as_uuid=True), ForeignKey("categories.id"), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(SQLEnum(ProductStatus), default=ProductStatus.CREATED, nullable=False)
    deleted = Column(Boolean, default=False, nullable=False)
    blocked = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    seller = relationship("Seller", back_populates="products")
    category = relationship("Category", back_populates="products")
    images = relationship("ProductImage", back_populates="product", cascade="all, delete-orphan")
    characteristics = relationship("ProductCharacteristic", back_populates="product", cascade="all, delete-orphan")
    skus = relationship("SKU", back_populates="product", cascade="all, delete-orphan")


class ProductImage(Base):
    __tablename__ = "product_images"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    url = Column(String(2000), nullable=False)
    ordering = Column(Integer, default=0, nullable=False)

    product = relationship("Product", back_populates="images")


class ProductCharacteristic(Base):
    __tablename__ = "product_characteristics"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    name = Column(String(200), nullable=False)
    value = Column(String(2000), nullable=False)

    product = relationship("Product", back_populates="characteristics")


class SKU(Base):
    __tablename__ = "skus"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    name = Column(String(255), nullable=False)
    price = Column(Integer, nullable=False)
    stock_quantity = Column(Integer, default=0, nullable=False)
    reserved_quantity = Column(Integer, default=0, nullable=False)
    article = Column(String(255), nullable=True)
    cost_price = Column(Integer, nullable=True)
    discount = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    product = relationship("Product", back_populates="skus")
    images = relationship("SKUImage", back_populates="sku", cascade="all, delete-orphan")
    characteristics = relationship("SKUCharacteristic", back_populates="sku", cascade="all, delete-orphan")


class SKUImage(Base):
    __tablename__ = "sku_images"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sku_id = Column(UUID(as_uuid=True), ForeignKey("skus.id"), nullable=False)
    url = Column(String(2000), nullable=False)
    ordering = Column(Integer, default=0, nullable=False)

    sku = relationship("SKU", back_populates="images")


class SKUCharacteristic(Base):
    __tablename__ = "sku_characteristics"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sku_id = Column(UUID(as_uuid=True), ForeignKey("skus.id"), nullable=False)
    name = Column(String(200), nullable=False)
    value = Column(String(2000), nullable=False)

    sku = relationship("SKU", back_populates="characteristics")
