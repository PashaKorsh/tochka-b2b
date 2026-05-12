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
from backend.modules.products.models import Product, ProductStatus
from backend.core.auth import SECRET_KEY, ALGORITHM


# Test database URL
TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/tochkab2b_test"

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
async def test_seller(db_session: AsyncSession) -> Seller:
    """Create test seller"""
    seller = Seller(
        id=uuid4(),
        email="test@example.com",
        hashed_password="hashed_password",
        first_name="Test",
        last_name="Seller",
        company_name="Test Company"
    )
    db_session.add(seller)
    await db_session.commit()
    await db_session.refresh(seller)
    return seller


@pytest_asyncio.fixture
async def test_category(db_session: AsyncSession) -> Category:
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
def auth_token(test_seller: Seller) -> str:
    """Generate JWT token for test seller"""
    payload = {
        "sub": str(test_seller.id),
        "exp": datetime.utcnow() + timedelta(hours=1)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


@pytest.mark.asyncio
async def test_create_product_returns_201_with_created_status(
    client: AsyncClient,
    db_session: AsyncSession,
    test_seller: Seller,
    test_category: Category,
    auth_token: str
):
    """
    Canon test: create_product_returns_201_with_created_status

    Проверяет:
    - Товар создается с status=CREATED
    - skus=[] изначально
    - deleted=False, blocked=False
    """
    response = await client.post(
        "/api/v1/products",
        json={
            "category_id": str(test_category.id),
            "title": "iPhone 15 Pro Max",
            "description": "Флагманский смартфон Apple 2024 года",
            "images": [
                {"url": "/s3/iphone15-front.jpg", "ordering": 0}
            ],
            "characteristics": [
                {"name": "Бренд", "value": "Apple"}
            ]
        },
        headers={"Authorization": f"Bearer {auth_token}"}
    )

    assert response.status_code == 201
    data = response.json()

    # Проверяем обязательные поля из канона
    assert data["status"] == "CREATED"
    assert data["skus"] == []
    assert data["deleted"] is False
    assert data["blocked"] is False
    assert data["title"] == "iPhone 15 Pro Max"
    assert data["seller_id"] == str(test_seller.id)
    assert data["category_id"] == str(test_category.id)
    assert len(data["images"]) == 1
    assert len(data["characteristics"]) == 1


@pytest.mark.asyncio
async def test_seller_id_taken_from_jwt(
    client: AsyncClient,
    db_session: AsyncSession,
    test_seller: Seller,
    test_category: Category,
    auth_token: str
):
    """
    Canon test: seller_id_taken_from_jwt

    Проверяет:
    - seller_id берется только из JWT claims
    - seller_id из body игнорируется (IDOR prevention)
    """
    fake_seller_id = str(uuid4())

    response = await client.post(
        "/api/v1/products",
        json={
            "category_id": str(test_category.id),
            "title": "Test Product",
            "seller_id": fake_seller_id  # Попытка подменить seller_id
        },
        headers={"Authorization": f"Bearer {auth_token}"}
    )

    assert response.status_code == 201
    data = response.json()

    # seller_id должен быть из JWT, не из body
    assert data["seller_id"] == str(test_seller.id)
    assert data["seller_id"] != fake_seller_id


@pytest.mark.asyncio
async def test_missing_category_returns_400(
    client: AsyncClient,
    test_seller: Seller,
    auth_token: str
):
    """
    Canon test: missing_category_returns_400

    Проверяет:
    - Запрос без category_id возвращает 422 (Pydantic validation)
    """
    response = await client.post(
        "/api/v1/products",
        json={
            "title": "Test Product",
            "description": "Test"
        },
        headers={"Authorization": f"Bearer {auth_token}"}
    )

    # Pydantic validation error для missing required field
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_invalid_category_id_returns_400(
    client: AsyncClient,
    test_seller: Seller,
    auth_token: str
):
    """
    Canon test: invalid_category_id_returns_400

    Проверяет:
    - Несуществующий category_id возвращает 400 с кодом INVALID_REQUEST
    """
    non_existent_category_id = str(uuid4())

    response = await client.post(
        "/api/v1/products",
        json={
            "category_id": non_existent_category_id,
            "title": "Test Product"
        },
        headers={"Authorization": f"Bearer {auth_token}"}
    )

    assert response.status_code == 400
    data = response.json()
    assert data["code"] == "INVALID_REQUEST"
    assert "Category not found" in data["message"]


@pytest.mark.asyncio
async def test_product_without_images_is_allowed(
    client: AsyncClient,
    db_session: AsyncSession,
    test_seller: Seller,
    test_category: Category,
    auth_token: str
):
    """
    Проверяет, что товар можно создать без изображений.

    Важно: В openapi.yaml images имеет default=[] (не required).
    Другие команды ошибочно делали min_length=1.
    """
    response = await client.post(
        "/api/v1/products",
        json={
            "category_id": str(test_category.id),
            "title": "Product Without Images"
        },
        headers={"Authorization": f"Bearer {auth_token}"}
    )

    assert response.status_code == 201
    data = response.json()
    assert data["images"] == []


@pytest.mark.asyncio
async def test_product_with_optional_description(
    client: AsyncClient,
    db_session: AsyncSession,
    test_seller: Seller,
    test_category: Category,
    auth_token: str
):
    """
    Проверяет, что description опциональное.

    В openapi.yaml description: anyOf [string, null] (не required).
    """
    response = await client.post(
        "/api/v1/products",
        json={
            "category_id": str(test_category.id),
            "title": "Product Without Description"
        },
        headers={"Authorization": f"Bearer {auth_token}"}
    )

    assert response.status_code == 201
    data = response.json()
    assert data["description"] is None


@pytest.mark.asyncio
async def test_unauthorized_request_returns_401(
    client: AsyncClient,
    test_category: Category
):
    """
    Проверяет, что запрос без токена возвращает 401.
    """
    response = await client.post(
        "/api/v1/products",
        json={
            "category_id": str(test_category.id),
            "title": "Test Product"
        }
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_response_contains_all_required_fields(
    client: AsyncClient,
    db_session: AsyncSession,
    test_seller: Seller,
    test_category: Category,
    auth_token: str
):
    """
    Проверяет, что response содержит все обязательные поля из openapi.yaml ProductResponse.

    Обязательные поля (openapi.yaml:1788-1800):
    - id, seller_id, category_id, title, description, status
    - images, characteristics, skus
    - created_at, updated_at

    Дополнительно из канона (b2b-flows.md:89-91):
    - deleted, blocked
    """
    response = await client.post(
        "/api/v1/products",
        json={
            "category_id": str(test_category.id),
            "title": "Complete Product",
            "description": "Full description",
            "images": [{"url": "/test.jpg", "ordering": 0}],
            "characteristics": [{"name": "Brand", "value": "Test"}]
        },
        headers={"Authorization": f"Bearer {auth_token}"}
    )

    assert response.status_code == 201
    data = response.json()

    # OpenAPI required fields
    assert "id" in data
    assert "seller_id" in data
    assert "category_id" in data
    assert "title" in data
    assert "description" in data
    assert "status" in data
    assert "images" in data
    assert "characteristics" in data
    assert "skus" in data
    assert "created_at" in data
    assert "updated_at" in data

    # Canon required fields (missing in openapi but required by business logic)
    assert "deleted" in data
    assert "blocked" in data
