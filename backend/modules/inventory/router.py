import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.modules.products.schemas import ErrorResponse
from backend.modules.inventory.schemas import (
    ReserveRequest, UnreserveRequest, ReserveResponse, ReserveConflictResponse,
    UnreserveResponse,
)
from backend.modules.inventory.service import InventoryService, ReserveConflict


router = APIRouter(prefix="/api/v1", tags=["Inventory"])


def require_service_key(
    x_service_key: Optional[str] = Header(None, alias="X-Service-Key"),
) -> None:
    """
    Inventory endpoints are cross-service only — вызываются B2C по X-Service-Key.
    Без валидного ключа → 401 (canon b2b-flows.md#reserve-sku).
    """
    expected = os.getenv("SERVICE_API_KEY", "dev-service-key")
    if not x_service_key or x_service_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "Authorization required"},
        )


def _value_error_response(error_msg: str) -> Optional[JSONResponse]:
    """Map an InventoryService ValueError to a unified {code, message} response."""
    if "SKU not found" in error_msg:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"code": "NOT_FOUND", "message": "SKU not found"},
        )
    if "quantity must be" in error_msg:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"code": "INVALID_REQUEST", "message": "quantity must be > 0"},
        )
    return None


@router.post(
    "/reserve",
    response_model=None,
    responses={
        200: {"model": ReserveResponse, "description": "All SKUs reserved"},
        400: {"model": ErrorResponse, "description": "Invalid quantity"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "SKU not found"},
        409: {"model": ReserveConflictResponse, "description": "Insufficient stock — all rolled back"},
    },
    summary="Зарезервировать SKU (US-B2B-08)",
    description="""
    All-or-nothing резервирование SKU. Вызывается B2C при checkout (X-Service-Key).

    Бизнес-логика (canon b2b-flows.md#reserve-sku):
    - SKU блокируются SELECT FOR UPDATE в детерминированном порядке id;
    - если хотя бы одному SKU не хватает active_quantity → 409, ни один не изменён;
    - на успехе reserved_quantity += qty;
    - идемпотентность по idempotency_key из тела (повтор → тот же 200 без списания);
    - если active_quantity SKU стал 0 → событие SKU_OUT_OF_STOCK в B2C.
    """,
)
async def reserve(
    request: ReserveRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_service_key),
):
    """
    POST /api/v1/reserve - Reserve SKUs (US-B2B-08).

    Canon test scenarios:
    - reserve_all_skus_succeeds
    - partial_insufficient_stock_returns_409_all_rollback
    - idempotent_reserve_returns_200_without_double_deduction
    - sku_out_of_stock_event_emitted
    """
    try:
        result = await InventoryService.reserve(
            db=db,
            idempotency_key=request.idempotency_key,
            items=request.items,
        )
        return JSONResponse(status_code=status.HTTP_200_OK, content=result)
    except ReserveConflict as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"reserved": False, "failed_items": e.failed_items},
        )
    except ValueError as e:
        response = _value_error_response(str(e))
        if response is not None:
            return response
        raise


@router.post(
    "/unreserve",
    response_model=None,
    responses={
        200: {"model": UnreserveResponse, "description": "Reservation released"},
        400: {"model": ErrorResponse, "description": "Invalid quantity"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "SKU not found"},
    },
    summary="Снять резерв SKU (US-B2B-08)",
    description="""
    Компенсирующая операция к reserve — освобождает зарезервированное количество.
    Вызывается B2C при отмене заказа (X-Service-Key).

    Бизнес-логика (canon b2b-flows.md#reserve-sku):
    - SKU блокируются SELECT FOR UPDATE; reserved_quantity -= qty (не ниже 0);
    - идемпотентность по order_id (повтор → {"ok": true} без повторного восстановления).
    """,
)
async def unreserve(
    request: UnreserveRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_service_key),
):
    """
    POST /api/v1/unreserve - Release a reservation (US-B2B-08).

    Canon test scenario:
    - unreserve_restores_quantities
    """
    try:
        result = await InventoryService.unreserve(
            db=db,
            order_id=request.order_id,
            items=request.items,
        )
        return JSONResponse(status_code=status.HTTP_200_OK, content=result)
    except ValueError as e:
        response = _value_error_response(str(e))
        if response is not None:
            return response
        raise
