"""Google Merchant Center Autonomous Export Engine.

Event-driven export pipeline that automatically exports validated products
to Google Merchant Center after quality check passes.

Pipeline:
  product.quality_check.passed
    → AI Text Optimization (OmniRoute)
    → AI Image Generation (OmniRoute)
    → Product Validation
    → Google Merchant API
    → Google Merchant Center
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

from .google_merchant_client import (
    GoogleMerchantClient, GoogleMerchantError,
    GoogleMerchantAuthenticationError, GoogleMerchantNotFoundError,
    GoogleMerchantRateLimitError,
)
from .google_product_mapper import GoogleProductMapper, GoogleProductValidator, build_export_record

logger = logging.getLogger(__name__)


# ── Export Statuses ──────────────────────────────────────────────────────

EXPORT_PENDING: str = "pending"
EXPORT_PROCESSING: str = "processing"
EXPORT_VALIDATION: str = "validation"
EXPORT_EXPORTING: str = "exporting"
EXPORT_COMPLETED: str = "completed"
EXPORT_ERROR: str = "error"
EXPORT_WITHDRAWN: str = "withdrawn"
EXPORT_RETRY: str = "retry"


# ── Export Queue Item ────────────────────────────────────────────────────

class ExportQueueItem:
    """A single item in the Google Merchant export queue."""

    def __init__(
        self,
        export_id: str,
        account_id: str,
        product_sku: str,
        offer_id: str,
        payload: dict[str, Any],
        status: str = EXPORT_PENDING,
        max_retries: int = 3,
        retry_count: int = 0,
        response: dict[str, Any] | None = None,
        error_message: str = "",
        validation_score: int = 0,
        validation_errors: list[str] | None = None,
        validation_warnings: list[str] | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        self.export_id: str = export_id
        self.account_id: str = account_id
        self.product_sku: str = product_sku
        self.offer_id: str = offer_id
        self.payload: dict[str, Any] = payload
        self.status: str = status
        self.max_retries: int = max_retries
        self.retry_count: int = retry_count
        self.response: dict[str, Any] = response or {}
        self.error_message: str = error_message
        self.validation_score: int = validation_score
        self.validation_errors: list[str] = validation_errors or []
        self.validation_warnings: list[str] = validation_warnings or []
        self.created_at: str = created_at or datetime.now(timezone.utc).isoformat()
        self.updated_at: str = updated_at or self.created_at
        self.started_at: str | None = started_at
        self.completed_at: str | None = completed_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "export_id": self.export_id,
            "account_id": self.account_id,
            "product_sku": self.product_sku,
            "offer_id": self.offer_id,
            "payload": self.payload,
            "status": self.status,
            "max_retries": self.max_retries,
            "retry_count": self.retry_count,
            "response": self.response,
            "error_message": self.error_message,
            "validation_score": self.validation_score,
            "validation_errors": self.validation_errors,
            "validation_warnings": self.validation_warnings,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


# ── Autonomous Export Engine ─────────────────────────────────────────────

class AutonomousExportEngine:
    """Manages the autonomous Google Merchant Center export pipeline.

    Triggered by product.quality_check.passed events.
    Handles AI optimization, validation, and export to Google Merchant Center.
    """

    def __init__(
        self,
        merchant_client: GoogleMerchantClient,
        mapper: GoogleProductMapper,
        validator: GoogleProductValidator,
        omniroute_client: Any | None = None,
        max_concurrent: int = 5,
        rate_limit_per_second: float = 5.0,
        auto_export: bool = True,
        auto_inventory_sync: bool = True,
        auto_price_sync: bool = True,
    ) -> None:
        self.merchant_client: GoogleMerchantClient = merchant_client
        self.mapper: GoogleProductMapper = mapper
        self.validator: GoogleProductValidator = validator
        self.omniroute_client: Any | None = omniroute_client
        self.max_concurrent: int = max_concurrent
        self.rate_limit_per_second: float = rate_limit_per_second
        self.auto_export: bool = auto_export
        self.auto_inventory_sync: bool = auto_inventory_sync
        self.auto_price_sync: bool = auto_price_sync
        self._queue: list[ExportQueueItem] = []
        self._processing: dict[str, ExportQueueItem] = {}
        self._completed: list[ExportQueueItem] = []
        self._errors: list[ExportQueueItem] = []
        self._dead_letter: list[ExportQueueItem] = []
        self._lock: asyncio.Lock | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._rate_limiter: asyncio.Semaphore | None = None
        self._last_request_time: float = 0.0
        self._initialized: bool = False
        self._shop_url: str | None = None

    async def _ensure_initialized(self) -> None:
        """Initialize async primitives."""
        if not self._initialized:
            self._lock = asyncio.Lock()
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
            self._rate_limiter = asyncio.Semaphore(int(self.rate_limit_per_second))
            self._initialized = True

    # ── Event Handler ──────────────────────────────────────────────────

    async def on_quality_check_passed(
        self,
        product_sku: str,
        account_id: str,
        canonical_product: Any,
        ai_title: str | None = None,
        ai_description: str | None = None,
        ai_image_url: str | None = None,
        shop_url: str | None = None,
    ) -> str:
        """Handle product.quality_check.passed event.

        This is the main entry point for the autonomous export pipeline.
        Maps the product to Google format, validates it, and queues it for export.

        Returns:
            Export queue item ID
        """
        self._shop_url = shop_url

        # Step 1: Map canonical product to Google format
        google_product = self.mapper.map_product(
            canonical_product,
            shop_url=shop_url,
            ai_title=ai_title,
            ai_description=ai_description,
            ai_image_url=ai_image_url,
        )

        # Step 2: Validate the Google product
        validation_result = self.validator.validate(
            canonical_product, google_product, shop_url=shop_url
        )

        # Step 3: Build export record
        export_record = build_export_record(
            account_id=account_id,
            product_sku=product_sku,
            google_product=google_product,
            validation_result=validation_result,
        )

        # Step 4: Add to queue
        queue_id = str(uuid4())
        item = ExportQueueItem(
            export_id=queue_id,
            account_id=account_id,
            product_sku=product_sku,
            offer_id=export_record["offer_id"],
            payload=export_record["payload_json"],
            status=EXPORT_PENDING,
            max_retries=export_record["max_retries"],
            validation_score=export_record["validation_score"],
            validation_errors=export_record["validation_errors"],
            validation_warnings=export_record["validation_warnings"],
        )
        self._queue.append(item)

        logger.info(
            "Quality check passed - queued for Google export: %s (SKU: %s, score: %d)",
            queue_id, product_sku, validation_result.get("score", 0),
        )

        # Step 5: Auto-export if enabled
        if self.auto_export:
            asyncio.create_task(self.process_queue())

        return queue_id

    # ── Queue Management ────────────────────────────────────────────────

    def add_to_queue(
        self,
        account_id: str,
        product_sku: str,
        offer_id: str,
        payload: dict[str, Any],
        validation_score: int = 0,
        validation_errors: list[str] | None = None,
        validation_warnings: list[str] | None = None,
        max_retries: int = 3,
    ) -> str:
        """Manually add an item to the export queue.

        Returns the export queue item ID.
        """
        queue_id = str(uuid4())
        item = ExportQueueItem(
            export_id=queue_id,
            account_id=account_id,
            product_sku=product_sku,
            offer_id=offer_id,
            payload=payload,
            max_retries=max_retries,
            validation_score=validation_score,
            validation_errors=validation_errors or [],
            validation_warnings=validation_warnings or [],
        )
        self._queue.append(item)
        logger.info("Added to Google export queue: %s (%s)", queue_id, product_sku)
        return queue_id

    def get_queue_item(self, export_id: str) -> ExportQueueItem | None:
        """Get a queue item by ID."""
        for container in (self._queue, self._processing, self._completed, self._errors, self._dead_letter):
            if isinstance(container, dict):
                items = container.values()
            else:
                items = container
            for item in items:
                if item.export_id == export_id:
                    return item
        return None

    def list_queue(
        self,
        status: str | None = None,
        account_id: str | None = None,
        limit: int = 100,
    ) -> list[ExportQueueItem]:
        """List queue items with optional filtering."""
        all_items = (
            self._queue
            + list(self._processing.values())
            + self._completed
            + self._errors
            + self._dead_letter
        )

        if status:
            all_items = [i for i in all_items if i.status == status]
        if account_id:
            all_items = [i for i in all_items if i.account_id == account_id]

        all_items.sort(key=lambda i: i.created_at, reverse=True)
        return all_items[:limit]

    def get_queue_stats(self) -> dict[str, Any]:
        """Return queue statistics."""
        all_items = (
            self._queue
            + list(self._processing.values())
            + self._completed
            + self._errors
            + self._dead_letter
        )
        stats: dict[str, int] = {}
        for item in all_items:
            stats[item.status] = stats.get(item.status, 0) + 1

        return {
            "total": len(all_items),
            "pending": stats.get(EXPORT_PENDING, 0),
            "processing": stats.get(EXPORT_PROCESSING, 0),
            "completed": stats.get(EXPORT_COMPLETED, 0),
            "errors": stats.get(EXPORT_ERROR, 0),
            "withdrawn": stats.get(EXPORT_WITHDRAWN, 0),
            "retry": stats.get(EXPORT_RETRY, 0),
            "dead_letter": stats.get("dead_letter", 0),
            "avg_validation_score": round(
                sum(i.validation_score for i in all_items) / max(len(all_items), 1), 1
            ),
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
        assert self._rate_limiter is not None

        processed: int = 0
        succeeded: int = 0
        failed: int = 0

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
                    self._processing[item.export_id] = item

                    # Validate before export
                    if item.validation_score < 50:
                        item.status = EXPORT_ERROR
                        item.error_message = f"Validation score too low: {item.validation_score}"
                        item.completed_at = datetime.now(timezone.utc).isoformat()
                        self._errors.append(item)
                        failed += 1
                        return

                    # Export to Google Merchant Center
                    result = await self._export_to_google(item)

                    item.status = EXPORT_COMPLETED
                    item.response = result
                    item.completed_at = datetime.now(timezone.utc).isoformat()
                    succeeded += 1
                    logger.info("Google export completed: %s (%s)", item.export_id, item.product_sku)

                except GoogleMerchantAuthenticationError as exc:
                    item.retry_count += 1
                    item.error_message = f"Authentication error: {exc}"
                    item.updated_at = datetime.now(timezone.utc).isoformat()

                    if item.retry_count < item.max_retries:
                        item.status = EXPORT_RETRY
                        await self._schedule_retry(item)
                    else:
                        item.status = EXPORT_ERROR
                        item.completed_at = datetime.now(timezone.utc).isoformat()
                        self._dead_letter.append(item)
                        failed += 1
                        logger.error(
                            "Google export failed (auth, dead letter): %s: %s",
                            item.export_id, exc,
                        )

                except GoogleMerchantRateLimitError as exc:
                    item.retry_count += 1
                    item.error_message = f"Rate limited: {exc}"
                    item.updated_at = datetime.now(timezone.utc).isoformat()

                    if item.retry_count < item.max_retries:
                        item.status = EXPORT_RETRY
                        delay = min(300, 2 ** item.retry_count * 5)
                        await asyncio.sleep(delay)
                        item.status = EXPORT_PENDING
                        item.started_at = None
                        self._queue.append(item)
                    else:
                        item.status = EXPORT_ERROR
                        item.completed_at = datetime.now(timezone.utc).isoformat()
                        self._dead_letter.append(item)
                        failed += 1
                        logger.error(
                            "Google export failed (rate limit, dead letter): %s: %s",
                            item.export_id, exc,
                        )

                except GoogleMerchantError as exc:
                    item.retry_count += 1
                    item.error_message = str(exc)
                    item.updated_at = datetime.now(timezone.utc).isoformat()

                    if item.retry_count < item.max_retries:
                        item.status = EXPORT_RETRY
                        delay = min(60, 2 ** item.retry_count)
                        await asyncio.sleep(delay)
                        item.status = EXPORT_PENDING
                        item.started_at = None
                        self._queue.append(item)
                    else:
                        item.status = EXPORT_ERROR
                        item.completed_at = datetime.now(timezone.utc).isoformat()
                        self._dead_letter.append(item)
                        failed += 1
                        logger.error(
                            "Google export failed (dead letter): %s: %s",
                            item.export_id, exc,
                        )

                except Exception as exc:  # pylint: disable=broad-except
                    item.retry_count += 1
                    item.error_message = str(exc)
                    item.updated_at = datetime.now(timezone.utc).isoformat()

                    if item.retry_count < item.max_retries:
                        item.status = EXPORT_RETRY
                        delay = min(60, 2 ** item.retry_count)
                        await asyncio.sleep(delay)
                        item.status = EXPORT_PENDING
                        item.started_at = None
                        self._queue.append(item)
                    else:
                        item.status = EXPORT_ERROR
                        item.completed_at = datetime.now(timezone.utc).isoformat()
                        self._dead_letter.append(item)
                        failed += 1
                        logger.exception("Google export failed unexpectedly: %s", item.export_id)

                processed += 1
                self._processing.pop(item.export_id, None)
                self._rate_limiter.release()

        # Process all pending items
        tasks = [process_item(item) for item in pending]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Move completed items from processing to completed
        for item in list(self._processing.values()):
            if item.status == EXPORT_COMPLETED:
                self._completed.append(item)
                self._processing.pop(item.export_id, None)

        return {
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "remaining_pending": len([i for i in self._queue if i.status == EXPORT_PENDING]),
            "dead_letter": len(self._dead_letter),
        }

    async def _schedule_retry(self, item: ExportQueueItem) -> None:
        """Schedule a retry for an item."""
        delay = min(120, 2 ** item.retry_count * 10)
        await asyncio.sleep(delay)
        item.status = EXPORT_PENDING
        item.started_at = None
        self._queue.append(item)

    async def _export_to_google(self, item: ExportQueueItem) -> dict[str, Any]:
        """Export a single item to Google Merchant Center."""
        payload = item.payload
        offer_id = item.offer_id

        # Check if product already exists
        try:
            existing = await self.merchant_client.get_product(offer_id)
            # Product exists, update it
            result = await self.merchant_client.update_product(payload)
            logger.info("Updated existing Google product: %s", offer_id)
            return result
        except GoogleMerchantNotFoundError:
            # Product doesn't exist, create it
            result = await self.merchant_client.insert_product(payload)
            logger.info("Created new Google product: %s", offer_id)
            return result
        except GoogleMerchantError:
            # If we can't check, try insert (idempotent)
            result = await self.merchant_client.insert_product(payload)
            logger.info("Inserted Google product (fallback): %s", offer_id)
            return result

    # ── Inventory Sync ──────────────────────────────────────────────────

    async def sync_inventory(
        self,
        account_id: str,
        product_sku: str,
        offer_id: str,
        quantity: int,
    ) -> dict[str, Any]:
        """Sync inventory for a specific product."""
        if not self.auto_inventory_sync:
            return {"status": "skipped", "reason": "auto_inventory_sync disabled"}

        try:
            result = await self.merchant_client.update_inventory(offer_id, quantity)
            logger.info("Synced inventory for %s: %d units", offer_id, quantity)
            return result
        except GoogleMerchantError as exc:
            logger.error("Inventory sync failed for %s: %s", offer_id, exc)
            return {"status": "error", "error": str(exc)}

    async def sync_price(
        self,
        account_id: str,
        product_sku: str,
        offer_id: str,
        price_value: str,
        currency: str = "EUR",
    ) -> dict[str, Any]:
        """Sync price for a specific product."""
        if not self.auto_price_sync:
            return {"status": "skipped", "reason": "auto_price_sync disabled"}

        try:
            payload = {
                "offerId": offer_id,
                "price": {"value": price_value, "currency": currency},
            }
            result = await self.merchant_client.update_product(payload)
            logger.info("Synced price for %s: %s %s", offer_id, price_value, currency)
            return result
        except GoogleMerchantError as exc:
            logger.error("Price sync failed for %s: %s", offer_id, exc)
            return {"status": "error", "error": str(exc)}

    # ── Withdraw Product ────────────────────────────────────────────────

    async def withdraw_product(self, offer_id: str) -> dict[str, Any]:
        """Withdraw a product from Google Merchant Center."""
        try:
            result = await self.merchant_client.delete_product(offer_id)
            # Mark queue items as withdrawn
            for item in self._completed + self._processing.values():
                if item.offer_id == offer_id:
                    item.status = EXPORT_WITHDRAWN
            logger.info("Withdrew Google product: %s", offer_id)
            return result
        except GoogleMerchantError as exc:
            logger.error("Withdrawal failed for %s: %s", offer_id, exc)
            return {"status": "error", "error": str(exc)}

    # ── Retry Failed ────────────────────────────────────────────────────

    async def retry_failed(
        self,
        account_id: str | None = None,
        export_id: str | None = None,
    ) -> int:
        """Retry failed export items, optionally limited to one account and item.

        Returns the number of items re-queued.
        """
        retried = 0
        for item in list(self._errors) + list(self._dead_letter):
            if account_id and item.account_id != account_id:
                continue
            if export_id and item.export_id != export_id:
                continue
            item.status = EXPORT_PENDING
            item.retry_count = 0
            item.error_message = ""
            item.completed_at = None
            self._queue.append(item)
            if item in self._errors:
                self._errors.remove(item)
            if item in self._dead_letter:
                self._dead_letter.remove(item)
            retried += 1
        return retried

    # ── Health Check ────────────────────────────────────────────────────

    async def health_check(self) -> dict[str, Any]:
        """Check the health of the export engine and Google API connection."""
        try:
            health = await self.merchant_client.check_health()
            return {
                "engine": "healthy",
                "google_api": health,
                "queue_stats": self.get_queue_stats(),
                "auto_export": self.auto_export,
                "auto_inventory_sync": self.auto_inventory_sync,
                "auto_price_sync": self.auto_price_sync,
            }
        except Exception as exc:  # pylint: disable=broad-except
            return {
                "engine": "error",
                "google_api": {"status": "error", "error": str(exc)},
                "queue_stats": self.get_queue_stats(),
            }