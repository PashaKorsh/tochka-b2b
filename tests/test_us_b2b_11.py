"""
US-B2B-11: Seller-cabinet GET /api/v1/products.

Canon flow b2b-flows.md#list-products. Covered scenarios:
- list_returns_only_own_products
- idor_query_param_seller_id_ignored
- deleted_products_visible_with_deleted_flag
- status_filter_works_correctly
- search_by_title_case_insensitive
plus skus_count / total_active_quantity aggregates.
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
async def other_seller(db_session):
    seller = Seller(
        id=uuid4(),
        email="other@test.com",
        hashed_password="hashed",
        first_name="Other",
        last_name="Seller",
        company_name="Other Company",
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


async def make_product(
    db_session, seller, category, *,
    title="Listed Product",
    status=ProductStatus.MODERATED,
    deleted=False,
):
    product = Product(
        id=uuid4(),
        seller_id=seller.id,
        category_id=category.id,
        title=title,
        slug=f"listed-{uuid4().hex[:8]}",
        description="Description",
        status=status,
        deleted=deleted,
        blocked=(status in (ProductStatus.BLOCKED, ProductStatus.HARD_BLOCKED)),
    )
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)
    return product


async def make_sku(db_session, product, *, stock_quantity=10, reserved_quantity=0):
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
async def test_list_returns_only_own_products(
    client, db_session, seller, other_seller, category
):
    """Canon: список возвращает только товары seller_id из JWT."""
    mine_a = await make_product(db_session, seller, category, title="Mine A")
    mine_b = await make_product(db_session, seller, category, title="Mine B")
    foreign = await make_product(db_session, other_seller, category, title="Foreign")

    token = create_access_token(str(seller.id))
    response = await client.get(
        "/api/v1/products", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(mine_a.id), str(mine_b.id)}
    assert str(foreign.id) not in ids


@pytest.mark.asyncio
async def test_idor_query_param_seller_id_ignored(
    client, db_session, seller, other_seller, category
):
    """
    Canon (IDOR): ?seller_id= в query НЕ меняет выборку — seller_id берётся из JWT.
    """
    mine = await make_product(db_session, seller, category, title="Mine")
    foreign = await make_product(db_session, other_seller, category, title="Foreign")

    token = create_access_token(str(seller.id))
    # Attempt to inject another seller's id via query — must be ignored.
    response = await client.get(
        f"/api/v1/products?seller_id={other_seller.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(mine.id)}
    assert str(foreign.id) not in ids


@pytest.mark.asyncio
async def test_deleted_products_visible_with_deleted_flag(
    client, db_session, seller, category
):
    """Canon: include_deleted=true → видны удалённые товары с deleted=true."""
    kept = await make_product(db_session, seller, category, title="Kept")
    removed = await make_product(db_session, seller, category, title="Removed", deleted=True)

    token = create_access_token(str(seller.id))
    headers = {"Authorization": f"Bearer {token}"}

    default = await client.get("/api/v1/products", headers=headers)
    default_ids = {item["id"] for item in default.json()["items"]}
    assert default_ids == {str(kept.id)}

    with_deleted = await client.get(
        "/api/v1/products?include_deleted=true", headers=headers
    )
    items = with_deleted.json()["items"]
    by_id = {item["id"]: item for item in items}
    assert by_id[str(removed.id)]["deleted"] is True
    assert by_id[str(kept.id)]["deleted"] is False


@pytest.mark.asyncio
async def test_status_filter_works_correctly(client, db_session, seller, category):
    """Canon: ?status=BLOCKED возвращает только товары со статусом BLOCKED."""
    blocked = await make_product(
        db_session, seller, category, title="Blocked", status=ProductStatus.BLOCKED
    )
    moderated = await make_product(
        db_session, seller, category, title="Moderated", status=ProductStatus.MODERATED
    )

    token = create_access_token(str(seller.id))
    response = await client.get(
        "/api/v1/products?status=BLOCKED",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(blocked.id)}
    assert str(moderated.id) not in ids


@pytest.mark.asyncio
async def test_search_by_title_case_insensitive(client, db_session, seller, category):
    """Canon: ?search= по title нечувствителен к регистру."""
    iphone = await make_product(db_session, seller, category, title="iPhone 15 Pro")
    samsung = await make_product(db_session, seller, category, title="Samsung Galaxy")

    token = create_access_token(str(seller.id))
    response = await client.get(
        "/api/v1/products?search=iphone",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {str(iphone.id)}
    assert str(samsung.id) not in ids


@pytest.mark.asyncio
async def test_short_response_includes_skus_aggregates(
    client, db_session, seller, category
):
    """
    Canon: каждая карточка списка содержит skus_count и total_active_quantity.
    total_active_quantity = Σ (stock_quantity − reserved_quantity) по SKU.
    """
    product = await make_product(db_session, seller, category)
    await make_sku(db_session, product, stock_quantity=10, reserved_quantity=3)  # active 7
    await make_sku(db_session, product, stock_quantity=5, reserved_quantity=0)   # active 5

    token = create_access_token(str(seller.id))
    response = await client.get(
        "/api/v1/products", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    item = next(i for i in response.json()["items"] if i["id"] == str(product.id))
    assert item["skus_count"] == 2
    assert item["total_active_quantity"] == 12
