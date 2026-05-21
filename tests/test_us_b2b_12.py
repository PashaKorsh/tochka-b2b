"""
US-B2B-12: Delete SKU.

Canon flow b2b-flows.md#delete-sku. Covered scenarios:
- delete_sku_succeeds
- delete_sku_with_active_reserves_returns_409
- last_sku_on_moderation_transitions_product_to_created
- delete_sku_hard_blocked_product_returns_403
- sku_out_of_stock_event_on_moderated_product
plus 404 and IDOR (other seller's SKU).
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

_MOD_EVENT = "backend.modules.products.service.ProductService._send_moderation_event"
_OOS_EVENT = "backend.modules.inventory.service.InventoryService._send_sku_out_of_stock"


@pytest_asyncio.fixture
async def db_session():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with TestSessionLocal() as session:
        yield session
        await session.close()


@pytest_asyncio.fixture
async def client(db_session):
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
    category = Category(id=uuid4(), name="Test Category")
    db_session.add(category)
    await db_session.commit()
    await db_session.refresh(category)
    return category


def create_access_token(seller_id: str) -> str:
    to_encode = {"sub": seller_id, "exp": datetime.utcnow() + timedelta(hours=1)}
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


async def make_product(db_session, seller, category, *, status=ProductStatus.MODERATED):
    product = Product(
        id=uuid4(),
        seller_id=seller.id,
        category_id=category.id,
        title="Variant Product",
        slug=f"var-{uuid4().hex[:8]}",
        description="Description",
        status=status,
        deleted=False,
        blocked=(status in (ProductStatus.BLOCKED, ProductStatus.HARD_BLOCKED)),
    )
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)
    return product


async def make_sku(
    db_session, product, *, stock_quantity=10, reserved_quantity=0,
):
    sku = SKU(
        id=uuid4(),
        product_id=product.id,
        name="Variant",
        price=10000,
        cost_price=5000,
        discount=0,
        stock_quantity=stock_quantity,
        reserved_quantity=reserved_quantity,
    )
    db_session.add(sku)
    await db_session.commit()
    await db_session.refresh(sku)
    return sku


@pytest.mark.asyncio
async def test_delete_sku_succeeds(client, db_session, seller, category):
    """Canon happy path: SKU без резервов удаляется."""
    product = await make_product(db_session, seller, category)
    # Two SKUs so we don't trigger the "last SKU" side-effect.
    keep = await make_sku(db_session, product)
    target = await make_sku(db_session, product)
    token = create_access_token(str(seller.id))

    with patch(_MOD_EVENT, new_callable=AsyncMock), \
            patch(_OOS_EVENT, new_callable=AsyncMock):
        response = await client.delete(
            f"/api/v1/skus/{target.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 204
    # Target SKU is gone, the sibling remains.
    assert (await db_session.get(SKU, target.id)) is None
    assert (await db_session.get(SKU, keep.id)) is not None


@pytest.mark.asyncio
async def test_delete_sku_with_active_reserves_returns_409(
    client, db_session, seller, category
):
    """Canon: SKU с reserved_quantity > 0 → 409, SKU не удалён."""
    product = await make_product(db_session, seller, category)
    sku = await make_sku(db_session, product, stock_quantity=10, reserved_quantity=3)
    token = create_access_token(str(seller.id))

    with patch(_MOD_EVENT, new_callable=AsyncMock), \
            patch(_OOS_EVENT, new_callable=AsyncMock):
        response = await client.delete(
            f"/api/v1/skus/{sku.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 409
    data = response.json()
    assert data["code"] == "CONFLICT"
    assert "reserve" in data["message"].lower()
    assert (await db_session.get(SKU, sku.id)) is not None


@pytest.mark.asyncio
async def test_last_sku_on_moderation_transitions_product_to_created(
    client, db_session, seller, category
):
    """
    Canon: последний SKU удалён + товар ON_MODERATION → товар → CREATED
    + событие DELETED в Moderation.
    """
    product = await make_product(
        db_session, seller, category, status=ProductStatus.ON_MODERATION
    )
    sku = await make_sku(db_session, product)
    token = create_access_token(str(seller.id))

    with patch(_MOD_EVENT, new_callable=AsyncMock) as mock_mod, \
            patch(_OOS_EVENT, new_callable=AsyncMock):
        response = await client.delete(
            f"/api/v1/skus/{sku.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 204

    await db_session.refresh(product)
    assert product.status == ProductStatus.CREATED

    mock_mod.assert_awaited_once()
    assert mock_mod.call_args.kwargs["event_type"] == "DELETED"
    assert mock_mod.call_args.kwargs["product_id"] == product.id


@pytest.mark.asyncio
async def test_delete_sku_hard_blocked_product_returns_403(
    client, db_session, seller, category
):
    """Canon: товар HARD_BLOCKED → 403, SKU не удалён."""
    product = await make_product(
        db_session, seller, category, status=ProductStatus.HARD_BLOCKED
    )
    sku = await make_sku(db_session, product)
    token = create_access_token(str(seller.id))

    response = await client.delete(
        f"/api/v1/skus/{sku.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert (await db_session.get(SKU, sku.id)) is not None


@pytest.mark.asyncio
async def test_sku_out_of_stock_event_on_moderated_product(
    client, db_session, seller, category
):
    """
    Canon: SKU с active_quantity > 0 на MODERATED товаре удалён →
    событие SKU_OUT_OF_STOCK в B2C.
    """
    product = await make_product(db_session, seller, category)
    # Two SKUs — деление одного не сделает SKU «последним»;
    # active_quantity у target = 10 - 0 > 0.
    await make_sku(db_session, product)
    target = await make_sku(db_session, product, stock_quantity=10)
    token = create_access_token(str(seller.id))

    with patch(_MOD_EVENT, new_callable=AsyncMock), \
            patch(_OOS_EVENT, new_callable=AsyncMock) as mock_oos:
        response = await client.delete(
            f"/api/v1/skus/{target.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 204
    mock_oos.assert_awaited_once_with(product.id, target.id)


@pytest.mark.asyncio
async def test_delete_sku_not_found_returns_404(client, seller):
    """Несуществующий sku_id → 404."""
    token = create_access_token(str(seller.id))

    response = await client.delete(
        f"/api/v1/skus/{uuid4()}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_delete_others_sku_returns_403(client, db_session, seller, category):
    """Canon (IDOR): удаление SKU чужого продавца → 403 NOT_OWNER."""
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
    other_sku = await make_sku(db_session, other_product)
    token = create_access_token(str(seller.id))

    response = await client.delete(
        f"/api/v1/skus/{other_sku.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_OWNER"
    assert (await db_session.get(SKU, other_sku.id)) is not None
