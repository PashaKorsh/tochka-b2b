"""
US-B2B-06: Create Invoice (накладная на поставку остатков).

Canon flow b2b-flows.md#create-invoice. Covered scenarios:
- create_invoice_with_moderated_sku_returns_201
- empty_items_returns_400
- non_moderated_sku_returns_400
- others_sku_returns_403
plus SKU-not-found (404), quantity<=0 (400), unauthorized (401).
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
from backend.modules.products.models import Product, ProductStatus, SKU
from backend.modules.invoices.models import Invoice
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


async def make_product(db_session, seller, category, status=ProductStatus.MODERATED):
    """Persist a product with the given status."""
    product = Product(
        id=uuid4(),
        seller_id=seller.id,
        category_id=category.id,
        title="Invoiced Product",
        slug=f"invoiced-{uuid4().hex[:8]}",
        description="Description",
        status=status,
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
        stock_quantity=0,
        reserved_quantity=0,
    )
    db_session.add(sku)
    await db_session.commit()
    await db_session.refresh(sku)
    return sku


@pytest.mark.asyncio
async def test_create_invoice_with_moderated_sku_returns_201(
    client, db_session, seller, category
):
    """
    Canon happy path: накладная на SKU MODERATED-товара — 201, статус PENDING.
    """
    product = await make_product(db_session, seller, category, ProductStatus.MODERATED)
    sku = await make_sku(db_session, product)
    token = create_access_token(str(seller.id))

    response = await client.post(
        "/api/v1/invoices",
        json={"items": [{"sku_id": str(sku.id), "quantity": 5}]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "PENDING"
    assert data["seller_id"] == str(seller.id)
    assert len(data["items"]) == 1
    assert data["items"][0]["sku_id"] == str(sku.id)
    assert data["items"][0]["quantity"] == 5
    # accepted_quantity появляется только при приёмке
    assert data["items"][0]["accepted_quantity"] is None


@pytest.mark.asyncio
async def test_empty_items_returns_400(client, seller):
    """Canon: пустой список items → 400."""
    token = create_access_token(str(seller.id))

    response = await client.post(
        "/api/v1/invoices",
        json={"items": []},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    data = response.json()
    assert data["code"] == "INVALID_REQUEST"
    assert "item" in data["message"].lower()


@pytest.mark.asyncio
async def test_non_moderated_sku_returns_400(client, db_session, seller, category):
    """Canon: SKU товара не в статусе MODERATED → 400."""
    product = await make_product(db_session, seller, category, ProductStatus.CREATED)
    sku = await make_sku(db_session, product)
    token = create_access_token(str(seller.id))

    response = await client.post(
        "/api/v1/invoices",
        json={"items": [{"sku_id": str(sku.id), "quantity": 3}]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    data = response.json()
    assert data["code"] == "INVALID_REQUEST"
    assert "MODERATED" in data["message"]


@pytest.mark.asyncio
async def test_others_sku_returns_403(client, db_session, seller, category):
    """Canon (IDOR): SKU чужого продавца → 403."""
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
    other_sku = await make_sku(db_session, other_product)
    token = create_access_token(str(seller.id))

    response = await client.post(
        "/api/v1/invoices",
        json={"items": [{"sku_id": str(other_sku.id), "quantity": 2}]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "NOT_OWNER"


@pytest.mark.asyncio
async def test_sku_not_found_returns_404(client, seller):
    """Несуществующий sku_id → 404."""
    token = create_access_token(str(seller.id))

    response = await client.post(
        "/api/v1/invoices",
        json={"items": [{"sku_id": str(uuid4()), "quantity": 1}]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_quantity_zero_returns_400(client, db_session, seller, category):
    """quantity <= 0 → 400."""
    product = await make_product(db_session, seller, category, ProductStatus.MODERATED)
    sku = await make_sku(db_session, product)
    token = create_access_token(str(seller.id))

    response = await client.post(
        "/api/v1/invoices",
        json={"items": [{"sku_id": str(sku.id), "quantity": 0}]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    data = response.json()
    assert data["code"] == "INVALID_REQUEST"
    assert "quantity" in data["message"].lower()


@pytest.mark.asyncio
async def test_create_invoice_unauthorized_returns_401(client, db_session, seller, category):
    """Запрос без токена → 401."""
    product = await make_product(db_session, seller, category, ProductStatus.MODERATED)
    sku = await make_sku(db_session, product)

    response = await client.post(
        "/api/v1/invoices",
        json={"items": [{"sku_id": str(sku.id), "quantity": 1}]},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_invoice_validation_is_all_or_nothing(client, db_session, seller, category):
    """
    Canon: валидация ДО создания. Если одна позиция невалидна — накладная
    не создаётся вовсе (ни одной строки в БД).
    """
    good_product = await make_product(db_session, seller, category, ProductStatus.MODERATED)
    good_sku = await make_sku(db_session, good_product)
    bad_product = await make_product(db_session, seller, category, ProductStatus.CREATED)
    bad_sku = await make_sku(db_session, bad_product)
    token = create_access_token(str(seller.id))

    response = await client.post(
        "/api/v1/invoices",
        json={
            "items": [
                {"sku_id": str(good_sku.id), "quantity": 1},
                {"sku_id": str(bad_sku.id), "quantity": 1},
            ]
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    result = await db_session.execute(Invoice.__table__.select())
    assert result.first() is None
