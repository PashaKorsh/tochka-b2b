import logging
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.inventory.models import InventoryOperation
from backend.modules.inventory.schemas import InventoryItem
from backend.modules.products.models import SKU

logger = logging.getLogger(__name__)


class ReserveConflict(Exception):
    """Raised when at least one SKU cannot be reserved — all-or-nothing reject."""

    def __init__(self, failed_items: list):
        self.failed_items = failed_items
        super().__init__("Insufficient stock for one or more SKUs")


def _aggregate(items: list) -> dict:
    """Sum requested quantity per SKU (an item may repeat in the request)."""
    requested: dict = {}
    for item in items:
        requested[item.sku_id] = requested.get(item.sku_id, 0) + item.quantity
    return requested


class InventoryService:
    """
    Service layer for inventory reservation (US-B2B-08).

    Invariant: active_quantity + reserved_quantity = stock_quantity (on hand).
    `active_quantity` is the computed property SKU.active_quantity.
    """

    @staticmethod
    async def reserve(
        db: AsyncSession,
        idempotency_key: str,
        items: list,
    ) -> dict:
        """
        All-or-nothing reservation (canon b2b-flows.md#reserve-sku).

        - Idempotent by `idempotency_key`: a repeat returns the stored response
          without re-deducting.
        - Locks the targeted SKU rows with SELECT ... FOR UPDATE in a
          deterministic id order (deadlock-safe).
        - If ANY SKU has insufficient active_quantity → raises ReserveConflict,
          nothing is mutated (the transaction is not committed).
        - On success: reserved_quantity += qty for each SKU.

        Returns the 200 response dict {"reserved": True, "items": [...]}.

        Raises:
            ReserveConflict: at least one SKU short of stock (→ 409).
            ValueError: SKU not found / quantity <= 0.
        """
        op_key = f"reserve:{idempotency_key}"
        existing = await db.get(InventoryOperation, op_key)
        if existing is not None:
            return existing.result_json

        for item in items:
            if item.quantity <= 0:
                raise ValueError("quantity must be > 0")

        requested = _aggregate(items)
        if not requested:
            return {"reserved": True, "items": []}

        # Lock the SKU rows in a stable order to avoid deadlocks under contention.
        result = await db.execute(
            select(SKU)
            .where(SKU.id.in_(list(requested.keys())))
            .order_by(SKU.id)
            .with_for_update()
        )
        skus = {sku.id: sku for sku in result.scalars().all()}

        for sku_id in requested:
            if sku_id not in skus:
                raise ValueError("SKU not found")

        # Validate every SKU before mutating any — all-or-nothing.
        failed = []
        for sku_id, qty in requested.items():
            sku = skus[sku_id]
            available = sku.stock_quantity - sku.reserved_quantity  # active_quantity
            if available < qty:
                failed.append({
                    "sku_id": str(sku_id),
                    "requested": qty,
                    "available": available,
                    "reason": "OUT_OF_STOCK" if available == 0 else "INSUFFICIENT_STOCK",
                })
        if failed:
            raise ReserveConflict(failed)

        # Apply the reservation.
        response_items = []
        out_of_stock = []
        for sku_id, qty in requested.items():
            sku = skus[sku_id]
            sku.reserved_quantity += qty
            active = sku.stock_quantity - sku.reserved_quantity
            response_items.append({
                "sku_id": str(sku_id),
                "reserved_quantity": sku.reserved_quantity,
                "remaining_stock": active,
            })
            if active == 0:
                out_of_stock.append(sku)

        result_json = {"reserved": True, "items": response_items}
        db.add(InventoryOperation(operation_key=op_key, result_json=result_json))
        await db.commit()

        # After commit — notify B2C about SKUs that went out of stock.
        for sku in out_of_stock:
            await InventoryService._send_sku_out_of_stock(sku.product_id, sku.id)

        return result_json

    @staticmethod
    async def unreserve(
        db: AsyncSession,
        order_id: UUID,
        items: list,
    ) -> dict:
        """
        Compensating operation — release a reservation (canon b2b-flows.md#reserve-sku).

        Idempotent by `order_id`. Locks SKU rows FOR UPDATE, then
        reserved_quantity -= qty (clamped at 0). Returns {"ok": True}.

        Raises:
            ValueError: SKU not found / quantity <= 0.
        """
        op_key = f"unreserve:{order_id}"
        existing = await db.get(InventoryOperation, op_key)
        if existing is not None:
            return existing.result_json

        for item in items:
            if item.quantity <= 0:
                raise ValueError("quantity must be > 0")

        requested = _aggregate(items)
        if requested:
            result = await db.execute(
                select(SKU)
                .where(SKU.id.in_(list(requested.keys())))
                .order_by(SKU.id)
                .with_for_update()
            )
            skus = {sku.id: sku for sku in result.scalars().all()}

            for sku_id in requested:
                if sku_id not in skus:
                    raise ValueError("SKU not found")

            for sku_id, qty in requested.items():
                sku = skus[sku_id]
                # Clamp at 0 — never let reserved_quantity go negative.
                sku.reserved_quantity = max(0, sku.reserved_quantity - qty)

        result_json = {"ok": True}
        db.add(InventoryOperation(operation_key=op_key, result_json=result_json))
        await db.commit()
        return result_json

    @staticmethod
    async def _send_sku_out_of_stock(product_id: UUID, sku_id: UUID) -> None:
        """
        Notify B2C that a SKU's active_quantity hit 0 (canon b2b-flows.md#reserve-sku).

        POST {b2c_url}/api/v1/events/product, X-Service-Key, body:
        {idempotency_key, event: "SKU_OUT_OF_STOCK", product_id, sku_id, date}.
        Fire-and-forget after commit — на ошибке логируем, ответ не ломаем.
        """
        b2c_url = os.getenv("B2C_URL", "http://b2c:8000")
        service_key = os.getenv("B2B_TO_B2C_KEY", "dev-b2b-to-b2c-key")

        event_payload = {
            "idempotency_key": str(uuid4()),
            "event": "SKU_OUT_OF_STOCK",
            "product_id": str(product_id),
            "sku_id": str(sku_id),
            "date": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{b2c_url}/api/v1/events/product",
                    json=event_payload,
                    headers={"X-Service-Key": service_key},
                )
                response.raise_for_status()
        except Exception as e:
            logger.warning("Failed to send SKU_OUT_OF_STOCK event: %s", e, exc_info=True)
