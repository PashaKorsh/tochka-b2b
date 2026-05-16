"""
US-B2B-04: Delete Product (soft delete).

Canon flow b2b-flows.md#delete-product. Covered scenarios:
- delete_sets_deleted_true
- delete_emits_event_to_moderation
- delete_emits_product_deleted_to_b2c
- delete_already_deleted_returns_400
- delete_others_product_returns_403
- deleted_product_not_in_seller_list
plus edge cases (404, include_deleted filter).
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

# Patch targets for the cascade-event senders.
_MOD_EVENT = "backend.modules.products.service.ProductService._send_moderation_event"
_B2C_EVENT = "backend.modules.products.service.ProductService._send_b2c_event"


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


async def make_product(db_session, seller, category, *, title="Deletable Product"):
    """Persist a CREATED product."""
    product = Product(
        id=uuid4(),
        seller_id=seller.id,
        category_id=category.id,
        title=title,
        slug=f"deletable-{uuid4().hex[:8]}",
        description="Description",
        status=ProductStatus.CREATED,
        deleted=False,
        blocked=False,
    )
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)
    return product


async def make_sku(db_session, product):
    """Persist a SKU under the given product."""
    sku = SKU(
        id=uuid4(),
        product_id=product.id,
        name="SKU",
        price=10000,
        cost_price=5000,
        discount=0,
        stock_quantity=10,
        reserved_quantity=0,
    )
    db_session.add(sku)
    await db_session.commit()
    await db_session.refresh(sku)
    return sku


@pytest.mark.asyncio
async def test_delete_sets_deleted_true(client, db_session, seller, category):
    """Canon: soft delete — поле deleted=true в БД, ответ 204."""
    product = await make_product(db_session, seller, category)
    token = create_access_token(str(seller.id))

    with patch(_MOD_EVENT, new_callable=AsyncMock), patch(_B2C_EVENT, new_callable=AsyncMock):
        response = await client.delete(
            f"/api/v1/products/{product.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 204

    await db_session.refresh(product)
    assert product.deleted is True


@pytest.mark.asyncio
async def test_delete_emits_event_to_moderation(client, db_session, seller, category):
    """Canon: при удалении событие DELETED уходит в Moderation."""
    product = await make_product(db_session, seller, category)
    token = create_access_token(str(seller.id))

    with patch(_MOD_EVENT, new_callable=AsyncMock) as mock_mod, \
            patch(_B2C_EVENT, new_callable=AsyncMock):
        response = await client.delete(
            f"/api/v1/products/{product.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 204
    mock_mod.assert_called_once()
    assert mock_mod.call_args.kwargs["event_type"] == "DELETED"
    assert mock_mod.call_args.kwargs["product_id"] == product.id
    assert mock_mod.call_args.kwargs["seller_id"] == seller.id


@pytest.mark.asyncio
async def test_delete_emits_product_deleted_to_b2c(client, db_session, seller, category):
    """Canon: событие PRODUCT_DELETED уходит в B2C со списком sku_ids."""
    product = await make_product(db_session, seller, category)
    sku1 = await make_sku(db_session, product)
    sku2 = await make_sku(db_session, product)
    token = create_access_token(str(seller.id))

    with patch(_MOD_EVENT, new_callable=AsyncMock), \
            patch(_B2C_EVENT, new_callable=AsyncMock) as mock_b2c:
        response = await client.delete(
            f"/api/v1/products/{product.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 204
    mock_b2c.assert_called_once()
    kwargs = mock_b2c.call_args.kwargs
    assert kwargs["event_type"] == "PRODUCT_DELETED"
    assert kwargs["product_id"] == product.id
    assert set(kwargs["sku_ids"]) == {str(sku1.id), str(sku2.id)}


@pytest.mark.asyncio
async def test_delete_already_deleted_returns_400(client, db_session, seller, category):
    """Canon: повторное удаление уже удалённого товара → 400."""
    product = await make_product(db_session, seller, category)
    token = create_access_token(str(seller.id))
    headers = {"Authorization": f"Bearer {token}"}

    with patch(_MOD_EVENT, new_callable=AsyncMock), patch(_B2C_EVENT, new_callable=AsyncMock):
        first = await client.delete(f"/api/v1/products/{product.id}", headers=headers)
        assert first.status_code == 204

        second = await client.delete(f"/api/v1/products/{product.id}", headers=headers)

    assert second.status_code == 400
    data = second.json()
    assert data["code"] == "INVALID_REQUEST"
    assert "already deleted" in data["message"].lower()


@pytest.mark.asyncio
async def test_delete_others_product_returns_403(client, db_session, seller, category):
    """Canon (IDOR): удаление чужого товара → 403 NOT_OWNER."""
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

    response = await client.delete(
        f"/api/v1/products/{other_product.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_OWNER"

    await db_session.refresh(other_product)
    assert other_product.deleted is False


@pytest.mark.asyncio
async def test_delete_product_not_found_returns_404(client, seller):
    """Несуществующий product_id → 404 NOT_FOUND."""
    token = create_access_token(str(seller.id))

    response = await client.delete(
        f"/api/v1/products/{uuid4()}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_deleted_product_not_in_seller_list(client, db_session, seller, category):
    """
    Canon: удалённый товар не виден в стандартном списке продавца,
    но доступен с include_deleted=true.
    """
    kept = await make_product(db_session, seller, category, title="Kept Product")
    removed = await make_product(db_session, seller, category, title="Removed Product")
    token = create_access_token(str(seller.id))
    headers = {"Authorization": f"Bearer {token}"}

    with patch(_MOD_EVENT, new_callable=AsyncMock), patch(_B2C_EVENT, new_callable=AsyncMock):
        delete_resp = await client.delete(f"/api/v1/products/{removed.id}", headers=headers)
        assert delete_resp.status_code == 204

    # Standard list — deleted product excluded.
    list_resp = await client.get("/api/v1/products", headers=headers)
    assert list_resp.status_code == 200
    data = list_resp.json()
    ids = {item["id"] for item in data["items"]}
    assert str(kept.id) in ids
    assert str(removed.id) not in ids
    assert data["total_count"] == 1

    # include_deleted=true — deleted product is present.
    list_all = await client.get("/api/v1/products?include_deleted=true", headers=headers)
    assert list_all.status_code == 200
    all_data = list_all.json()
    all_ids = {item["id"] for item in all_data["items"]}
    assert str(removed.id) in all_ids
    assert all_data["total_count"] == 2
    removed_item = next(i for i in all_data["items"] if i["id"] == str(removed.id))
    assert removed_item["deleted"] is True
