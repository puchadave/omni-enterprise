"""PuchaVinted Connector - Vinted/Vintage sales-channel integration (capability-gated).

Note: Vinted has no openly documented public merchant API. This connector is a
configurable, ToS-aware skeleton: operations are enabled only when a confirmed
(Partner/Pro) API base URL, auth and endpoint contract are provided via
connector account credentials. Without confirmed access it reports CONFIG_REQUIRED
and does not perform unofficial/scraping flows.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from services.connector_sdk import (
    ChannelProduct,
    ConnectionState,
    ConnectorCapability,
    ExportResult,
    HealthResult,
    ListingStatus,
    SalesChannelConnector,
    ValidationResult,
)

logger = logging.getLogger("omni.connectors.vinted")


class PuchaVintedConnector(SalesChannelConnector):
    """Vinted/Vintage connector (orders, listings, payment-status sync as available)."""

    @property
    def connector_id(self) -> str:
        return "pucha_vinted"

    @property
    def platform_name(self) -> str:
        return "Vinted"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def api_version(self) -> str:
        return "partner/configurable"

    def get_capabilities(self) -> List[ConnectorCapability]:
        return [
            ConnectorCapability.PRODUCT_CREATE,
            ConnectorCapability.PRODUCT_UPDATE,
            ConnectorCapability.PRODUCT_DELETE,
            ConnectorCapability.INVENTORY_SYNC,
            ConnectorCapability.PRICE_SYNC,
            ConnectorCapability.ORDER_IMPORT,
            ConnectorCapability.FULFILLMENT_UPDATE,
            ConnectorCapability.LISTING_STATUS,
            ConnectorCapability.ATTRIBUTE_MAPPING,
            ConnectorCapability.CATEGORY_MAPPING,
        ]

    def _credentials_ready(self) -> bool:
        # Confirmed access required: base URL + token/session credentials.
        base = (self.config.get("api_base_url") or self.config.get("VINTED_API_BASE_URL") or "").strip()
        token = (
            self.config.get("access_token")
            or self.config.get("VINTED_ACCESS_TOKEN")
            or self.config.get("session")
            or ""
        )
        return bool(base and token)

    async def test_connection(self) -> HealthResult:
        if not self._credentials_ready():
            return HealthResult(
                status=ConnectionState.CONFIG_REQUIRED,
                healthy=False,
                message=(
                    "Vinted-Zugang nicht konfiguriert/bestätigt: api_base_url und "
                    "access_token (oder freigegebene Credentials) erforderlich. "
                    "Kein inoffizieller Zugriff ohne Freigabe."
                ),
                account_id=self.account_id,
            )
        # Live probe delegated to vinted_client once endpoint contract is confirmed.
        return HealthResult(
            status=ConnectionState.CONNECTED,
            healthy=True,
            message="Vinted credentials configured (endpoint probe pending confirmed contract).",
            account_id=self.account_id,
        )

    async def validate_product(self, product: Any) -> ValidationResult:
        errors: List[str] = []
        warnings: List[str] = []
        sku = getattr(product, "sku", "") or (product.get("sku", "") if isinstance(product, dict) else "")
        title = getattr(product, "product_name", "") or getattr(product, "title", "") or (
            product.get("title", "") if isinstance(product, dict) else ""
        )
        price = getattr(product, "supplier_price_brutto", None)
        if price is None and isinstance(product, dict):
            price = product.get("price")
        if not sku:
            errors.append("SKU is required.")
        if not title:
            errors.append("Title is required.")
        try:
            if price is not None and float(price) <= 0:
                warnings.append("Price should be > 0 for a sale listing.")
        except (TypeError, ValueError):
            warnings.append("Price is not a valid number.")
        if not self._credentials_ready():
            warnings.append("Vinted API access not configured; export/sync disabled until confirmed.")
        return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings, sku=sku, channel="vinted")

    async def transform_product(self, product: Any, channel_override: Optional[Dict[str, Any]] = None) -> ChannelProduct:
        sku = getattr(product, "sku", "") or (product.get("sku", "") if isinstance(product, dict) else "")
        title = getattr(product, "product_name", "") or getattr(product, "title", "") or (
            product.get("title", "") if isinstance(product, dict) else ""
        )
        desc = getattr(product, "description", "") or (product.get("description", "") if isinstance(product, dict) else "")
        price = float(
            getattr(product, "supplier_price_brutto", 0.0)
            or (product.get("price", 0.0) if isinstance(product, dict) else 0.0)
            or 0.0
        )
        stock = int(getattr(product, "stock_available", 0) or (product.get("stock", 0) if isinstance(product, dict) else 0))
        override = channel_override or {}
        return ChannelProduct(
            sku=sku,
            channel="vinted",
            title=str(override.get("title") or title),
            description=str(override.get("description") or desc),
            price=float(override.get("price") or price),
            currency=str(override.get("currency") or "EUR"),
            stock=stock,
            raw_payload={
                "sku": sku,
                "title": override.get("title") or title,
                "description": override.get("description") or desc,
                "price": float(override.get("price") or price),
                "stock": stock,
                "status": override.get("status") or "draft",
            },
        )

    async def export_product(self, product: ChannelProduct) -> ExportResult:
        if not self._credentials_ready():
            return ExportResult(success=False, sku=product.sku, channel="vinted", message="Vinted API not configured/confirmed; listing not exported.")
        return ExportResult(success=False, sku=product.sku, channel="vinted", message="Vinted listing export pending confirmed endpoint contract.")

    async def update_product(self, listing_id: str, product: ChannelProduct) -> ExportResult:
        if not self._credentials_ready():
            return ExportResult(success=False, sku=product.sku, channel="vinted", remote_listing_id=listing_id, message="Vinted API not configured/confirmed.")
        return ExportResult(success=False, sku=product.sku, channel="vinted", remote_listing_id=listing_id, message="Vinted listing update pending confirmed endpoint contract.")

    async def delete_product(self, listing_id: str) -> ExportResult:
        if not self._credentials_ready():
            return ExportResult(success=False, sku="", channel="vinted", remote_listing_id=listing_id, message="Vinted API not configured/confirmed.")
        return ExportResult(success=False, sku="", channel="vinted", remote_listing_id=listing_id, message="Vinted listing delete pending confirmed endpoint contract.")

    async def get_listing_status(self, listing_id: str) -> ListingStatus:
        return ListingStatus(sku=listing_id, channel="vinted", remote_listing_id=listing_id, status="unknown", last_sync_message="Status sync pending confirmed Vinted API access.")

    # --- Orders / payment / fulfillment (capability-gated stubs) ---

    async def import_orders(self, since: Optional[str] = None) -> Dict[str, Any]:
        """Fetch/import orders (idempotent). Implemented once endpoint contract confirmed."""
        if not self._credentials_ready():
            return {"ok": False, "reason": "config_required", "imported": 0, "orders": []}
        return {"ok": False, "reason": "not_implemented_pending_contract", "imported": 0, "orders": []}

    async def map_payment_status(self, external_status: str) -> str:
        s = (external_status or "").strip().lower()
        if s in {"paid", "authorized", "captured", "released", "completed"}:
            return "paid"
        if s in {"failed", "cancelled", "canceled", "expired"}:
            return "failed"
        if s in {"refunded", "partially_refunded", "partial_refund"}:
            return "refunded"
        return "pending"

    async def push_fulfillment(self, external_order_id: str, tracking: Dict[str, Any]) -> ExportResult:
        if not self._credentials_ready():
            return ExportResult(success=False, sku="", channel="vinted", remote_listing_id=external_order_id, message="Vinted API not configured/confirmed.")
        return ExportResult(success=False, sku="", channel="vinted", remote_listing_id=external_order_id, message="Tracking writeback pending confirmed endpoint contract.")
