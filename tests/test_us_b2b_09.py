"""
US-B2B-09: Apply Moderation decision.

Canon flow b2b-flows.md#apply-moderation. Covered scenarios:
- moderated_event_clears_blocking_data
- blocked_soft_saves_field_reports
- blocked_hard_sets_terminal_status
- hard_blocked_product_rejects_seller_edits
- duplicate_event_same_idempotency_key_no_side_effects
plus missing service key (401).
"""
import os

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
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
from backend.modules.products.models import Product, ProductStatus, FieldReport
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

SERVICE_HEADERS = {"X-Service-Key": "dev-service-key"}
_CASCADE = "backend.modules.products.service.ProductService._send_b2c_event"


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


async def make_product(
    db_session, seller, category, *,
    status=ProductStatus.ON_MODERATION,
    blocking_reason_id=None,
    moderator_comment=None,
):
    """Persist a product."""
    product = Product(
        id=uuid4(),
        seller_id=seller.id,
        category_id=category.id,
        title="Moderated Product",
        slug=f"mod-{uuid4().hex[:8]}",
        description="Description",
        status=status,
        deleted=False,
        blocked=(status in (ProductStatus.BLOCKED, ProductStatus.HARD_BLOCKED)),
        blocking_reason_id=blocking_reason_id,
        moderator_comment=moderator_comment,
    )
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)
    return product


async def make_field_report(db_session, product, field_name, comment):
    report = FieldReport(
        id=uuid4(),
        product_id=product.id,
        field_name=field_name,
        comment=comment,
    )
    db_session.add(report)
    await db_session.commit()
    return report


async def count_field_reports(db_session, product_id):
    result = await db_session.execute(
        select(FieldReport).where(FieldReport.product_id == product_id)
    )
    return len(result.scalars().all())


@pytest.mark.asyncio
async def test_moderated_event_clears_blocking_data(client, db_session, seller, category):
    """Canon: status=MODERATED → товар MODERATED, blocking_reason и field_reports очищены."""
    product = await make_product(
        db_session, seller, category,
        status=ProductStatus.BLOCKED,
        blocking_reason_id=uuid4(),
        moderator_comment="Old block reason",
    )
    await make_field_report(db_session, product, "title", "stale remark")

    response = await client.post(
        "/api/v1/events/moderation",
        json={
            "idempotency_key": str(uuid4()),
            "product_id": str(product.id),
            "status": "MODERATED",
        },
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 200

    await db_session.refresh(product)
    assert product.status == ProductStatus.MODERATED
    assert product.blocked is False
    assert product.blocking_reason_id is None
    assert product.moderator_comment is None
    assert await count_field_reports(db_session, product.id) == 0


@pytest.mark.asyncio
async def test_blocked_soft_saves_field_reports(client, db_session, seller, category):
    """Canon: BLOCKED + hard_block=false → BLOCKED, field_reports сохранены, каскад в B2C."""
    product = await make_product(db_session, seller, category)
    reason_id = uuid4()

    with patch(_CASCADE, new_callable=AsyncMock) as mock_cascade:
        response = await client.post(
            "/api/v1/events/moderation",
            json={
                "idempotency_key": str(uuid4()),
                "product_id": str(product.id),
                "status": "BLOCKED",
                "hard_block": False,
                "blocking_reason": {
                    "id": str(reason_id),
                    "title": "Запрещённый контент",
                    "comment": "Замените описание и фото.",
                },
                "field_reports": [
                    {"field_name": "title", "comment": "Запрещённое слово."},
                    {"field_name": "description", "comment": "Недостоверно."},
                ],
            },
            headers=SERVICE_HEADERS,
        )

    assert response.status_code == 200

    await db_session.refresh(product)
    assert product.status == ProductStatus.BLOCKED
    assert product.blocked is True
    assert product.blocking_reason_id == reason_id
    assert product.moderator_comment == "Замените описание и фото."
    assert await count_field_reports(db_session, product.id) == 2

    mock_cascade.assert_awaited_once()
    assert mock_cascade.call_args.kwargs["event_type"] == "PRODUCT_BLOCKED"
    assert mock_cascade.call_args.kwargs["product_id"] == product.id


@pytest.mark.asyncio
async def test_blocked_hard_sets_terminal_status(client, db_session, seller, category):
    """Canon: BLOCKED + hard_block=true → HARD_BLOCKED, каскад в B2C, field_reports не хранятся."""
    product = await make_product(db_session, seller, category)

    with patch(_CASCADE, new_callable=AsyncMock) as mock_cascade:
        response = await client.post(
            "/api/v1/events/moderation",
            json={
                "idempotency_key": str(uuid4()),
                "product_id": str(product.id),
                "status": "BLOCKED",
                "hard_block": True,
                "blocking_reason": {
                    "id": str(uuid4()),
                    "title": "Грубое нарушение",
                    "comment": "Товар снят навсегда.",
                },
                "field_reports": [
                    {"field_name": "title", "comment": "ignored on hard block"},
                ],
            },
            headers=SERVICE_HEADERS,
        )

    assert response.status_code == 200

    await db_session.refresh(product)
    assert product.status == ProductStatus.HARD_BLOCKED
    assert product.blocked is True
    # hard block persists blocking_reason only — no field reports
    assert await count_field_reports(db_session, product.id) == 0

    mock_cascade.assert_awaited_once()
    assert mock_cascade.call_args.kwargs["event_type"] == "PRODUCT_BLOCKED"


@pytest.mark.asyncio
async def test_hard_blocked_product_rejects_seller_edits(client, db_session, seller, category):
    """Canon: HARD_BLOCKED — терминальный статус, PATCH и DELETE продавца → 403."""
    product = await make_product(
        db_session, seller, category, status=ProductStatus.HARD_BLOCKED
    )
    token = create_access_token(str(seller.id))
    headers = {"Authorization": f"Bearer {token}"}

    patch_resp = await client.patch(
        f"/api/v1/products/{product.id}",
        json={"title": "Trying to edit"},
        headers=headers,
    )
    assert patch_resp.status_code == 403
    assert patch_resp.json()["code"] == "FORBIDDEN"

    delete_resp = await client.delete(f"/api/v1/products/{product.id}", headers=headers)
    assert delete_resp.status_code == 403
    assert delete_resp.json()["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_duplicate_event_same_idempotency_key_no_side_effects(
    client, db_session, seller, category
):
    """Canon: повторное событие с тем же idempotency_key → 200 без изменений."""
    product = await make_product(db_session, seller, category)
    key = str(uuid4())
    blocked_event = {
        "idempotency_key": key,
        "product_id": str(product.id),
        "status": "BLOCKED",
        "hard_block": False,
        "blocking_reason": {"id": str(uuid4()), "title": "Reason", "comment": "fix it"},
        "field_reports": [{"field_name": "title", "comment": "bad"}],
    }

    with patch(_CASCADE, new_callable=AsyncMock):
        first = await client.post(
            "/api/v1/events/moderation", json=blocked_event, headers=SERVICE_HEADERS
        )
        assert first.status_code == 200

        await db_session.refresh(product)
        assert product.status == ProductStatus.BLOCKED

        # Replay with the SAME idempotency_key but a different decision —
        # must be ignored as a duplicate.
        replay = dict(blocked_event, status="MODERATED")
        second = await client.post(
            "/api/v1/events/moderation", json=replay, headers=SERVICE_HEADERS
        )

    assert second.status_code == 200
    await db_session.refresh(product)
    # Still BLOCKED — the duplicate event had no side effects.
    assert product.status == ProductStatus.BLOCKED
    assert await count_field_reports(db_session, product.id) == 1


@pytest.mark.asyncio
async def test_missing_service_key_returns_401(client, db_session, seller, category):
    """Без X-Service-Key → 401."""
    product = await make_product(db_session, seller, category)

    response = await client.post(
        "/api/v1/events/moderation",
        json={
            "idempotency_key": str(uuid4()),
            "product_id": str(product.id),
            "status": "MODERATED",
        },
    )

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
