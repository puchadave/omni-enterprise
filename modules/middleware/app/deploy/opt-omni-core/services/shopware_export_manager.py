"""Shopware Export Manager.

Handles the export queue for pushing products to Shopware.
Supports multiple export modes, idempotent processing, retry with
exponential backoff, rate limiting, and bulk operations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from datetime import datetime, timezone, timedelta
from typing import Any
from uuid import uuid4

from .canonical_product_mapping import CanonicalProduct
from .shopware_connection_manager import ShopwareConnectionManager
from .shopware_mapping_engine import MappingEngine, FieldMapping

logger = logging.getLogger(__name__)


# ── Export Statuses ──────────────────────────────────────────────────────

EXPORT_PENDING: str = "pending"
EXPORT_PROCESSING: str = "processing"
EXPORT_VALIDATION: str = "validation"
EXPORT_RELEASE_REQUIRED: str = "release_required"
EXPORT_EXPORTING: str = "exporting"
EXPORT_COMPLETED: str = "completed"
EXPORT_ERROR: str = "error"
EXPORT_RETRY: str = "retry"

# ── Export Modes ─────────────────────────────────────────────────────────

EXPORT_PUBLISH: str = "publish"
EXPORT_UPDATE: str = "update"
EXPORT_VARIANT_UPDATE: str = "variant_update"
EXPORT_PRICE_UPDATE: str = "price_update"
EXPORT_STOCK_UPDATE: str = "stock_update"
EXPORT_IMAGE_UPDATE: str = "image_update"
EXPORT_DESCRIPTION_UPDATE: str = "description_update"
EXPORT_SEO_UPDATE: str = "seo_update"
EXPORT_CATEGORY_UPDATE: str = "category_update"
EXPORT_FULL_SYNC: str = "full_sync"


# ── Export Queue Item ─────────────────────────────────────────────────────

class ExportQueueItem:
    """A single item in the export queue."""

    def __init__(
        self,
        queue_id: str,
        connection_id: str,
        product_sku: str,
        export_mode: str,
        payload: dict[str, Any],
        status: str = EXPORT_PENDING,
        max_retries: int = 3,
        retry_count: int = 0,
        response: dict[str, Any] | None = None,
        error_message: str = "",
        created_at: str | None = None,
        updated_at: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        self.queue_id: str = queue_id
        self.connection_id: str = connection_id
        self.product_sku: str = product_sku
        self.export_mode: str = export_mode
        self.payload: dict[str, Any] = payload
        self.status: str = status
        self.max_retries: int = max_retries
        self.retry_count: int = retry_count
        self.response: dict[str, Any] = response or {}
        self.error_message: str = error_message
        self.created_at: str = created_at or datetime.now(timezone.utc).isoformat()
        self.updated_at: str = updated_at or self.created_at
        self.started_at: str | None = started_at
        self.completed_at: str | None = completed_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "connection_id": self.connection_id,
            "product_sku": self.product_sku,
            "export_mode": self.export_mode,
            "payload": self.payload,
            "status": self.status,
            "max_retries": self.max_retries,
            "retry_count": self.retry_count,
            "response": self.response,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExportQueueItem:
        return cls(
            queue_id=data["queue_id"],
            connection_id=data["connection_id"],
            product_sku=data["product_sku"],
            export_mode=data["export_mode"],
            payload=data.get("payload", {}),
            status=data.get("status", EXPORT_PENDING),
            max_retries=data.get("max_retries", 3),
            retry_count=data.get("retry_count", 0),
            response=data.get("response"),
            error_message=data.get("error_message", ""),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
        )


# ── Export Manager ───────────────────────────────────────────────────────

class ExportManager:
    """Manages the Shopware export queue and processing."""

    def __init__(
        self,
        connection_manager: ShopwareConnectionManager,
        mapping_engine: MappingEngine,
        max_concurrent: int = 3,
        rate_limit_per_second: float = 10.0,
    ) -> None:
        self.connection_manager: ShopwareConnectionManager = connection_manager
        self.mapping_engine: MappingEngine = mapping_engine
        self.max_concurrent: int = max_concurrent
        self.rate_limit_per_second: float = rate_limit_per_second
        self._queue: list[ExportQueueItem] = []
        self._processing: dict[str, ExportQueueItem] = {}
        self._completed: list[ExportQueueItem] = []
        self._errors: list[ExportQueueItem] = []
        self._lock: asyncio.Lock | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._rate_limiter: asyncio.Semaphore | None = None
        self._last_request_time: float = 0.0
        self._initialized: bool = False

    async def _ensure_initialized(self) -> None:
        """Initialize async primitives."""
        if not self._initialized:
            self._lock = asyncio.Lock()
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
            self._rate_limiter = asyncio.Semaphore(int(self.rate_limit_per_second))
            self._initialized = True

    # ── Queue Management ────────────────────────────────────────────────

    def add_to_queue(
        self,
        connection_id: str,
        product_sku: str,
        export_mode: str,
        payload: dict[str, Any],
        max_retries: int = 3,
    ) -> str:
        """Add an item to the export queue.

        Returns the queue item ID.
        """
        queue_id = str(uuid4())
        item = ExportQueueItem(
            queue_id=queue_id,
            connection_id=connection_id,
            product_sku=product_sku,
            export_mode=export_mode,
            payload=payload,
            max_retries=max_retries,
        )
        self._queue.append(item)
        logger.info("Added to export queue: %s (%s, %s)", queue_id, product_sku, export_mode)
        return queue_id

    def get_queue_item(self, queue_id: str) -> ExportQueueItem | None:
        """Get a queue item by ID."""
        for item in self._queue:
            if item.queue_id == queue_id:
                return item
        for item in self._processing.values():
            if item.queue_id == queue_id:
                return item
        for item in self._completed:
            if item.queue_id == queue_id:
                return item
        for item in self._errors:
            if item.queue_id == queue_id:
                return item
        return None

    def list_queue(
        self,
        status: str | None = None,
        connection_id: str | None = None,
        limit: int = 100,
    ) -> list[ExportQueueItem]:
        """List queue items with optional filtering."""
        all_items = self._queue + list(self._processing.values()) + self._completed + self._errors

        if status:
            all_items = [i for i in all_items if i.status == status]
        if connection_id:
            all_items = [i for i in all_items if i.connection_id == connection_id]

        # Sort by created_at descending
        all_items.sort(key=lambda i: i.created_at, reverse=True)
        return all_items[:limit]

    def get_queue_stats(self) -> dict[str, Any]:
        """Return queue statistics."""
        all_items = self._queue + list(self._processing.values()) + self._completed + self._errors
        stats: dict[str, int] = {}
        for item in all_items:
            stats[item.status] = stats.get(item.status, 0) + 1

        return {
            "total": len(all_items),
            "pending": stats.get(EXPORT_PENDING, 0),
            "processing": stats.get(EXPORT_PROCESSING, 0),
            "completed": stats.get(EXPORT_COMPLETED, 0),
            "errors": stats.get(EXPORT_ERROR, 0),
            "retry": stats.get(EXPORT_RETRY, 0),
            "by_mode": {
                mode: sum(1 for i in all_items if i.export_mode == mode)
                for mode in set(i.export_mode for i in all_items)
            },
        }

    # ── Processing ──────────────────────────────────────────────────────

    async def process_queue(self, max_items: int | None = None) -> dict[str, Any]:
        """Process pending export queue items.

        Args:
            max_items: Maximum number of items to process in this run

        Returns:
            Processing summary
        """
        await self._ensure_initialized()
        assert self._lock is not None
        assert self._semaphore is not None

        processed: int = 0
        succeeded: int = 0
        failed: int = 0

        # Get pending items
        pending = [i for i in self._queue if i.status == EXPORT_PENDING]
        if max_items:
            pending = pending[:max_items]

        async def process_item(item: ExportQueueItem) -> None:
            nonlocal processed, succeeded, failed
            async with self._semaphore:
                await self._rate_limiter.acquire()
                try:
                    # Rate limiting
                    elapsed = time.monotonic() - self._last_request_time
                    min_interval = 1.0 / self.rate_limit_per_second
                    if elapsed < min_interval:
                        await asyncio.sleep(min_interval - elapsed)
                    self._last_request_time = time.monotonic()

                    item.status = EXPORT_PROCESSING
                    item.started_at = datetime.now(timezone.utc).isoformat()
                    self._processing[item.queue_id] = item

                    # Export based on mode
                    result = await self._export_item(item)

                    item.status = EXPORT_COMPLETED
                    item.response = result
                    item.completed_at = datetime.now(timezone.utc).isoformat()
                    succeeded += 1
                    logger.info("Export completed: %s (%s)", item.queue_id, item.product_sku)

                except Exception as exc:  # pylint: disable=broad-except
                    item.retry_count += 1
                    item.error_message = str(exc)
                    item.updated_at = datetime.now(timezone.utc).isoformat()

                    if item.retry_count < item.max_retries:
                        # Exponential backoff retry
                        delay = min(60, 2 ** item.retry_count)
                        item.status = EXPORT_RETRY
                        logger.warning("Export retry %d/%d for %s in %ds",
                                       item.retry_count, item.max_retries, item.queue_id, delay)
                        await asyncio.sleep(delay)
                        # Re-queue for retry
                        item.status = EXPORT_PENDING
                        item.started_at = None
                        self._queue.append(item)
                    else:
                        item.status = EXPORT_ERROR
                        item.completed_at = datetime.now(timezone.utc).isoformat()
                        self._errors.append(item)
                        failed += 1
                        logger.error("Export failed: %s (%s): %s", item.queue_id, item.product_sku, exc)

                    processed += 1
                    self._processing.pop(item.queue_id, None)

                finally:
                    self._rate_limiter.release()

        # Process all pending items
        tasks = [process_item(item) for item in pending]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Move completed items from processing to completed
        for item in list(self._processing.values()):
            if item.status == EXPORT_COMPLETED:
                self._completed.append(item)
                self._processing.pop(item.queue_id, None)

        return {
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "remaining_pending": len([i for i in self._queue if i.status == EXPORT_PENDING]),
        }

    async def _export_item(self, item: ExportQueueItem) -> dict[str, Any]:
        """Export a single queue item to Shopware."""
        export_mode = item.export_mode
        payload = item.payload

        if export_mode == EXPORT_PRICE_UPDATE:
            return await self._export_price_update(item, payload)
        if export_mode == EXPORT_STOCK_UPDATE:
            return await self._export_stock_update(item, payload)
        if export_mode == EXPORT_IMAGE_UPDATE:
            return await self._export_image_update(item, payload)
        if export_mode == EXPORT_DESCRIPTION_UPDATE:
            return await self._export_description_update(item, payload)
        if export_mode == EXPORT_SEO_UPDATE:
            return await self._export_seo_update(item, payload)
        if export_mode == EXPORT_CATEGORY_UPDATE:
            return await self._export_category_update(item, payload)
        if export_mode == EXPORT_VARIANT_UPDATE:
            return await self._export_variant_update(item, payload)
        if export_mode == EXPORT_FULL_SYNC:
            return await self._export_full_sync(item, payload)

        # Default: publish or update
        return await self._export_product(item, payload)

    # ── Individual Export Handlers ──────────────────────────────────────

    async def _export_product(self, item: ExportQueueItem, payload: dict[str, Any]) -> dict[str, Any]:
        """Export a complete product to Shopware."""
        # Check if product exists in Shopware first
        shopware_id = payload.get("shopware_id")
        if shopware_id:
            result = await self.connection_manager.update_product(shopware_id, payload)
        else:
            result = await self.connection_manager.create_product(payload)
        return result

    async def _export_price_update(self, item: ExportQueueItem, payload: dict[str, Any]) -> dict[str, Any]:
        """Export only price updates."""
        shopware_id = payload.get("shopware_id")
        price_data = {"price": payload.get("price"), "currency": payload.get("currency", "EUR")}
        if shopware_id:
            return await self.connection_manager.update_product(shopware_id, price_data)
        return {"status": "skipped", "reason": "no shopware_id"}

    async def _export_stock_update(self, item: ExportQueueItem, payload: dict[str, Any]) -> dict[str, Any]:
        """Export only stock updates."""
        shopware_id = payload.get("shopware_id")
        stock_data = {"stock": payload.get("stock", 0)}
        if shopware_id:
            return await self.connection_manager.update_product(shopware_id, stock_data)
        return {"status": "skipped", "reason": "no shopware_id"}

    async def _export_image_update(self, item: ExportQueueItem, payload: dict[str, Any]) -> dict[str, Any]:
        """Export image updates."""
        shopware_id = payload.get("shopware_id")
        image_data = {"media": payload.get("images", [])}
        if shopware_id:
            return await self.connection_manager.update_product(shopware_id, image_data)
        return {"status": "skipped", "reason": "no shopware_id"}

    async def _export_description_update(self, item: ExportQueueItem, payload: dict[str, Any]) -> dict[str, Any]:
        """Export description updates."""
        shopware_id = payload.get("shopware_id")
        desc_data = {"description": payload.get("description", "")}
        if shopware_id:
            return await self.connection_manager.update_product(shopware_id, desc_data)
        return {"status": "skipped", "reason": "no shopware_id"}

    async def _export_seo_update(self, item: ExportQueueItem, payload: dict[str, Any]) -> dict[str, Any]:
        """Export SEO data updates."""
        shopware_id = payload.get("shopware_id")
        seo_data = {
            "seoTitle": payload.get("seo_title", ""),
            "seoUrl": payload.get("seo_url", ""),
            "metaDescription": payload.get("meta_description", ""),
        }
        if shopware_id:
            return await self.connection_manager.update_product(shopware_id, seo_data)
        return {"status": "skipped", "reason": "no shopware_id"}

    async def _export_category_update(self, item: ExportQueueItem, payload: dict[str, Any]) -> dict[str, Any]:
        """Export category updates."""
        shopware_id = payload.get("shopware_id")
        cat_data = {"categories": payload.get("categories", [])}
        if shopware_id:
            return await self.connection_manager.update_product(shopware_id, cat_data)
        return {"status": "skipped", "reason": "no shopware_id"}

    async def _export_variant_update(self, item: ExportQueueItem, payload: dict[str, Any]) -> dict[str, Any]:
        """Export variant updates."""
        shopware_id = payload.get("shopware_id")
        variant_data = {"variants": payload.get("variants", [])}
        if shopware_id:
            return await self.connection_manager.update_product(shopware_id, variant_data)
        return {"status": "skipped", "reason": "no shopware_id"}

    async def _export_full_sync(self, item: ExportQueueItem, payload: dict[str, Any]) -> dict[str, Any]:
        """Full product synchronization."""
        return await self._export_product(item, payload)

    # ── Bulk Operations ─────────────────────────────────────────────────

    async def bulk_publish(
        self,
        connection_id: str,
        products: list[dict[str, Any]],
        mappings: list[FieldMapping],
    ) -> list[str]:
        """Bulk publish multiple products to Shopware.

        Returns list of queue item IDs.
        """
        queue_ids = []
        for product_data in products:
            sku = product_data.get("sku", "")
            # Apply mappings to transform to Shopware format
            shopware_payload = self.mapping_engine.apply_mapping(product_data)
            queue_id = self.add_to_queue(
                connection_id=connection_id,
                product_sku=sku,
                export_mode=EXPORT_PUBLISH,
                payload=shopware_payload,
            )
            queue_ids.append(queue_id)
        return queue_ids

    # ── Retry Failed ────────────────────────────────────────────────────

    async def retry_failed(self, connection_id: str | None = None) -> int:
        """Retry all failed export items.

        Returns the number of items re-queued.
        """
        retried = 0
        for item in list(self._errors):
            if connection_id and item.connection_id != connection_id:
                continue
            item.status = EXPORT_PENDING
            item.retry_count = 0
            item.error_message = ""
            item.completed_at = None
            self._queue.append(item)
            self._errors.remove(item)
            retried += 1
        return retried