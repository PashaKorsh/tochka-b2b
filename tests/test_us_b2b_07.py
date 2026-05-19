"""
US-B2B-07: B2C catalog mode of GET /api/v1/products.

Canon flow b2b-flows.md#catalog-for-b2c. Covered scenarios:
- catalog_returns_moderated_in_stock_products
- catalog_excludes_hard_blocked
- catalog_missing_service_key_returns_401
- catalog_response_has_no_cost_price
- batch_ids_returns_visible_subset
plus deleted exclusion and invalid service key.
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

# Must match SERVICE_API_KEY default in router.get_product_viewer.
SERVICE_KEY = "dev-service-key"
CATALOG_HEADERS = {"X-Service-Key": SERVICE_KEY}


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
    """Create test seller (owns the catalogued products)."""
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


async def make_product(
    db_session, seller, category, *,
    status=ProductStatus.MODERATED,
    deleted=False,
    title="Catalog Product",
):
    """Persist a product."""
    product = Product(
        id=uuid4(),
        seller_id=seller.id,
        category_id=category.id,
        title=title,
        slug=f"catalog-{uuid4().hex[:8]}",
        description="Catalog description",
        status=status,
        deleted=deleted,
        blocked=(status in (ProductStatus.BLOCKED, ProductStatus.HARD_BLOCKED)),
    )
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)
    return product


async def make_sku(db_session, product, *, stock_quantity=10, reserved_quantity=0):
    """Persist a SKU. active_quantity = stock - reserved."""
    sku = SKU(
        id=uuid4(),
        product_id=product.id,
        name="Variant",
        price=12000,
        cost_price=8000,
        discount=0,
        stock_quantity=stock_quantity,
        reserved_quantity=reserved_quantity,
    )
    db_session.add(sku)
    await db_session.commit()
    await db_session.refresh(sku)
    return sku


async def make_visible_product(db_session, seller, category, *, title="Visible"):
    """A product that satisfies all B2C visibility conditions."""
    product = await make_product(
        db_session, seller, category, status=ProductStatus.MODERATED, title=title,
    )
    await make_sku(db_session, product, stock_quantity=10, reserved_quantity=0)
    return product


@pytest.mark.asyncio
async def test_catalog_returns_moderated_in_stock_products(
    client, db_session, seller, category
):
    """
    Canon: каталог отдаёт только MODERATED + deleted=false + active_quantity>0.
    """
    visible = await make_visible_product(db_session, seller, category, title="Visible")

    # Hidden: not MODERATED.
    created = await make_product(
        db_session, seller, category, status=ProductStatus.CREATED, title="Created"
    )
    await make_sku(db_session, created, stock_quantity=10)

    # Hidden: MODERATED but no SKU with active stock.
    no_stock = await make_product(db_session, seller, category, title="NoStock")
    await make_sku(db_session, no_stock, stock_quantity=0)

    response = await client.get("/api/v1/products", headers=CATALOG_HEADERS)

    assert response.status_code == 200
    data = response.json()
    ids = {item["id"] for item in data["items"]}
    assert ids == {str(visible.id)}
    assert data["total_count"] == 1


@pytest.mark.asyncio
async def test_catalog_excludes_hard_blocked(client, db_session, seller, category):
    """Canon: HARD_BLOCKED товар не попадает в каталог."""
    visible = await make_visible_product(db_session, seller, category)

    hard_blocked = await make_product(
        db_session, seller, category, status=ProductStatus.HARD_BLOCKED, title="Blocked"
    )
    await make_sku(db_session, hard_blocked, stock_quantity=10)

    response = await client.get("/api/v1/products", headers=CATALOG_HEADERS)

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert str(visible.id) in ids
    assert str(hard_blocked.id) not in ids


@pytest.mark.asyncio
async def test_catalog_excludes_deleted(client, db_session, seller, category):
    """Мягко удалённый товар не виден в каталоге."""
    visible = await make_visible_product(db_session, seller, category)

    deleted = await make_product(db_session, seller, category, deleted=True, title="Deleted")
    await make_sku(db_session, deleted, stock_quantity=10)

    response = await client.get("/api/v1/products", headers=CATALOG_HEADERS)

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(visible.id)}


@pytest.mark.asyncio
async def test_catalog_missing_service_key_returns_401(client):
    """Canon: без X-Service-Key (и без JWT продавца) → 401."""
    response = await client.get("/api/v1/products")

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_catalog_invalid_service_key_returns_401(client):
    """Неверный X-Service-Key → 401."""
    response = await client.get(
        "/api/v1/products", headers={"X-Service-Key": "wrong-key"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_catalog_response_has_no_cost_price(client, db_session, seller, category):
    """
    Canon: B2C-ответ не содержит cost_price / reserved_quantity —
    чувствительные поля продавца не утекают в каталог.
    """
    await make_visible_product(db_session, seller, category)

    response = await client.get("/api/v1/products", headers=CATALOG_HEADERS)

    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert len(item["skus"]) == 1
    sku = item["skus"][0]
    assert "cost_price" not in sku
    assert "reserved_quantity" not in sku
    # public SKU still exposes what B2C needs
    assert "price" in sku
    assert "active_quantity" in sku


@pytest.mark.asyncio
async def test_batch_ids_returns_visible_subset(client, db_session, seller, category):
    """
    Canon: ?ids= возвращает только видимые товары из списка,
    скрытые просто отсутствуют (без 404).
    """
    visible1 = await make_visible_product(db_session, seller, category, title="V1")
    visible2 = await make_visible_product(db_session, seller, category, title="V2")

    hidden = await make_product(
        db_session, seller, category, status=ProductStatus.HARD_BLOCKED, title="Hidden"
    )
    await make_sku(db_session, hidden, stock_quantity=10)

    missing_id = uuid4()
    requested = [visible1.id, visible2.id, hidden.id, missing_id]
    ids_param = ",".join(str(i) for i in requested)

    response = await client.get(
        f"/api/v1/products?ids={ids_param}", headers=CATALOG_HEADERS
    )

    assert response.status_code == 200
    data = response.json()
    returned = {item["id"] for item in data["items"]}
    assert returned == {str(visible1.id), str(visible2.id)}
    assert str(hidden.id) not in returned
    assert str(missing_id) not in returned
    assert data["total_count"] == 2
