import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.modules.products.schemas import ErrorResponse
from backend.modules.moderation.schemas import ModerationEvent
from backend.modules.moderation.service import ModerationService


router = APIRouter(prefix="/api/v1/moderation", tags=["Moderation Events"])


def require_service_key(
    x_service_key: Optional[str] = Header(None, alias="X-Service-Key"),
) -> None:
    """
    The moderation-events endpoint is cross-service only — вызывается сервисом
    Moderation по X-Service-Key. Без валидного ключа → 401.
    """
    expected = os.getenv("SERVICE_API_KEY", "dev-service-key")
    if not x_service_key or x_service_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "Authorization required"},
        )


@router.post(
    "/events",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    responses={
        204: {"description": "Moderation decision applied (or duplicate ignored)"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "Product not found"},
        422: {"description": "Validation Error"},
    },
    summary="Применить решение модерации (US-B2B-09)",
    description="""
    Приём события от сервиса Moderation (X-Service-Key).

    Контракт (spec b2b/neomarket-b2b.yaml#ModerationEventRequest):
    - path: POST /api/v1/moderation/events;
    - тело: idempotency_key (uuid), product_id (uuid), event_type
      (MODERATED|BLOCKED), occurred_at (date-time), опционально
      moderator_id / moderator_comment / blocking_reason_id (uuid) /
      hard_block / field_reports;
    - успех: 204 No Content (тело не возвращается).

    Бизнес-логика (canon b2b-flows.md#apply-moderation), три пути:
    - event_type=MODERATED → товар MODERATED, blocked=false,
      blocking_reason_id/moderator_comment и field_reports очищены;
    - event_type=BLOCKED, hard_block=false → BLOCKED, blocking_reason_id и
      moderator_comment сохранены, field_reports сохранены,
      каскад PRODUCT_BLOCKED в B2C;
    - event_type=BLOCKED, hard_block=true → HARD_BLOCKED (терминальный),
      blocking_reason_id и moderator_comment сохранены, каскад в B2C.

    Идемпотентность по idempotency_key — повторное событие → 204 без изменений,
    каскад в B2C на повторе НЕ вызывается.
    """,
)
async def apply_moderation_event(
    event: ModerationEvent,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_service_key),
):
    """
    POST /api/v1/moderation/events - Apply a Moderation decision (US-B2B-09).

    Canon test scenarios:
    - moderated_event_clears_blocking_data
    - blocked_soft_saves_field_reports
    - blocked_hard_sets_terminal_status
    - duplicate_event_same_idempotency_key_no_side_effects
    """
    try:
        await ModerationService.apply_event(db=db, event=event)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except ValueError as e:
        if "Product not found" in str(e):
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"code": "NOT_FOUND", "message": "Product not found"},
            )
        raise
