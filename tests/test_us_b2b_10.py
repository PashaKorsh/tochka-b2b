"""
US-B2B-10: Fulfill delivery — final step of the reserve lifecycle.

Canon flow b2b-flows.md#fulfill-delivery. Covered scenarios:
- fulfill_decreases_reserved_quantity
- active_quantity_unchanged
- idempotent_fulfill_no_double_deduction
plus missing service key (401).
"""
import os

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool
from uuid import uuid4

from backend.main import app
from backend.database import Base, get_db
from backend.modules.auth.models import Seller
from backend.modules.categories.models import Category
from backend.modules.products.models import Product, ProductStatus, SKU


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

SERVICE_HEADERS = {"X-Service-Key": "dev-service-key"}


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
async def reserved_sku(db_session):
    """
    A SKU with an active reservation: stock_quantity=10, reserved_quantity=4
    → active_quantity = 6. The fulfill tests will deliver part of the reserve.
    """
    seller = Seller(
        id=uuid4(),
        email="seller@test.com",
        hashed_password="hashed",
        first_name="Test",
        last_name="Seller",
        company_name="Test Company",
    )
    category = Category(id=uuid4(), name="Test Category")
    db_session.add_all([seller, category])
    await db_session.flush()

    product = Product(
        id=uuid4(),
        seller_id=seller.id,
        category_id=category.id,
        title="Fulfilled Product",
        slug=f"ful-{uuid4().hex[:8]}",
        description="Description",
        status=ProductStatus.MODERATED,
        deleted=False,
        blocked=False,
    )
    db_session.add(product)
    await db_session.flush()

    sku = SKU(
        id=uuid4(),
        product_id=product.id,
        name="Variant",
        price=10000,
        cost_price=5000,
        discount=0,
        stock_quantity=10,
        reserved_quantity=4,
    )
    db_session.add(sku)
    await db_session.commit()
    await db_session.refresh(sku)
    return sku


@pytest.mark.asyncio
async def test_fulfill_decreases_reserved_quantity(client, db_session, reserved_sku):
    """Canon: fulfill уменьшает reserved_quantity на доставленное количество.

    Ответ соответствует spec InventoryOrderResponse {order_id, status, processed_at}.
    """
    order_id = uuid4()
    response = await client.post(
        "/api/v1/inventory/fulfill",
        json={
            "order_id": str(order_id),
            "items": [{"sku_id": str(reserved_sku.id), "quantity": 3}],
        },
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["order_id"] == str(order_id)
    assert body["status"] == "FULFILLED"
    assert "processed_at" in body and body["processed_at"]

    await db_session.refresh(reserved_sku)
    # reserved 4 - 3 = 1; stock 10 - 3 = 7 (товар физически уехал)
    assert reserved_sku.reserved_quantity == 1
    assert reserved_sku.stock_quantity == 7


@pytest.mark.asyncio
async def test_active_quantity_unchanged(client, db_session, reserved_sku):
    """Canon: active_quantity (= stock − reserved) НЕ меняется после fulfill."""
    before = reserved_sku.stock_quantity - reserved_sku.reserved_quantity

    response = await client.post(
        "/api/v1/inventory/fulfill",
        json={
            "order_id": str(uuid4()),
            "items": [{"sku_id": str(reserved_sku.id), "quantity": 3}],
        },
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 200
    await db_session.refresh(reserved_sku)
    after = reserved_sku.stock_quantity - reserved_sku.reserved_quantity
    assert after == before


@pytest.mark.asyncio
async def test_idempotent_fulfill_no_double_deduction(client, db_session, reserved_sku):
    """Canon: повтор с тем же order_id → 200 без двойного списания."""
    order_id = str(uuid4())
    payload = {
        "order_id": order_id,
        "items": [{"sku_id": str(reserved_sku.id), "quantity": 3}],
    }

    first = await client.post("/api/v1/inventory/fulfill", json=payload, headers=SERVICE_HEADERS)
    second = await client.post("/api/v1/inventory/fulfill", json=payload, headers=SERVICE_HEADERS)

    assert first.status_code == 200
    assert second.status_code == 200
    # Replay returns the stored InventoryOrderResponse byte-for-byte; processed_at
    # is the original timestamp (no second deduction took place).
    assert first.json() == second.json()
    assert first.json()["order_id"] == order_id
    assert first.json()["status"] == "FULFILLED"

    await db_session.refresh(reserved_sku)
    # Deducted exactly once: reserved 4 - 3 = 1, stock 10 - 3 = 7.
    assert reserved_sku.reserved_quantity == 1
    assert reserved_sku.stock_quantity == 7


@pytest.mark.asyncio
async def test_fulfill_insufficient_reserved_returns_409(client, db_session, reserved_sku):
    """
    Canon: при reserved_quantity < requested quantity отказ должен быть явным
    (409), а не молчаливым clamp-ом до нуля.
    """
    # reserved_sku starts with reserved_quantity=4; we try to fulfill 5.
    response = await client.post(
        "/api/v1/inventory/fulfill",
        json={
            "order_id": str(uuid4()),
            "items": [{"sku_id": str(reserved_sku.id), "quantity": 5}],
        },
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 409
    data = response.json()
    assert data["code"] == "CONFLICT"
    assert "insufficient" in data["message"].lower()

    # Reservation untouched — no partial deduction.
    await db_session.refresh(reserved_sku)
    assert reserved_sku.reserved_quantity == 4
    assert reserved_sku.stock_quantity == 10


@pytest.mark.asyncio
async def test_fulfill_empty_items_returns_422(client, reserved_sku):
    """Spec InventoryOrderRequest.items: minItems=1 — пустой список → 422."""
    response = await client.post(
        "/api/v1/inventory/fulfill",
        json={"order_id": str(uuid4()), "items": []},
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_fulfill_missing_service_key_returns_401(client, reserved_sku):
    """Без X-Service-Key → 401."""
    response = await client.post(
        "/api/v1/inventory/fulfill",
        json={
            "order_id": str(uuid4()),
            "items": [{"sku_id": str(reserved_sku.id), "quantity": 1}],
        },
    )

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
