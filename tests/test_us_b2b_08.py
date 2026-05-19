"""
US-B2B-08: Reserve / Unreserve SKU.

Canon flow b2b-flows.md#reserve-sku. Covered scenarios:
- reserve_all_skus_succeeds
- partial_insufficient_stock_returns_409_all_rollback
- idempotent_reserve_returns_200_without_double_deduction
- sku_out_of_stock_event_emitted
- unreserve_restores_quantities
plus 401 (no service key) and 404 (SKU not found).
"""
import os

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool
from uuid import uuid4
from unittest.mock import patch, AsyncMock

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
_OOS_EVENT = "backend.modules.inventory.service.InventoryService._send_sku_out_of_stock"


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
async def product(db_session):
    """Create a seller, category and a parent product for SKUs."""
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
        title="Inventory Product",
        slug=f"inv-{uuid4().hex[:8]}",
        description="Description",
        status=ProductStatus.MODERATED,
        deleted=False,
        blocked=False,
    )
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)
    return product


async def make_sku(db_session, product, *, stock_quantity, reserved_quantity=0):
    """Persist a SKU. active_quantity = stock_quantity - reserved_quantity."""
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
async def test_reserve_all_skus_succeeds(client, db_session, product):
    """Canon happy path: active_quantity уменьшился, reserved_quantity вырос."""
    sku1 = await make_sku(db_session, product, stock_quantity=10)
    sku2 = await make_sku(db_session, product, stock_quantity=10)

    with patch(_OOS_EVENT, new_callable=AsyncMock):
        response = await client.post(
            "/api/v1/reserve",
            json={
                "idempotency_key": str(uuid4()),
                "items": [
                    {"sku_id": str(sku1.id), "quantity": 3},
                    {"sku_id": str(sku2.id), "quantity": 4},
                ],
            },
            headers=SERVICE_HEADERS,
        )

    assert response.status_code == 200
    data = response.json()
    assert data["reserved"] is True
    by_sku = {item["sku_id"]: item for item in data["items"]}
    assert by_sku[str(sku1.id)]["reserved_quantity"] == 3
    assert by_sku[str(sku1.id)]["remaining_stock"] == 7

    await db_session.refresh(sku1)
    await db_session.refresh(sku2)
    assert sku1.reserved_quantity == 3
    assert sku2.reserved_quantity == 4


@pytest.mark.asyncio
async def test_partial_insufficient_stock_returns_409_all_rollback(
    client, db_session, product
):
    """Canon: одному SKU не хватает → 409, ни один SKU не зарезервирован."""
    sku_ok = await make_sku(db_session, product, stock_quantity=10)
    sku_short = await make_sku(db_session, product, stock_quantity=2)

    with patch(_OOS_EVENT, new_callable=AsyncMock):
        response = await client.post(
            "/api/v1/reserve",
            json={
                "idempotency_key": str(uuid4()),
                "items": [
                    {"sku_id": str(sku_ok.id), "quantity": 3},
                    {"sku_id": str(sku_short.id), "quantity": 5},
                ],
            },
            headers=SERVICE_HEADERS,
        )

    assert response.status_code == 409
    data = response.json()
    assert data["reserved"] is False
    failed_ids = {f["sku_id"] for f in data["failed_items"]}
    assert str(sku_short.id) in failed_ids

    # Nothing reserved — full rollback.
    await db_session.refresh(sku_ok)
    await db_session.refresh(sku_short)
    assert sku_ok.reserved_quantity == 0
    assert sku_short.reserved_quantity == 0


@pytest.mark.asyncio
async def test_idempotent_reserve_returns_200_without_double_deduction(
    client, db_session, product
):
    """Canon: повтор с тем же idempotency_key → 200 без повторного списания."""
    sku = await make_sku(db_session, product, stock_quantity=10)
    key = str(uuid4())
    payload = {
        "idempotency_key": key,
        "items": [{"sku_id": str(sku.id), "quantity": 3}],
    }

    with patch(_OOS_EVENT, new_callable=AsyncMock):
        first = await client.post("/api/v1/reserve", json=payload, headers=SERVICE_HEADERS)
        second = await client.post("/api/v1/reserve", json=payload, headers=SERVICE_HEADERS)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()

    # Reserved exactly once.
    await db_session.refresh(sku)
    assert sku.reserved_quantity == 3


@pytest.mark.asyncio
async def test_sku_out_of_stock_event_emitted(client, db_session, product):
    """Canon: active_quantity стал 0 → событие SKU_OUT_OF_STOCK уходит в B2C."""
    sku = await make_sku(db_session, product, stock_quantity=5)

    with patch(_OOS_EVENT, new_callable=AsyncMock) as mock_event:
        response = await client.post(
            "/api/v1/reserve",
            json={
                "idempotency_key": str(uuid4()),
                "items": [{"sku_id": str(sku.id), "quantity": 5}],
            },
            headers=SERVICE_HEADERS,
        )

    assert response.status_code == 200
    assert response.json()["items"][0]["remaining_stock"] == 0

    mock_event.assert_awaited_once()
    args = mock_event.call_args.args
    assert args == (product.id, sku.id)


@pytest.mark.asyncio
async def test_unreserve_restores_quantities(client, db_session, product):
    """Canon: unreserve восстанавливает active_quantity и reserved_quantity."""
    sku = await make_sku(db_session, product, stock_quantity=10)

    with patch(_OOS_EVENT, new_callable=AsyncMock):
        reserve = await client.post(
            "/api/v1/reserve",
            json={
                "idempotency_key": str(uuid4()),
                "items": [{"sku_id": str(sku.id), "quantity": 4}],
            },
            headers=SERVICE_HEADERS,
        )
    assert reserve.status_code == 200
    await db_session.refresh(sku)
    assert sku.reserved_quantity == 4

    unreserve = await client.post(
        "/api/v1/unreserve",
        json={
            "order_id": str(uuid4()),
            "items": [{"sku_id": str(sku.id), "quantity": 4}],
        },
        headers=SERVICE_HEADERS,
    )

    assert unreserve.status_code == 200
    assert unreserve.json() == {"ok": True}

    await db_session.refresh(sku)
    assert sku.reserved_quantity == 0
    # active_quantity restored to full stock
    assert sku.stock_quantity - sku.reserved_quantity == 10


@pytest.mark.asyncio
async def test_reserve_missing_service_key_returns_401(client, db_session, product):
    """Без X-Service-Key → 401."""
    sku = await make_sku(db_session, product, stock_quantity=10)

    response = await client.post(
        "/api/v1/reserve",
        json={
            "idempotency_key": str(uuid4()),
            "items": [{"sku_id": str(sku.id), "quantity": 1}],
        },
    )

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_reserve_sku_not_found_returns_404(client):
    """Несуществующий sku_id → 404."""
    with patch(_OOS_EVENT, new_callable=AsyncMock):
        response = await client.post(
            "/api/v1/reserve",
            json={
                "idempotency_key": str(uuid4()),
                "items": [{"sku_id": str(uuid4()), "quantity": 1}],
            },
            headers=SERVICE_HEADERS,
        )

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
