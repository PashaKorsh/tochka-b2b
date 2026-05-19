import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.modules.products.schemas import ErrorResponse
from backend.modules.moderation.schemas import ModerationEvent
from backend.modules.moderation.service import ModerationService


router = APIRouter(prefix="/api/v1", tags=["Moderation Events"])


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
    "/events/moderation",
    response_model=None,
    responses={
        200: {"description": "Moderation decision applied (or duplicate ignored)"},
        400: {"model": ErrorResponse, "description": "Invalid event"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "Product not found"},
    },
    summary="Применить решение модерации (US-B2B-09)",
    description="""
    Приём события от сервиса Moderation (X-Service-Key).

    Бизнес-логика (canon b2b-flows.md#apply-moderation), три пути:
    - status=MODERATED → товар MODERATED, blocked=false, blocking_reason и
      field_reports очищены;
    - status=BLOCKED, hard_block=false → BLOCKED, сохраняем blocking_reason и
      field_reports, каскад PRODUCT_BLOCKED в B2C;
    - status=BLOCKED, hard_block=true → HARD_BLOCKED (терминальный статус),
      сохраняем blocking_reason, каскад PRODUCT_BLOCKED в B2C.

    Идемпотентность по idempotency_key — повторное событие → 200 без изменений.
    """,
)
async def apply_moderation_event(
    event: ModerationEvent,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_service_key),
):
    """
    POST /api/v1/events/moderation - Apply a Moderation decision (US-B2B-09).

    Canon test scenarios:
    - moderated_event_clears_blocking_data
    - blocked_soft_saves_field_reports
    - blocked_hard_sets_terminal_status
    - duplicate_event_same_idempotency_key_no_side_effects
    """
    try:
        applied = await ModerationService.apply_event(db=db, event=event)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"ok": True, "applied": applied},
        )
    except ValueError as e:
        error_msg = str(e)
        if "Product not found" in error_msg:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"code": "NOT_FOUND", "message": "Product not found"},
            )
        if "invalid status" in error_msg:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "code": "INVALID_REQUEST",
                    "message": "status must be MODERATED or BLOCKED",
                },
            )
        raise
