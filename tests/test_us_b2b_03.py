"""
US-B2B-03: Edit Product / SKU.

Canon flow b2b-flows.md#edit-product. Covered scenarios:
- edit_moderated_product_returns_to_on_moderation
- edit_blocked_product_returns_to_on_moderation
- reserves_preserved_after_sku_edit
- edit_hard_blocked_returns_403
- edit_others_product_returns_403
plus edge cases (CREATED stays CREATED, 404, SKU edit transitions parent, 400).
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
from unittest.mock import patch, AsyncMock

from backend.main import app
from backend.database import Base, get_db
from backend.modules.auth.models import Seller
from backend.modules.categories.models import Category
from backend.modules.products.models import Product, ProductStatus, SKU
from backend.modules.products.service import ProductService
from backend.core.auth import SECRET_KEY, ALGORITHM


# Test database URL (CI: localhost; docker-compose pytest: host postgres or override)
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

# Patch target for the synchronous Moderation notification.
_EVENT_PATCH = "backend.modules.products.service.ProductService._send_moderation_event"


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


async def make_product(db_session, seller, category, status, *, blocked=False):
    """Persist a product with the given status."""
    product = Product(
        id=uuid4(),
        seller_id=seller.id,
        category_id=category.id,
        title="Editable Product",
        slug=f"editable-product-{uuid4().hex[:8]}",
        description="Original description",
        status=status,
        deleted=False,
        blocked=blocked,
    )
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)
    return product


async def make_sku(db_session, product, *, reserved_quantity=0):
    """Persist a SKU under the given product."""
    sku = SKU(
        id=uuid4(),
        product_id=product.id,
        name="Original SKU",
        price=10000,
        cost_price=5000,
        discount=0,
        stock_quantity=100,
        reserved_quantity=reserved_quantity,
    )
    db_session.add(sku)
    await db_session.commit()
    await db_session.refresh(sku)
    return sku


# ───────────────────── Product edit ─────────────────────

@pytest.mark.asyncio
async def test_edit_moderated_product_returns_to_on_moderation(
    client, db_session, seller, category
):
    """
    Canon: MODERATED → ON_MODERATION + событие EDITED при редактировании.
    """
    product = await make_product(db_session, seller, category, ProductStatus.MODERATED)
    token = create_access_token(str(seller.id))

    with patch(_EVENT_PATCH, new_callable=AsyncMock) as mock_send:
        response = await client.patch(
            f"/api/v1/products/{product.id}",
            json={"title": "Updated title", "description": "Updated description"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ON_MODERATION"
    assert data["title"] == "Updated title"

    await db_session.refresh(product)
    assert product.status == ProductStatus.ON_MODERATION

    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["event_type"] == "EDITED"
    assert mock_send.call_args.kwargs["product_id"] == product.id


@pytest.mark.asyncio
async def test_edit_blocked_product_returns_to_on_moderation(
    client, db_session, seller, category
):
    """
    Canon: BLOCKED → ON_MODERATION + событие EDITED (повторная проверка после правок).
    """
    product = await make_product(
        db_session, seller, category, ProductStatus.BLOCKED, blocked=True
    )
    token = create_access_token(str(seller.id))

    with patch(_EVENT_PATCH, new_callable=AsyncMock) as mock_send:
        response = await client.patch(
            f"/api/v1/products/{product.id}",
            json={"description": "Fixed per moderator comments"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ON_MODERATION"

    await db_session.refresh(product)
    assert product.status == ProductStatus.ON_MODERATION

    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["event_type"] == "EDITED"


@pytest.mark.asyncio
async def test_edit_created_product_stays_created_no_event(
    client, db_session, seller, category
):
    """
    Canon: CREATED → CREATED, статус не меняется, событие не отправляется.
    """
    product = await make_product(db_session, seller, category, ProductStatus.CREATED)
    token = create_access_token(str(seller.id))

    with patch(_EVENT_PATCH, new_callable=AsyncMock) as mock_send:
        response = await client.patch(
            f"/api/v1/products/{product.id}",
            json={"title": "New title"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "CREATED"

    await db_session.refresh(product)
    assert product.status == ProductStatus.CREATED
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_edit_hard_blocked_returns_403(client, db_session, seller, category):
    """
    Canon: правка HARD_BLOCKED товара запрещена → 403 FORBIDDEN.
    """
    product = await make_product(
        db_session, seller, category, ProductStatus.HARD_BLOCKED, blocked=True
    )
    token = create_access_token(str(seller.id))

    response = await client.patch(
        f"/api/v1/products/{product.id}",
        json={"title": "Trying to edit"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    data = response.json()
    assert data["code"] == "FORBIDDEN"
    assert "hard-blocked" in data["message"].lower()

    await db_session.refresh(product)
    assert product.status == ProductStatus.HARD_BLOCKED


@pytest.mark.asyncio
async def test_edit_others_product_returns_403(client, db_session, seller, category):
    """
    Canon (IDOR): правка чужого товара → 403 NOT_OWNER. seller_id берётся из JWT.
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

    other_product = await make_product(
        db_session, other_seller, category, ProductStatus.MODERATED
    )
    # Token of the FIRST seller — not the owner of other_product.
    token = create_access_token(str(seller.id))

    response = await client.patch(
        f"/api/v1/products/{other_product.id}",
        json={"title": "Hijack attempt"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_OWNER"

    await db_session.refresh(other_product)
    assert other_product.status == ProductStatus.MODERATED


@pytest.mark.asyncio
async def test_edit_product_not_found_returns_404(client, seller):
    """Несуществующий product_id → 404 NOT_FOUND."""
    token = create_access_token(str(seller.id))

    response = await client.patch(
        f"/api/v1/products/{uuid4()}",
        json={"title": "Whatever"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_edit_product_invalid_category_returns_400(
    client, db_session, seller, category
):
    """Несуществующий category_id → 400 INVALID_REQUEST."""
    product = await make_product(db_session, seller, category, ProductStatus.CREATED)
    token = create_access_token(str(seller.id))

    response = await client.patch(
        f"/api/v1/products/{product.id}",
        json={"category_id": str(uuid4())},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"


# ───────────────────── SKU edit ─────────────────────

@pytest.mark.asyncio
async def test_reserves_preserved_after_sku_edit(client, db_session, seller, category):
    """
    Canon: при PUT/PATCH SKU активные резервы сохраняются — reserved_quantity не меняется.
    """
    product = await make_product(db_session, seller, category, ProductStatus.MODERATED)
    sku = await make_sku(db_session, product, reserved_quantity=7)
    token = create_access_token(str(seller.id))

    with patch(_EVENT_PATCH, new_callable=AsyncMock):
        response = await client.patch(
            f"/api/v1/skus/{sku.id}",
            json={"price": 15000, "name": "Updated SKU"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["price"] == 15000
    assert data["name"] == "Updated SKU"
    # Reserves untouched by the edit.
    assert data["reserved_quantity"] == 7

    await db_session.refresh(sku)
    assert sku.reserved_quantity == 7


@pytest.mark.asyncio
async def test_edit_sku_returns_parent_to_on_moderation(
    client, db_session, seller, category
):
    """
    Canon: редактирование SKU возвращает родительский товар в ON_MODERATION
    (MODERATED → ON_MODERATION) + событие EDITED.
    """
    product = await make_product(db_session, seller, category, ProductStatus.MODERATED)
    sku = await make_sku(db_session, product)
    token = create_access_token(str(seller.id))

    with patch(_EVENT_PATCH, new_callable=AsyncMock) as mock_send:
        response = await client.patch(
            f"/api/v1/skus/{sku.id}",
            json={"price": 20000},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200

    await db_session.refresh(product)
    assert product.status == ProductStatus.ON_MODERATION

    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["event_type"] == "EDITED"
    assert mock_send.call_args.kwargs["product_id"] == product.id


@pytest.mark.asyncio
async def test_edit_sku_hard_blocked_returns_403(client, db_session, seller, category):
    """Canon: правка SKU у HARD_BLOCKED товара → 403 FORBIDDEN."""
    product = await make_product(
        db_session, seller, category, ProductStatus.HARD_BLOCKED, blocked=True
    )
    sku = await make_sku(db_session, product)
    token = create_access_token(str(seller.id))

    response = await client.patch(
        f"/api/v1/skus/{sku.id}",
        json={"price": 20000},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_edit_others_sku_returns_403(client, db_session, seller, category):
    """Canon (IDOR): правка SKU чужого товара → 403 NOT_OWNER."""
    other_seller = Seller(
        id=uuid4(),
        email="other2@test.com",
        hashed_password="hashed",
        first_name="Other",
        last_name="Seller",
        company_name="Other Company",
    )
    db_session.add(other_seller)
    await db_session.commit()

    other_product = await make_product(
        db_session, other_seller, category, ProductStatus.MODERATED
    )
    sku = await make_sku(db_session, other_product)
    token = create_access_token(str(seller.id))

    response = await client.patch(
        f"/api/v1/skus/{sku.id}",
        json={"price": 20000},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_OWNER"


@pytest.mark.asyncio
async def test_edit_sku_not_found_returns_404(client, seller):
    """Несуществующий sku_id → 404 NOT_FOUND."""
    token = create_access_token(str(seller.id))

    response = await client.patch(
        f"/api/v1/skus/{uuid4()}",
        json={"price": 20000},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_edit_sku_invalid_price_returns_400(client, db_session, seller, category):
    """price <= 0 → 400 INVALID_REQUEST (mirrors B2B-2 validation)."""
    product = await make_product(db_session, seller, category, ProductStatus.CREATED)
    sku = await make_sku(db_session, product)
    token = create_access_token(str(seller.id))

    response = await client.patch(
        f"/api/v1/skus/{sku.id}",
        json={"price": 0},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    data = response.json()
    assert data["code"] == "INVALID_REQUEST"
    assert "price" in data["message"].lower()
