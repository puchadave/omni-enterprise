"""Matterhorn WebIF Control Center.

Administrative interface for managing all Matterhorn integrations
within the Omni-Core ERP system.

Provides dashboard, connection management, feed management,
import control, and monitoring of all Matterhorn-related operations.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .matterhorn_rest_client import MatterhornAPIClient
from .matterhorn_connection_manager import MatterhornConnectionManager
from .brand_category_manager import BrandManager, CategoryManager
from .price_engine import PriceEngine, PriceCalculationResult
from .inventory_sync import InventorySyncResult, InventorySyncService
from .order_management import OrderManagementService, OrderItem, DeliveryAddress

logger = logging.getLogger(__name__)

# ── Control Center Status ─────────────────────────────────────────────────

class ControlCenterStatus:
    """Status information for the Matterhorn WebIF Control Center."""

    def __init__(self) -> None:
        self.api_connection: str = "unknown"
        self.last_api_call: datetime | None = None
        self.last_feed_update: datetime | None = None
        self.imported_products: int = 0
        self.imported_variants: int = 0
        self.available_ean_codes: int = 0
        self.active_brands: int = 0
        self.active_categories: int = 0
        stock_changes_today: int = 0
        price_changes_today: int = 0
        open_orders: int = 0
        error_count_24h: int = 0
        last_successful_sync: datetime | None = None
        status: str = "initializing"

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_connection": self.api_connection,
            "last_api_call": self.last_api_call.isoformat() if self.last_api_call else None,
            "last_feed_update": self.last_feed_update.isoformat() if self.last_feed_update else None,
            "imported_products": self.imported_products,
            "imported_variants": self.imported_variants,
            "available_ean_codes": self.available_ean_codes,
            "active_brands": self.active_brands,
            "active_categories": self.active_categories,
            "stock_changes_today": stock_changes_today,
            "price_changes_today": price_changes_today,
            "open_orders": open_orders,
            "error_count_24h": error_count_24h,
            "last_successful_sync": self.last_successful_sync.isoformat() if self.last_successful_sync else None,
            "status": self.status,
        }

# ── Control Center Core ───────────────────────────────────────────────────

class MatterhornControlCenter:
    """Central control center for all Matterhorn integrations.

    Provides dashboard data, connection management, and orchestration
    of all Matterhorn operations through the WebIF.
    """

    def __init__(self, api_client: MatterhornAPIClient | None = None) -> None:
        self.api_client: MatterhornAPIClient | None = api_client
        self.brand_manager: BrandManager = BrandManager()
        self.category_manager: CategoryManager = CategoryManager()
        self.price_engine: PriceEngine = PriceEngine()
        self.inventory_sync_service: InventorySyncService | None = None
        self.order_service: OrderManagementService | None = None
        self.status: ControlCenterStatus = ControlCenterStatus()
        self._initialized: bool = False

    # ── Initialization ───────────────────────────────────────────────────

    async def initialize(self, api_client: MatterhornAPIClient) -> None:
        """Initialize the control center with an API client.

        Loads existing brand/category mappings and sets up all services.
        """
        self.api_client = api_client
        self.order_service = OrderManagementService(api_client)

        # Load existing brands and categories from the brand/category managers
        # (These would normally be loaded from the database/persistence layer)
        # For now, we set up the services that can work with the API client
        self.inventory_sync_service = InventorySyncService(
            api_client=api_client,
            db_products={},  # Would be loaded from DB in production
        )

        self._initialized = True
        logger.info("Matterhorn Control Center initialized")

    # ── Status Dashboard ──────────────────────────────────────────────────

    async def update_status(self) -> ControlCenterStatus:
        """Update the control center status by checking all components.

        Returns:
            Updated ControlCenterStatus with current state information.
        """
        status = ControlCenterStatus()
        status.status = "checking"

        # 1. API Connection check
        if self.api_client and self.api_client.connection:
            health = await self.api_client.connection.check_health()
            if health.get("status") == "healthy":
                status.api_connection = "connected"
                status.last_api_call = datetime.now(timezone.utc)
                status.status = "online"
            else:
                status.api_connection = "error"
                status.status = "error"

        # 2. Count imported products from database (placeholder)
        # In production, this would query the actual product count
        status.imported_products = 0  # Placeholder

        # 3. Count active brands
        if self.brand_manager:
            status.active_brands = len(self.brand_manager.list_brands())

        # 4. Count active categories
        if self.category_manager:
            status.active_categories = len(self.category_manager.list_categories())

        # 5. EAN codes count
        # Placeholder - would come from product database
        status.available_ean_codes = 0

        # 6. Open orders count
        if self.order_service:
            # Placeholder - would query actual orders
            status.open_orders = 0

        # 7. Error count (placeholder)
        status.error_count_24h = 0

        # 8. Last successful sync
        status.last_successful_sync = datetime.now(timezone.utc)  # Placeholder

        self.status = status
        return status

    # ── Brand & Category Management ─────────────────────────────────────

    async def import_brands(self) -> dict[str, Any]:
        """Import brands from Matterhorn through the control center.

        Returns import statistics.
        """
        if not self.api_client:
            raise ValueError("API client not initialized")

        return await self.brand_manager.import_brands(self.api_client)

    async def import_categories(self) -> dict[str, Any]:
        """Import categories from Matterhorn through the control center.

        Returns import statistics.
        """
        if not self.api_client:
            raise ValueError("API client not initialized")

        return await self.category_manager.import_categories(self.api_client)

    # ── Price Calculation ───────────────────────────────────────────────

    def calculate_product_price(
        self, canonical_product: CanonicalProduct,
        purchase_netto: float | None = None,
        purchase_brutto: float | None = None,
    ) -> PriceCalculationResult:
        """Calculate the sales price for a product.

        Args:
            canonical_product: The product with supplier pricing
            purchase_netto: Explicit net purchase price (optional)
            purchase_brutto: Explicit gross purchase price (optional)

        Returns:
            PriceCalculationResult with all calculated values
        """
        return self.price_engine.calculate_price(
            canonical_product,
            purchase_netto=purchase_netto,
            purchase_brutto=purchase_brutto,
        )

    # ── Inventory Sync ──────────────────────────────────────────────────

    async def sync_inventory(self, force_full: bool = False) -> InventorySyncResult:
        """Run inventory synchronization against Matterhorn.

        Args:
            force_full: If True, force full sync ignoring last_update filters

        Returns:
            InventorySyncResult with statistics
        """
        if not self.inventory_sync_service:
            raise ValueError("Inventory sync service not initialized")

        return await self.inventory_sync_service.sync_all(force_full=force_full)

    # ── Order Management ────────────────────────────────────────────────

    async def create_order(
        self, items: list[OrderItem], delivery: DeliveryAddress,
        metadata: OrderMetadata = OrderMetadata(),
    ) -> dict[str, Any]:
        """Create a new order in Matterhorn.

        Args:
            items: List of OrderItem with variant_uid and quantity
            delivery: DeliveryAddress with all required fields
            metadata: Additional order metadata

        Returns:
            Order response from Matterhorn API
        """
        if not self.order_service:
            raise ValueError("Order service not initialized")

        return await self.order_service.create_order(items, delivery, metadata)

    # ── Feed Management ─────────────────────────────────────────────────

    async def import_feeds(self, feed_formats: list[str] | None = None) -> dict[str, Any]:
        """Import product feeds from Matterhorn.

        Args:
            feed_formats: List of formats to import (xml, csv, etc.)

        Returns:
            Import statistics
        """
        # This would integrate with the FeedManager
        # For now, return empty stats
        return {"imported": 0, "updated": 0, "errors": 0}

    # ── Export Methods for WebIF ────────────────────────────────────────

    def get_dashboard_data(self) -> dict[str, Any]:
        """Get data for the WebIF dashboard.

        Returns a dict with all the information needed to render
        the Matterhorn control center dashboard.
        """
        # Update status first
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            status = loop.run_until_complete(self.update_status())
        finally:
            loop.close()

        return {
            "status": status.to_dict() if status else {},
            "brands": self.brand_manager.list_brands() if self.brand_manager else [],
            "categories": self.category_manager.list_categories() if self.category_manager else [],
            "price_engine_stats": self.price_engine._global_rules if self.price_engine else {},
            "open_orders": getattr(self.order_service, 'open_orders', 0) if self.order_service else 0,
        }

    def get_api_diagnostics(self) -> dict[str, Any]:
        """Get API diagnostics for troubleshooting.

        Returns detailed information about the Matterhorn API connection
        and operation statistics.
        """
        if not self.api_client or not self.api_client.connection:
            return {"error": "API client not initialized"}

        conn = self.api_client.connection

        return {
            "api_version": conn.api_version,
            "request_count": conn.request_count,
            "error_count": conn.error_count,
            "success_count": conn.success_count,
            "endpoint_stats": conn.endpoint_stats,
        }