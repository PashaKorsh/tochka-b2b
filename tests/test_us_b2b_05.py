"""
US-B2B-05: View Product Card.

Canon flow b2b-flows.md#view-product. Covered scenarios:
- get_moderated_product_returns_full_payload
- get_blocked_product_returns_blocking_reason_and_field_reports
- get_others_product_returns_404
- get_nonexistent_returns_404
plus 401 (no auth) and the cross-service public view (X-Service-Key).
"""
import os

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool
from uuid import uuid4
from jose import jwt
from datetime import datetime, timedelta

from backend.main import app
from backend.database import Base, get_db
from backend.modules.auth.models import Seller
from backend.modules.categories.models import Category
from backend.modules.products.models import (
    Product, ProductStatus, SKU, BlockingReason, FieldReport,
)
from backend.core.auth import SECRET_KEY, ALGORITHM


TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/tochkab2b",
)

test_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, echo=False)

TestSessionLocal = async_sessionmaker(
    test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Must match SERVICE_API_KEY default in router.get_product_viewer.
SERVICE_KEY = "dev-service-key"


@pytest_asyncio.fixture
async def db_session():
    """Create test database tables and provide session"""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with TestSessionLocal() as session:
        yield session
        await session.close()


@pytest_asyncio.fixture
async def client(db_session):
    """Create test client with database override"""
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def seller(db_session):
    """Create test seller"""
    seller = Seller(
        id=uuid4(),
        email="seller@test.com",
        hashed_password="hashed",
        first_name="Test",
        last_name="Seller",
        company_name="Test Company",
    )
    db_session.add(seller)
    await db_session.commit()
    await db_session.refresh(seller)
    return seller


@pytest_asyncio.fixture
async def category(db_session):
    """Create test category"""
    category = Category(id=uuid4(), name="Test Category")
    db_session.add(category)
    await db_session.commit()
    await db_session.refresh(category)
    return category


def create_access_token(seller_id: str) -> str:
    """Create JWT token for testing"""
    to_encode = {"sub": seller_id, "exp": datetime.utcnow() + timedelta(hours=1)}
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


async def make_product(
    db_session, seller, category, *,
    status=ProductStatus.MODERATED,
    blocking_reason_id=None,
    moderator_comment=None,
):
    """Persist a product with the given status / moderation feedback."""
    product = Product(
        id=uuid4(),
        seller_id=seller.id,
        category_id=category.id,
        title="Viewable Product",
        slug=f"viewable-{uuid4().hex[:8]}",
        description="Product description",
        status=status,
        deleted=False,
        blocked=(status in (ProductStatus.BLOCKED, ProductStatus.HARD_BLOCKED)),
        blocking_reason_id=blocking_reason_id,
        moderator_comment=moderator_comment,
    )
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)
    return product


async def make_sku(db_session, product):
    """Persist a SKU with seller-only fields populated."""
    sku = SKU(
        id=uuid4(),
        product_id=product.id,
        name="Variant",
        price=12000,
        cost_price=8000,
        discount=0,
        stock_quantity=20,
        reserved_quantity=3,
    )
    db_session.add(sku)
    await db_session.commit()
    await db_session.refresh(sku)
    return sku


async def make_blocking_reason(db_session, title="Запрещённый контент"):
    reason = BlockingReason(id=uuid4(), title=title)
    db_session.add(reason)
    await db_session.commit()
    await db_session.refresh(reason)
    return reason


async def make_field_report(db_session, product, field_name, comment, sku_id=None):
    report = FieldReport(
        id=uuid4(),
        product_id=product.id,
        field_name=field_name,
        comment=comment,
        sku_id=sku_id,
    )
    db_session.add(report)
    await db_session.commit()
    await db_session.refresh(report)
    return report


@pytest.mark.asyncio
async def test_get_moderated_product_returns_full_payload(
    client, db_session, seller, category
):
    """
    Canon: MODERATED — поля товара, skus с cost_price, blocking_reason=null.
    """
    product = await make_product(db_session, seller, category, status=ProductStatus.MODERATED)
    await make_sku(db_session, product)
    token = create_access_token(str(seller.id))

    response = await client.get(
        f"/api/v1/products/{product.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(product.id)
    assert data["status"] == "MODERATED"
    assert data["title"] == "Viewable Product"
    # seller-view SKU exposes cost_price / reserved_quantity
    assert len(data["skus"]) == 1
    assert data["skus"][0]["cost_price"] == 8000
    assert data["skus"][0]["reserved_quantity"] == 3
    # not blocked → no moderation feedback
    assert data["blocking_reason"] is None
    assert data["field_reports"] == []


@pytest.mark.asyncio
async def test_get_blocked_product_returns_blocking_reason_and_field_reports(
    client, db_session, seller, category
):
    """
    Canon: BLOCKED — blocking_reason.title и массив field_reports с замечаниями.
    """
    reason = await make_blocking_reason(db_session, title="Запрещённый контент")
    product = await make_product(
        db_session, seller, category,
        status=ProductStatus.BLOCKED,
        blocking_reason_id=reason.id,
        moderator_comment="Уберите запрещённые формулировки и замените фото.",
    )
    sku = await make_sku(db_session, product)
    await make_field_report(
        db_session, product, "title", "Название содержит запрещённое слово.",
    )
    await make_field_report(
        db_session, product, "sku_price", "Цена ниже допустимой.", sku_id=sku.id,
    )
    token = create_access_token(str(seller.id))

    response = await client.get(
        f"/api/v1/products/{product.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "BLOCKED"

    assert data["blocking_reason"] is not None
    assert data["blocking_reason"]["id"] == str(reason.id)
    assert data["blocking_reason"]["title"] == "Запрещённый контент"
    assert data["blocking_reason"]["comment"] == (
        "Уберите запрещённые формулировки и замените фото."
    )

    reports = data["field_reports"]
    assert len(reports) == 2
    by_field = {r["field_name"]: r for r in reports}
    assert by_field["title"]["sku_id"] is None
    assert by_field["title"]["comment"]
    assert by_field["sku_price"]["sku_id"] == str(sku.id)


@pytest.mark.asyncio
async def test_get_others_product_returns_404(client, db_session, seller, category):
    """
    Canon: чужой товар → 404 (не 403) — не раскрываем факт существования.
    """
    other_seller = Seller(
        id=uuid4(),
        email="other@test.com",
        hashed_password="hashed",
        first_name="Other",
        last_name="Seller",
        company_name="Other Company",
    )
    db_session.add(other_seller)
    await db_session.commit()

    other_product = await make_product(db_session, other_seller, category)
    token = create_access_token(str(seller.id))

    response = await client.get(
        f"/api/v1/products/{other_product.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_get_nonexistent_returns_404(client, seller):
    """Canon: несуществующий ID → 404."""
    token = create_access_token(str(seller.id))

    response = await client.get(
        f"/api/v1/products/{uuid4()}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_get_product_without_auth_returns_401(client, db_session, seller, category):
    """Без JWT и без X-Service-Key → 401."""
    product = await make_product(db_session, seller, category)

    response = await client.get(f"/api/v1/products/{product.id}")

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_get_product_with_service_key_returns_public_view(
    client, db_session, seller, category
):
    """
    Межсервисный режим (X-Service-Key): ProductPublicResponse —
    без cost_price / reserved_quantity и без модерационной обратной связи.
    """
    reason = await make_blocking_reason(db_session)
    product = await make_product(
        db_session, seller, category,
        status=ProductStatus.BLOCKED,
        blocking_reason_id=reason.id,
        moderator_comment="internal note",
    )
    await make_sku(db_session, product)

    response = await client.get(
        f"/api/v1/products/{product.id}",
        headers={"X-Service-Key": SERVICE_KEY},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(product.id)
    # Sensitive seller-only fields must not leak into the public view.
    assert "blocking_reason" not in data
    assert "field_reports" not in data
    assert len(data["skus"]) == 1
    assert "cost_price" not in data["skus"][0]
    assert "reserved_quantity" not in data["skus"][0]
