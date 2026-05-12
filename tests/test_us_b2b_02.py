import os
import uuid as uuid_mod

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


# Test database URL (CI: localhost; docker-compose pytest: host postgres or override)
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/tochkab2b",
)

# Create test engine
test_engine = create_async_engine(
    TEST_DATABASE_URL,
    poolclass=NullPool,
    echo=False
)

TestSessionLocal = async_sessionmaker(
    test_engine,
    class_=AsyncSession,
    expire_on_commit=False
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
        base_url="http://test"
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
        company_name="Test Company"
    )
    db_session.add(seller)
    await db_session.commit()
    await db_session.refresh(seller)
    return seller


@pytest_asyncio.fixture
async def category(db_session):
    """Create test category"""
    category = Category(
        id=uuid4(),
        name="Test Category"
    )
    db_session.add(category)
    await db_session.commit()
    await db_session.refresh(category)
    return category


@pytest_asyncio.fixture
async def product(db_session, seller, category):
    """Create test product with CREATED status"""
    product = Product(
        id=uuid4(),
        seller_id=seller.id,
        category_id=category.id,
        title="Test Product",
        description="Test Description",
        status=ProductStatus.CREATED,
        deleted=False,
        blocked=False
    )
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)
    return product


@pytest_asyncio.fixture
async def product_with_sku(db_session, seller, category):
    """Create test product with one SKU (status ON_MODERATION)"""
    product = Product(
        id=uuid4(),
        seller_id=seller.id,
        category_id=category.id,
        title="Test Product with SKU",
        description="Test Description",
        status=ProductStatus.ON_MODERATION,
        deleted=False,
        blocked=False
    )
    db_session.add(product)
    await db_session.flush()

    sku = SKU(
        id=uuid4(),
        product_id=product.id,
        name="Existing SKU",
        price=10000,
        cost_price=5000,
        discount=0,
        stock_quantity=0,
        reserved_quantity=0
    )
    db_session.add(sku)
    await db_session.commit()
    await db_session.refresh(product)
    return product


@pytest_asyncio.fixture
async def hard_blocked_product(db_session, seller, category):
    """Create test product with HARD_BLOCKED status"""
    product = Product(
        id=uuid4(),
        seller_id=seller.id,
        category_id=category.id,
        title="Hard Blocked Product",
        description="Test Description",
        status=ProductStatus.HARD_BLOCKED,
        deleted=False,
        blocked=True
    )
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)
    return product


def create_access_token(seller_id: str) -> str:
    """Create JWT token for testing"""
    expire = datetime.utcnow() + timedelta(hours=1)
    to_encode = {
        "sub": seller_id,
        "exp": expire
    }
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


@pytest.mark.asyncio
async def test_first_sku_transitions_product_to_on_moderation(client, db_session, seller, product):
    """
    US-B2B-02: first_sku_transitions_product_to_on_moderation

    Canon requirement (b2b-flows.md:237-239):
    If this is the first SKU for product with status CREATED:
    - Status changes: CREATED → ON_MODERATION
    """
    token = create_access_token(str(seller.id))

    with patch('backend.modules.products.service.ProductService._send_moderation_event', new_callable=AsyncMock):
        response = await client.post(
            "/api/v1/skus",
            json={
                "product_id": str(product.id),
                "name": "256GB Black",
                "price": 12999000,
                "cost_price": 9500000,
                "discount": 0,
                "images": [{"url": "/s3/iphone15-black-256.jpg", "ordering": 0}],
                "characteristics": [
                    {"name": "Цвет", "value": "Чёрный"},
                    {"name": "Объём памяти", "value": "256 ГБ"}
                ]
            },
            headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "256GB Black"
    assert data["price"] == 12999000
    assert data["cost_price"] == 9500000

    # Verify product status changed
    await db_session.refresh(product)
    assert product.status == ProductStatus.ON_MODERATION


@pytest.mark.asyncio
async def test_first_sku_emits_created_event_to_moderation(client, seller, product):
    """
    US-B2B-02: first_sku_emits_created_event_to_moderation

    Canon requirement (b2b-flows.md:240-252):
    Event CREATED sent to Moderation with:
    - idempotency_key
    - product_id
    - seller_id
    - event: "CREATED"
    - date
    """
    token = create_access_token(str(seller.id))

    with patch('backend.modules.products.service.ProductService._send_moderation_event', new_callable=AsyncMock) as mock_send:
        response = await client.post(
            "/api/v1/skus",
            json={
                "product_id": str(product.id),
                "name": "256GB Black",
                "price": 12999000,
                "cost_price": 9500000,
                "discount": 0,
                "images": [{"url": "/s3/iphone15-black-256.jpg", "ordering": 0}],
                "characteristics": []
            },
            headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 201

    # Verify event was sent
    mock_send.assert_called_once()
    call_args = mock_send.call_args
    assert call_args.kwargs["product_id"] == product.id
    assert call_args.kwargs["seller_id"] == seller.id
    assert call_args.kwargs["event_type"] == "CREATED"
    expected_key = str(
        uuid_mod.uuid5(
            uuid_mod.NAMESPACE_URL,
            f"neomarket:b2b:product_created:{product.id}",
        )
    )
    assert call_args.kwargs["idempotency_key"] == expected_key


@pytest.mark.asyncio
async def test_second_sku_no_state_change(client, db_session, seller, product_with_sku):
    """
    US-B2B-02: second_sku_no_state_change

    Canon requirement (b2b-flows.md:255):
    If product already has SKU - SKU просто добавляется,
    статус не меняется, события не отправляются.
    """
    token = create_access_token(str(seller.id))
    original_status = product_with_sku.status

    with patch('backend.modules.products.service.ProductService._send_moderation_event', new_callable=AsyncMock) as mock_send:
        response = await client.post(
            "/api/v1/skus",
            json={
                "product_id": str(product_with_sku.id),
                "name": "512GB White",
                "price": 14999000,
                "cost_price": 11000000,
                "discount": 0,
                "images": [{"url": "/s3/iphone15-white-512.jpg", "ordering": 0}],
                "characteristics": []
            },
            headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "512GB White"

    # Verify product status did NOT change
    await db_session.refresh(product_with_sku)
    assert product_with_sku.status == original_status

    # Verify NO event was sent
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_add_sku_to_hard_blocked_returns_403(client, seller, hard_blocked_product):
    """
    US-B2B-02: add_sku_to_hard_blocked_returns_403

    Canon requirement (b2b-flows.md:229):
    Товар в статусе HARD_BLOCKED → 403 FORBIDDEN
    """
    token = create_access_token(str(seller.id))

    response = await client.post(
        "/api/v1/skus",
        json={
            "product_id": str(hard_blocked_product.id),
            "name": "256GB Black",
            "price": 12999000,
            "cost_price": 9500000,
            "discount": 0,
            "images": [{"url": "/s3/test.jpg", "ordering": 0}],
            "characteristics": []
        },
        headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403
    data = response.json()
    assert data["code"] == "FORBIDDEN"
    assert "hard-blocked" in data["message"].lower()


@pytest.mark.asyncio
async def test_cost_price_zero_returns_400(client, seller, product):
    """cost_price <= 0 → 400 INVALID_REQUEST (canon b2b-flows.md:231)."""
    token = create_access_token(str(seller.id))

    response = await client.post(
        "/api/v1/skus",
        json={
            "product_id": str(product.id),
            "name": "256GB Black",
            "price": 12999000,
            "cost_price": 0,
            "discount": 0,
            "images": [{"url": "/s3/iphone15-black-256.jpg", "ordering": 0}],
            "characteristics": [],
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    data = response.json()
    assert data["code"] == "INVALID_REQUEST"
    assert "cost_price" in data["message"].lower()


@pytest.mark.asyncio
async def test_openapi_optional_fields_omitted_returns_201(client, seller, product):
    """OpenAPI SKUCreate: stock_quantity/article/cost_price optional — запрос без них проходит."""
    token = create_access_token(str(seller.id))

    with patch(
        "backend.modules.products.service.ProductService._send_moderation_event",
        new_callable=AsyncMock,
    ):
        response = await client.post(
            "/api/v1/skus",
            json={
                "product_id": str(product.id),
                "name": "Basic variant",
                "price": 1000,
                "images": [{"url": "/s3/x.jpg", "ordering": 0}],
                "characteristics": [],
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Basic variant"
    assert data["stock_quantity"] == 0
    assert data["article"] is None
    assert data["cost_price"] is None


@pytest.mark.asyncio
async def test_missing_image_returns_400(client, seller, product):
    """
    US-B2B-02: missing_image_returns_400

    Canon requirement (b2b-flows.md:233):
    Нет image → 400 INVALID_REQUEST
    """
    token = create_access_token(str(seller.id))

    response = await client.post(
        "/api/v1/skus",
        json={
            "product_id": str(product.id),
            "name": "256GB Black",
            "price": 12999000,
            "cost_price": 9500000,
            "discount": 0,
            "images": [],  # Empty images array
            "characteristics": []
        },
        headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 400
    data = response.json()
    assert data["code"] == "INVALID_REQUEST"
    assert "image" in data["message"].lower()


@pytest.mark.asyncio
async def test_product_not_found_returns_404(client, seller):
    """
    US-B2B-02: product_not_found_returns_404

    Canon requirement (b2b-flows.md:228):
    product_id не существует → 404 NOT_FOUND
    """
    token = create_access_token(str(seller.id))
    non_existent_id = uuid4()

    response = await client.post(
        "/api/v1/skus",
        json={
            "product_id": str(non_existent_id),
            "name": "256GB Black",
            "price": 12999000,
            "cost_price": 9500000,
            "discount": 0,
            "images": [{"url": "/s3/test.jpg", "ordering": 0}],
            "characteristics": []
        },
        headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 404
    data = response.json()
    assert data["code"] == "NOT_FOUND"
    assert "Product not found" in data["message"]


@pytest.mark.asyncio
async def test_not_owner_returns_403(client, db_session, seller, category):
    """
    US-B2B-02: IDOR prevention - not_owner_returns_403

    Canon requirement (b2b-flows.md:342-343):
    Ownership check через seller_id из JWT.
    Если product.seller_id != jwt.seller_id → 403 NOT_OWNER
    """
    # Create another seller
    other_seller = Seller(
        id=uuid4(),
        email="other@test.com",
        hashed_password="hashed",
        first_name="Other",
        last_name="Seller",
        company_name="Other Company"
    )
    db_session.add(other_seller)
    await db_session.flush()

    # Create product owned by other_seller
    other_product = Product(
        id=uuid4(),
        seller_id=other_seller.id,
        category_id=category.id,
        title="Other Product",
        description="Test",
        status=ProductStatus.CREATED,
        deleted=False,
        blocked=False
    )
    db_session.add(other_product)
    await db_session.commit()

    # Try to add SKU using first seller's token
    token = create_access_token(str(seller.id))

    response = await client.post(
        "/api/v1/skus",
        json={
            "product_id": str(other_product.id),
            "name": "256GB Black",
            "price": 12999000,
            "cost_price": 9500000,
            "discount": 0,
            "images": [{"url": "/s3/test.jpg", "ordering": 0}],
            "characteristics": []
        },
        headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403
    data = response.json()
    assert data["code"] == "NOT_OWNER"


@pytest.mark.asyncio
async def test_sku_response_contains_all_required_fields(client, seller, product):
    """
    US-B2B-02: Verify response contains all required fields

    Canon requirement (b2b-flows.md:200-222):
    Response должен содержать все поля SKU
    """
    token = create_access_token(str(seller.id))

    with patch('backend.modules.products.service.ProductService._send_moderation_event', new_callable=AsyncMock):
        response = await client.post(
            "/api/v1/skus",
            json={
                "product_id": str(product.id),
                "name": "256GB Black",
                "price": 12999000,
                "cost_price": 9500000,
                "discount": 500000,
                "images": [
                    {"url": "/s3/iphone15-black-256.jpg", "ordering": 0}
                ],
                "characteristics": [
                    {"name": "Цвет", "value": "Чёрный"}
                ]
            },
            headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 201
    data = response.json()

    # Verify all required fields present
    assert "id" in data
    assert data["product_id"] == str(product.id)
    assert data["name"] == "256GB Black"
    assert data["price"] == 12999000
    assert data["cost_price"] == 9500000
    assert data["discount"] == 500000
    assert data["stock_quantity"] == 0
    assert data["reserved_quantity"] == 0
    assert len(data["images"]) == 1
    assert data["images"][0]["url"] == "/s3/iphone15-black-256.jpg"
    assert len(data["characteristics"]) == 1
    assert data["characteristics"][0]["name"] == "Цвет"
    assert "created_at" in data
    assert "updated_at" in data
