"""FastAPI application for the Omni-Core ERP & BI middleware.

The service is intentionally single-process and uses an in-memory asyncio queue
for short background tasks. Durable application state always resides in the
configured PostgreSQL database (or SQLite WAL fallback for local development).
"""

from __future__ import annotations

import asyncio
import csv
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from database import (
    DATABASE_URL,
    FulfillmentBlockedError,
    InsufficientStockError,
    close_pg_pool,
    count_dashboard_metrics,
    create_order as persist_order,
    fetch_order,
    fetch_product,
    get_setting,
    init_db,
    list_bundles,
    list_orders,
    list_products,
    list_settings,
    recent_import_runs,
    job_create,
    job_list,
    job_get,
    job_update,
    job_claim_next,
    job_complete,
    job_fail,
    job_cancel,
    job_retry,
    scheduler_list,
    scheduler_upsert,
    scheduler_mark_run,
    matterhorn_get_config,
    matterhorn_set_config,
    matterhorn_list_feeds,
    matterhorn_upsert_feed,
    matterhorn_delete_feed,
    matterhorn_list_mappings,
    matterhorn_upsert_mapping,
    matterhorn_delete_mapping,
    matterhorn_list_previews,
    matterhorn_get_preview,
    list_import_runs,
    news_list_sources,
    news_upsert_source,
    news_list_items,
    news_add_item,
    knowledge_list,
    knowledge_upsert,
    audit_list,
    analytics_overview,
    recent_logs,
    refresh_all_bundle_stocks,
    reserve_order_stock,
    set_setting,
    update_order_status,
    upsert_product,
    upsert_product_aggregate,
    delete_product,
    delete_bundle,
    upsert_bundle,
    write_log,
    iam_get_user_by_email, iam_get_user, iam_list_users, iam_update_user, iam_ensure_role, iam_assign_role, iam_list_roles, iam_list_permissions, iam_grant_permission, iam_create_session, iam_get_session, iam_revoke_session, iam_touch_session, iam_get_user_roles, iam_create_user,
    finance_document_upsert, finance_transaction_upsert, finance_settlement_upsert,
    finance_document_list, finance_transaction_list, finance_settlement_list,
    finance_transaction_get, finance_document_candidates, finance_reconciliation_create,
    finance_reconciliation_list, finance_overview,
)
from services.fulfillment import FulfillmentService
from services.finance import calculate_contribution_margin, reconcile_payment_to_documents
from services.inventory_guard import InventoryGuard
from services.matterhorn_importer import MatterhornUniversalImporter
from services.omniroute_client import OmniRouteClient
from services.pricing_engine import PricingEngine
from services.shopware_connection_manager import ShopwareConnectionManager
from services.shopware_mapping_engine import MappingEngine, FieldMapping, MAPPING_DIRECT, get_default_mappings
from services.shopware_export_manager import ExportManager
from services.shopware_description_engine import DescriptionEngine, SEOCenter
from services.competitor_provider import provider_registry
from services.competitor_matching import ProductMatchingEngine
from services.competitor_pricing import DynamicPricingEngine
from services.google_merchant_client import GoogleMerchantClient
from services.google_product_mapper import GoogleProductMapper, GoogleProductValidator
from services.google_export_engine import AutonomousExportEngine
from services.omniroute_provider import AIProvider, ProviderManager
from services.omniroute_model_registry import AIModel, ModelRegistry, RoutingRule, RoutingPolicy, IntelligentRoutingEngine, CostCalculator
from services.trend_engine import TrendEngine
from services.event_bus import EventBus, get_event_bus, shutdown_event_bus
from services.global_control_center import GlobalControlCenter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

SECRET_KEY_MARKERS = ("api_key", "token", "secret", "password", "credential")
SESSION_COOKIE_NAME = "omni_session"
CSRF_COOKIE_NAME = "csrf_token"
SESSION_TTL_SECONDS = 8 * 60 * 60
STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# RBAC: permission required by (method, path_prefix). Most-specific prefix wins.
# Unclassified paths are denied unless explicitly granted outside the default roles.
RBAC_RULES: list[tuple[str, tuple[str, ...], str]] = [
    ("GET", ("/docs", "/redoc", "/openapi.json"), "system.read"),
    ("GET", ("/", "/api/stats", "/api/analytics", "/api/health", "/api/health/integrations", "/api/integrations/", "/api/control-center/", "/control-center"), "system.read"),
    ("GET", ("/api/auth/me", "/api/auth/logout"), "system.read"),
    ("POST", ("/api/auth/logout",), "system.read"),
    ("GET", ("/api/iam/", "/ui/security"), "users.manage"),
    ("POST", ("/api/iam/",), "users.manage"),
    ("PUT", ("/api/iam/",), "users.manage"),
    ("DELETE", ("/api/iam/",), "users.manage"),
    ("POST", ("/api/settings",), "system.configure"),
    ("GET", ("/api/settings", "/ui/settings", "/settings"), "system.configure"),
    ("POST", ("/api/matterhorn/",), "matterhorn.configure"),
    ("PUT", ("/api/matterhorn/",), "matterhorn.configure"),
    ("DELETE", ("/api/matterhorn/",), "matterhorn.configure"),
    ("POST", ("/api/import/matterhorn",), "matterhorn.import"),
    ("GET", ("/api/matterhorn/", "/api/import/matterhorn", "/api/import-runs", "/ui/matterhorn-import"), "matterhorn.read"),
    ("POST", ("/api/shopware/",), "shopware.configure"),
    ("GET", ("/api/shopware/", "/api/stats/shopware", "/ui/shopware"), "shopware.read"),
    ("POST", ("/api/omniroute/", "/api/ai/"), "ai.configure"),
    ("GET", ("/api/omniroute/", "/ui/ai-control", "/omniroute"), "ai.configure"),
    ("POST", ("/api/ai-agent/",), "ai.configure"),
    ("GET", ("/api/ai-agent/",), "ai.configure"),
    ("GET", ("/api/creative/", "/api/photoshoot/", "/ui/ai-photoshooting", "/ui/content-studio"), "ai.generate"),
    ("POST", ("/api/creative/", "/api/photoshoot/"), "ai.configure"),
    ("POST", ("/api/content/generate", "/api/marketing/"), "ai.generate"),
    ("GET", ("/api/content/", "/api/marketing/"), "products.read"),
    ("POST", ("/api/pricing/",), "pricing.write"),
    ("GET", ("/api/pricing/", "/ui/pricing-center"), "pricing.read"),
    ("POST", ("/api/bundles",), "bundles.manage"),
    ("DELETE", ("/api/bundles/",), "bundles.manage"),
    ("GET", ("/api/bundles", "/ui/bundle-studio"), "bundles.read"),
    ("POST", ("/api/products",), "products.write"),
    ("PUT", ("/api/products",), "products.write"),
    ("DELETE", ("/api/products",), "products.delete"),
    ("GET", ("/api/products", "/api/categories", "/ui/products", "/admin/products"), "products.read"),
    ("GET", ("/api/orders", "/ui/orders", "/admin/orders"), "fulfillment.read"),
    ("POST", ("/api/orders",), "fulfillment.manage"),
    ("GET", ("/api/finance/",), "finance.read"),
    ("POST", ("/api/finance/contribution-margin",), "finance.read"),
    ("POST", ("/api/finance/",), "finance.write"),
    ("GET", ("/api/audit-log", "/api/logs", "/ui/audit-log"), "audit.read"),
    ("GET", ("/api/events/", "/ui/internal-communication"), "audit.read"),
    ("POST", ("/api/events/",), "system.configure"),
    ("GET", ("/api/jobs", "/ui/jobs-automation"), "system.configure"),
    ("POST", ("/api/jobs",), "system.configure"),
    ("GET", ("/api/scheduler/",), "system.configure"),
    ("POST", ("/api/scheduler/",), "system.configure"),
    ("GET", ("/api/connectors", "/ui/connectors"), "connectors.read"),
    ("POST", ("/api/connectors",), "connectors.configure"),
    ("DELETE", ("/api/connectors",), "connectors.configure"),
    ("GET", ("/api/crm/", "/ui/crm"), "products.read"),
    ("POST", ("/api/crm/",), "system.configure"),
    ("GET", ("/api/competitor/providers", "/api/stats/competitor", "/ui/competitor-intelligence"), "competitor.read"),
    ("POST", ("/api/competitor/",), "competitor.manage"),
    ("GET", ("/api/sales/overview", "/api/sales/product-scores"), "products.read"),
    ("GET", ("/api/sales/pricing-recommendations",), "pricing.read"),
    ("GET", ("/api/scene-intelligence/", "/api/news/", "/ui/news-events", "/ui/trend-intelligence"), "trends.read"),
    ("POST", ("/api/news/",), "trends.manage"),
    ("GET", ("/api/knowledge", "/ui/knowledge-base"), "system.read"),
    ("POST", ("/api/knowledge",), "system.configure"),
    ("GET", ("/api/export/",), "exports.execute"),
    ("POST", ("/api/export/", "/api/pipeline/"), "exports.execute"),
    ("GET", ("/api/google/accounts", "/api/google/export/health/", "/google"), "connectors.read"),
    ("POST", ("/api/google/accounts",), "connectors.configure"),
    ("GET", ("/api/google/export/",), "exports.execute"),
    ("POST", ("/api/google/export/", "/api/google/sync"), "exports.execute"),
    ("GET", ("/api/gtin/", "/ui/gtin-intelligence"), "products.read"),
    ("POST", ("/api/gtin/lookup",), "products.read"),
    ("POST", ("/api/gtin/providers",), "products.write"),
    ("GET", ("/ui/master-dashboard", "/ui/analytics"), "system.read"),
    ("GET", ("/ui/inventory",), "products.read"),
    ("GET", ("/ui/erp-pim",), "products.read"),
    ("GET", ("/ui/integrations", "/ui/marketplaces"), "connectors.read"),
    ("GET", ("/ui/model-management",), "ai.configure"),
    ("GET", ("/ui/competitor-intelligence",), "competitor.read"),
    ("GET", ("/ui/trend-intelligence",), "trends.read"),
    ("GET", ("/ui/knowledge-base",), "system.read"),
    ("GET", ("/api/import/matterhorn/preview",), "matterhorn.read"),
]



class SettingsUpdate(BaseModel):
    """Validated single-setting update."""

    key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    value: str = Field(max_length=4096)


class ProductCreate(BaseModel):
    """Validated manual/extended product input."""

    sku: str = Field(min_length=1, max_length=64)
    external_matterhorn_id: str = Field(default="", max_length=128)
    parent_sku: str = Field(default="", max_length=64)
    title: str = Field(min_length=1, max_length=512)
    brand: str = Field(default="", max_length=128)
    manufacturer: str = Field(default="", max_length=128)
    category: str = Field(default="", max_length=128)
    short_description: str = Field(default="", max_length=10_000)
    raw_description: str = Field(default="", max_length=50_000)
    seo_title: str = Field(default="", max_length=255)
    meta_description: str = Field(default="", max_length=1000)
    slug: str = Field(default="", max_length=255)
    canonical_url: str = Field(default="", max_length=1000)
    supplier_price: float = Field(default=0, ge=0, le=1_000_000)
    purchase_price: float = Field(default=0, ge=0, le=1_000_000)
    sale_price: float = Field(default=0, ge=0, le=1_000_000)
    compare_at_price: float = Field(default=0, ge=0, le=1_000_000)
    tax_rate: float = Field(default=0, ge=0, le=100)
    currency: str = Field(default="EUR", max_length=8)
    stock: int = Field(default=0, ge=0, le=10_000_000)
    stock_quantity: int | None = Field(default=None, ge=0, le=10_000_000)
    reserved_quantity: int = Field(default=0, ge=0, le=10_000_000)
    available_quantity: int | None = Field(default=None, ge=0, le=10_000_000)
    reorder_level: int = Field(default=0, ge=0, le=10_000_000)
    lead_time_days: int = Field(default=0, ge=0, le=10_000)
    supplier_stock: int = Field(default=0, ge=0, le=10_000_000)
    warehouse_location: str = Field(default="", max_length=128)
    bin_code: str = Field(default="", max_length=128)
    ean: str = Field(default="", max_length=64)
    barcode: str = Field(default="", max_length=64)
    requires_serial_number: bool = False
    is_fragile: bool = False
    is_hazmat: bool = False
    weight_net_kg: float = Field(default=0, ge=0, le=1_000_000)
    weight_gross_kg: float = Field(default=0, ge=0, le=1_000_000)
    length_cm: float = Field(default=0, ge=0, le=1_000_000)
    width_cm: float = Field(default=0, ge=0, le=1_000_000)
    height_cm: float = Field(default=0, ge=0, le=1_000_000)
    hs_code: str = Field(default="", max_length=64)
    country_of_origin: str = Field(default="", max_length=64)
    main_image_url: str = Field(default="", max_length=2000)
    gallery_json: list[Any] = Field(default_factory=list)
    videos_json: list[Any] = Field(default_factory=list)
    documents_json: list[Any] = Field(default_factory=list)
    marketplace_mappings_json: dict[str, Any] = Field(default_factory=dict)
    variants: list[dict[str, Any]] = Field(default_factory=list)
    attributes: list[dict[str, Any]] = Field(default_factory=list)
    categories: list[Any] = Field(default_factory=list)
    status: Literal["active", "draft", "paused", "archived"] = "active"


class OrderCreate(BaseModel):
    """Validated channel order input."""

    order_id: str = Field(min_length=1, max_length=64)
    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=10_000)
    marketplace: str = Field(min_length=1, max_length=32)
    customer_reference: str = Field(default="", max_length=256)


class PricingRequest(BaseModel):
    """Validated price-calculation input."""

    supplier_price: float = Field(ge=0, le=1_000_000)



class JobCreateRequest(BaseModel):
    type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    run_at: str | None = None
    max_attempts: int = Field(default=3, ge=1, le=20)

class SchedulerConfigRequest(BaseModel):
    job_type: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    interval_seconds: int = Field(default=3600, ge=30, le=31_536_000)
    payload: dict[str, Any] = Field(default_factory=dict)

class MatterhornConfigPayload(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)

class MatterhornFeedPayload(BaseModel):
    id: str | None = None
    name: str = "Matterhorn Feed"
    feed_url: str = ""
    feed_type: str = "xml"
    enabled: bool = True
    config_json: dict[str, Any] = Field(default_factory=dict)

class MatterhornMappingPayload(BaseModel):
    id: str | None = None
    source_field: str = ""
    target_field: str = ""
    transform: str = ""
    enabled: bool = True
    config_json: dict[str, Any] = Field(default_factory=dict)

class MatterhornImportRequest(BaseModel):
    mode: str = Field(default="safe")  # safe|partial|full|dry_run|stock_only|ek_only
    feed_url: str | None = None
    preview_id: str | None = None
    confirm_full_without_preview: bool = False
    dry_run: bool = False
    async_job: bool = False
    sync_rules: dict[str, Any] = Field(default_factory=dict)
    filename: str | None = None
    content: str | None = None  # optional inline content (for upload flows)


class FinancePayloadRequest(BaseModel):
    payload_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider", "external_id", mode="before", check_fields=False)
    @classmethod
    def require_finance_identifier(cls, value: Any) -> Any:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Provider and external ID must not be blank.")
        return value.strip()

    @field_validator("order_reference", "settlement_reference", mode="before", check_fields=False)
    @classmethod
    def normalize_finance_reference(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("payload_json")
    @classmethod
    def limit_finance_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")) > 65_536:
            raise ValueError("Finance source metadata must not exceed 64 KiB.")
        return value


class FinanceDocumentRequest(FinancePayloadRequest):
    provider: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=256)
    document_type: Literal["invoice", "credit_note", "supplier_invoice", "expense"]
    direction: Literal["receivable", "payable"]
    order_reference: str = Field(default="", max_length=256)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    net_amount: Decimal = Field(ge=0)
    tax_amount: Decimal = Field(ge=0)
    gross_amount: Decimal = Field(ge=0)
    status: str = Field(default="issued", min_length=1, max_length=32)
    issued_at: datetime | None = None
    due_at: datetime | None = None

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("issued_at", "due_at")
    @classmethod
    def normalize_document_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class FinanceTransactionRequest(FinancePayloadRequest):
    provider: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=256)
    transaction_type: Literal["payment", "refund", "fee", "payout", "adjustment"]
    order_reference: str = Field(default="", max_length=256)
    settlement_reference: str = Field(default="", max_length=256)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    amount: Decimal = Field(ge=0)
    occurred_at: datetime | None = None

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("occurred_at")
    @classmethod
    def normalize_transaction_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class FinanceSettlementRequest(FinancePayloadRequest):
    provider: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=256)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    gross_amount: Decimal = Field(ge=0)
    fees_amount: Decimal = Field(ge=0)
    net_amount: Decimal = Field()
    status: str = Field(default="received", min_length=1, max_length=32)
    settled_at: datetime | None = None

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("settled_at")
    @classmethod
    def normalize_settlement_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class FinanceReconcileRequest(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=64)


class FinanceContributionMarginRequest(BaseModel):
    revenue: Decimal = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    cost_of_goods: Decimal | None = Field(default=None, ge=0)
    marketplace_fee: Decimal | None = Field(default=None, ge=0)
    payment_fee: Decimal | None = Field(default=None, ge=0)
    shipping: Decimal | None = Field(default=None, ge=0)
    packaging: Decimal | None = Field(default=None, ge=0)
    returns: Decimal | None = Field(default=None, ge=0)

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value



class AppRuntime:
    """Small in-process runtime state; it deliberately contains no durable data."""

    def __init__(self) -> None:
        self.jobs: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1_000)
        self.worker_task: asyncio.Task[None] | None = None
        self.trend_task: asyncio.Task[None] | None = None
        self.scheduler_task: asyncio.Task[None] | None = None
        self.persistent_job_task: asyncio.Task[None] | None = None
        self.importer = MatterhornUniversalImporter()
        self.fulfillment = FulfillmentService()
        self.pricing = PricingEngine()
        self.inventory = InventoryGuard()
        self.omniroute: OmniRouteClient | None = None
        self.started_at = time.monotonic()
        self.omniroute_latency_ms: int | None = None
        self.omniroute_healthy = False
        # Shopware integration
        self.shopware_connections: dict[str, ShopwareConnectionManager] = {}
        self.shopware_mapping_engines: dict[str, MappingEngine] = {}
        self.shopware_export_manager: ExportManager | None = None
        self.shopware_description_engine: DescriptionEngine | None = None
        self.shopware_seo_center: SEOCenter | None = None
        # Competitor intelligence
        self.competitor_matching_engine: ProductMatchingEngine | None = None
        self.competitor_pricing_engine: DynamicPricingEngine | None = None
        # Google Merchant Center
        self.google_merchant_clients: dict[str, GoogleMerchantClient] = {}
        self.google_mappers: dict[str, GoogleProductMapper] = {}
        self.google_validators: dict[str, GoogleProductValidator] = {}
        self.google_export_engines: dict[str, AutonomousExportEngine] = {}
        # OmniRoute Control Center
        self.omniroute_provider_manager: ProviderManager | None = None
        self.omniroute_model_registry: ModelRegistry | None = None
        self.omniroute_routing_engine: IntelligentRoutingEngine | None = None
        self.omniroute_cost_calculator: CostCalculator | None = None
        # Internal communication & control center
        self.event_bus: EventBus | None = None
        self.control_center: GlobalControlCenter | None = None

    @property
    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self.started_at)


runtime = AppRuntime()
templates = Jinja2Templates(directory="templates")


def is_secret_key(key: str) -> bool:
    """Classify configuration keys that must be masked in responses."""
    normalized = key.lower()
    return any(marker in normalized for marker in SECRET_KEY_MARKERS)


async def configure_omniroute() -> None:
    """Load the optional OmniRoute configuration after database migration."""
    base_url = await get_setting("omniroute_url") or await get_setting("omni_route_base_url")
    api_key = await get_setting("omniroute_api_key") or await get_setting("omni_route_api_key")
    if not base_url:
        runtime.omniroute = None
        runtime.omniroute_healthy = False
        return
    runtime.omniroute = OmniRouteClient(base_url, api_key or "")
    started = time.monotonic()
    runtime.omniroute_healthy = await runtime.omniroute.check_health()
    runtime.omniroute_latency_ms = int((time.monotonic() - started) * 1000)


async def _handle_fulfillment_job(job: dict[str, Any]) -> None:
    """Reserve inventory and dispatch a durable queued order."""
    order_id = str(job["order_id"])
    try:
        await reserve_order_stock(order_id, str(job["sku"]), int(job["quantity"]))
        persisted_order = await fetch_order(order_id)
        if (
            not persisted_order
            or str(persisted_order.get("payment_status") or "").lower() != "paid"
            or bool(persisted_order.get("fulfillment_locked"))
        ):
            raise FulfillmentBlockedError(
                f"Fulfillment is blocked until order {order_id} is paid and unlocked."
            )
        result = await runtime.fulfillment.dispatch_reserved_order({**job, **persisted_order})
        if result["status"] == "dispatched":
            await write_log("INFO", "order_dispatched", f"Order {order_id} dispatched.", {"order_id": order_id})
        else:
            await write_log("ERROR", "order_dispatch_failed", result["message"], {"order_id": order_id})
    except InsufficientStockError as exc:
        await update_order_status(order_id, "failed", error=str(exc))
        await write_log("WARNING", "order_stock_rejected", str(exc), {"order_id": order_id})
    except FulfillmentBlockedError as exc:
        await update_order_status(order_id, "received", error=str(exc))
        await write_log("WARNING", "order_fulfillment_blocked", str(exc), {"order_id": order_id})
    except Exception as exc:  # Defensive boundary: worker remains alive.
        logger.exception("Fulfillment job failed for %s", order_id)
        await update_order_status(order_id, "failed", error="Supplier dispatch failed")
        await write_log("ERROR", "order_fulfillment_exception", str(exc), {"order_id": order_id})


async def fulfillment_worker() -> None:
    """Process one queue job at a time, preserving queue accounting on failures."""
    while True:
        job = await runtime.jobs.get()
        try:
            if job.get("type") == "fulfillment":
                await _handle_fulfillment_job(job)
            else:
                await write_log("WARNING", "unknown_job", "Discarded unknown queue job.")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unhandled job worker exception")
        finally:
            runtime.jobs.task_done()


async def trend_scheduler() -> None:
    """Run weekly trend analysis only when an OmniRoute gateway is configured."""
    while True:
        try:
            if runtime.omniroute is not None:
                await TrendEngine(runtime.omniroute).run_weekly_trend_job()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Weekly trend job failed")
        await asyncio.sleep(7 * 24 * 60 * 60)




async def execute_persistent_job(job: dict[str, Any]) -> dict[str, Any]:
    jtype = str(job.get("type") or "")
    payload = job.get("payload_json") or job.get("payload") or {}
    if isinstance(payload, str):
        try: payload = json.loads(payload)
        except Exception: payload = {}
    if jtype == "matterhorn_import":
        return await runtime.importer.import_products(
            source=str(payload.get("source") or "matterhorn_xml"),
            feed_url=payload.get("feed_url"),
            mode=str(payload.get("mode") or "safe"),
            dry_run=bool(payload.get("dry_run", False)),
            preview_id=payload.get("preview_id"),
            confirm_full_without_preview=bool(payload.get("confirm_full_without_preview", False)),
        )
    if jtype == "trend_analysis":
        if runtime.omniroute is None:
            raise RuntimeError("OmniRoute not configured")
        return await TrendEngine(runtime.omniroute).run_weekly_trend_job()
    if jtype == "fulfillment":
        await _handle_fulfillment_job(payload if "order_id" in payload else job)
        return {"status": "dispatched_or_processed"}
    # generic no-op job
    await write_log("INFO", "job_executed", f"Executed job type {jtype}", {"job_id": job.get("id")})
    return {"status": "ok", "type": jtype}


async def persistent_job_worker() -> None:
    """Poll DB-backed jobs and execute them with status tracking."""
    while True:
        try:
            job = await job_claim_next(worker_id="default-worker")
            if not job:
                await asyncio.sleep(2)
                continue
            jid = str(job.get("id"))
            try:
                result = await execute_persistent_job(job)
                await job_complete(jid, result if isinstance(result, dict) else {"result": result})
                await publish_runtime_event("job.completed", {"job_id": jid, "type": job.get("type")}, source="job_worker", entity_id=jid)
            except Exception as exc:
                logger.exception("Persistent job failed: %s", jid)
                await job_fail(jid, str(exc), retry=True, delay_seconds=30)
                await publish_runtime_event("job.failed", {"job_id": jid, "type": job.get("type"), "error": str(exc)[:500]}, source="job_worker", entity_id=jid)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Persistent job worker loop error")
            await asyncio.sleep(2)


async def scheduler_loop() -> None:
    """Evaluate scheduler_config and enqueue due jobs."""
    from datetime import datetime, timezone, timedelta
    # seed defaults
    defaults = [
        ("matterhorn_import", 24*3600, {"mode": "safe"}),
        ("trend_analysis", 7*24*3600, {}),
    ]
    for jt, interval, payload in defaults:
        try:
            await scheduler_upsert(job_type=jt, enabled=False if jt=="matterhorn_import" else True, interval_seconds=interval, payload=payload)
        except Exception:
            pass
    while True:
        try:
            configs = await scheduler_list()
            now_dt = datetime.now(timezone.utc)
            for cfg in configs:
                enabled = cfg.get('enabled')
                if isinstance(enabled, str): enabled = enabled.lower() in ('1','true','yes')
                if not enabled:
                    continue
                job_type = str(cfg.get('job_type'))
                interval = int(cfg.get('interval_seconds') or 3600)
                last = cfg.get('last_run_at')
                due = True
                if last:
                    try:
                        last_dt = datetime.fromisoformat(str(last).replace('Z','+00:00'))
                        if last_dt.tzinfo is None: last_dt = last_dt.replace(tzinfo=timezone.utc)
                        due = (now_dt - last_dt).total_seconds() >= interval
                    except Exception:
                        due = True
                if due:
                    payload = cfg.get('payload_json') or {}
                    if isinstance(payload, str):
                        try: payload = json.loads(payload)
                        except Exception: payload = {}
                    await job_create(job_type=job_type, payload=payload, created_by="scheduler")
                    await scheduler_mark_run(job_type, status="enqueued")
                    await write_log("INFO", "scheduler_enqueued", f"Scheduled job enqueued: {job_type}", {"interval_seconds": interval})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler loop error")
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Initialize storage, services and workers; cancel them cleanly on shutdown."""
    await init_db()
    await configure_omniroute()
    runtime.worker_task = asyncio.create_task(fulfillment_worker(), name="fulfillment-worker")
    runtime.trend_task = asyncio.create_task(trend_scheduler(), name="trend-scheduler")
    runtime.persistent_job_task = asyncio.create_task(persistent_job_worker(), name="persistent-job-worker")
    runtime.scheduler_task = asyncio.create_task(scheduler_loop(), name="scheduler-loop")
    # Initialize Shopware services
    runtime.shopware_export_manager = ExportManager(
        connection_manager=ShopwareConnectionManager("", "", ""),
        mapping_engine=MappingEngine([]),
    )
    runtime.shopware_description_engine = DescriptionEngine()
    runtime.shopware_seo_center = SEOCenter()
    # Initialize Competitor Intelligence
    runtime.competitor_matching_engine = ProductMatchingEngine()
    runtime.competitor_pricing_engine = DynamicPricingEngine()
    # Initialize Google Merchant Center
    runtime.google_mappers = {}
    runtime.google_validators = {}
    runtime.google_export_engines = {}
    # Initialize OmniRoute Control Center
    runtime.omniroute_provider_manager = ProviderManager()
    runtime.omniroute_model_registry = ModelRegistry()
    runtime.omniroute_routing_engine = IntelligentRoutingEngine(runtime.omniroute_model_registry)
    runtime.omniroute_cost_calculator = CostCalculator()
    # Initialize Event Bus and Global Control Center
    try:
        runtime.event_bus = await get_event_bus()
    except Exception:
        logger.exception("Failed to initialize event bus")
        runtime.event_bus = None
    runtime.control_center = GlobalControlCenter(
        event_bus=runtime.event_bus,
        provider_manager=runtime.omniroute_provider_manager,
        model_registry=runtime.omniroute_model_registry,
        routing_engine=runtime.omniroute_routing_engine,
        export_managers=runtime.google_export_engines,
        shopware_connections=runtime.shopware_connections,
        competitor_engines={
            "matching": runtime.competitor_matching_engine,
            "pricing": runtime.competitor_pricing_engine,
        },
    )
    await write_log("INFO", "application_started", "Omni-Core middleware started.")
    try:
        yield
    finally:
        for task in (runtime.worker_task, runtime.trend_task, runtime.persistent_job_task, runtime.scheduler_task):
            if task is not None:
                task.cancel()
        tasks = [task for task in (runtime.worker_task, runtime.trend_task, runtime.persistent_job_task, runtime.scheduler_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await shutdown_event_bus()
        except Exception:
            logger.exception("Failed to shutdown event bus")
        runtime.event_bus = None
        await runtime.importer.close()
        await close_pg_pool()


app = FastAPI(
    title="Omni-Core ERP & BI Enterprise Middleware",
    version="2.1.0",
    description="Lean multi-channel e-commerce middleware with PostgreSQL and SQLite WAL fallback.",
    lifespan=lifespan,
)

_INTERNAL_API_KEY_HEADER = "X-Internal-API-Key"
_INTERNAL_API_EXCLUDED_PATHS = frozenset({"/healthz"})
_AUTH_EXCLUDED_PATHS = frozenset({"/login", "/api/auth/login", "/api/auth/csrf"})
_CSRF_EXEMPT_PATHS = frozenset({"/api/auth/login", "/api/auth/csrf"})


def _auth_secret() -> str:
    secret = os.getenv("SESSION_SECRET", "").strip()
    return secret if len(secret) >= 32 else ""


def _sign(data: str) -> str:
    secret = _auth_secret()
    if not secret:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters before issuing sessions.")
    return hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + pad).encode())


def _issue_session_token(*, principal_type: str, user_id: str = "", role: str = "admin", sid: str = "") -> str:
    payload = {
        "typ": principal_type,  # api_key | user
        "uid": user_id,
        "role": role,
        "sid": sid or str(uuid.uuid4()),
        "iat": int(time.time()),
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
        "nonce": secrets.token_hex(8),
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    return f"v1.{body}.{_sign(body)}"


def _decode_session_token(token: str) -> dict[str, Any] | None:
    if not token or not _auth_secret():
        return None
    # legacy format: <issued_ts>.<hmac>
    if token.count(".") == 1:
        issued, signature = token.split(".", 1)
        if issued.isdigit():
            signing_key = (os.getenv("INTERNAL_API_KEY", "").strip() or _auth_secret()).encode()
            expected = hmac.new(signing_key, issued.encode(), hashlib.sha256).hexdigest()
            if hmac.compare_digest(signature, expected) and int(time.time()) - int(issued) <= SESSION_TTL_SECONDS:
                return {"typ": "api_key", "uid": "internal-api-key", "role": "SUPERADMIN", "iat": int(issued), "exp": int(issued) + SESSION_TTL_SECONDS, "sid": "legacy"}
        return None
    if not token.startswith("v1."):
        return None
    try:
        _, body, sig = token.split(".", 2)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    try:
        payload = json.loads(_b64url_decode(body).decode())
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def _new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _cookie_secure(request: Request) -> bool:
    secure_setting = os.getenv("COOKIE_SECURE", "").strip().lower()
    if secure_setting:
        return secure_setting in {"1", "true", "yes", "on"}
    return request.url.scheme == "https" or os.getenv("OMNI_ENV", "").lower() == "production"


def _set_auth_cookies(request: Request, response: Response, session_token: str) -> str:
    csrf = _new_csrf_token()
    secure = _cookie_secure(request)
    response.set_cookie(SESSION_COOKIE_NAME, session_token, max_age=SESSION_TTL_SECONDS, httponly=True, samesite="lax", secure=secure, path="/")
    # readable by JS for header injection
    response.set_cookie(CSRF_COOKIE_NAME, csrf, max_age=SESSION_TTL_SECONDS, httponly=False, samesite="lax", secure=secure, path="/")
    return csrf


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


async def _resolve_principal(request: Request) -> dict[str, Any]:
    # 1) Internal API key header (programmatic, full access, CSRF not required)
    provided_api_key = request.headers.get(_INTERNAL_API_KEY_HEADER, "")
    expected_api_key = os.getenv("INTERNAL_API_KEY", "").strip()
    if provided_api_key and expected_api_key and hmac.compare_digest(provided_api_key, expected_api_key):
        return {"authenticated": True, "via": "api_key_header", "role": "SUPERADMIN", "permissions": ["*"], "user_id": "internal-api-key", "user": {"id": "internal-api-key", "email": "internal@local", "display_name": "Internal API Key", "role": "SUPERADMIN"}}

    # 2) Browser/session cookie
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    payload = _decode_session_token(token)
    if not payload:
        return {"authenticated": False}

    # legacy/internal session token => superadmin
    if payload.get("typ") == "api_key":
        return {"authenticated": True, "via": "session_internal", "role": "SUPERADMIN", "permissions": ["*"], "user_id": str(payload.get("uid") or "internal"), "session": payload, "user": {"id": "internal", "email": "internal@local", "display_name": "Internal Session", "role": "SUPERADMIN"}}

    # DB-backed user session
    sid = str(payload.get("sid") or "")
    sess = await iam_get_session(sid) if sid else None
    if not sess or bool(sess.get("revoked")):
        return {"authenticated": False}
    stored_token_hash = str(sess.get("session_token_hash") or "")
    presented_token_hash = hashlib.sha256(token.encode()).hexdigest()
    if not stored_token_hash or not hmac.compare_digest(stored_token_hash, presented_token_hash):
        return {"authenticated": False}
    expires_at = sess.get("expires_at")
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")) if isinstance(expires_at, str) else expires_at
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if exp_dt < datetime.now(timezone.utc):
                return {"authenticated": False}
        except (TypeError, ValueError):
            return {"authenticated": False}
    user_id = str(payload.get("uid") or sess.get("user_id") or "")
    user = await iam_get_user(user_id) if user_id else None
    if not user or not bool(user.get("is_active", True)):
        return {"authenticated": False}
    roles = await iam_get_user_roles(user_id)
    if bool(user.get("is_superadmin")) and "SUPERADMIN" not in roles:
        roles.append("SUPERADMIN")
    if not roles:
        roles = [str(payload.get("role") or "VIEWER")]
    perms: set[str] = set()
    for r in roles:
        for p in await iam_list_permissions(role_name=r):
            perms.add(str(p.get("permission")))
    if "SUPERADMIN" in roles:
        perms.add("*")
    return {
        "authenticated": True,
        "via": "session_user",
        "role": roles[0],
        "roles": roles,
        "permissions": sorted(perms),
        "user_id": user_id,
        "session": payload,
        "user": {"id": user.get("id"), "email": user.get("email"), "display_name": user.get("display_name") or user.get("username"), "roles": roles},
    }


def _required_permission(method: str, path: str) -> str | None:
    normalized_method = "GET" if method.upper() == "HEAD" else method.upper()
    candidates = [
        (len(pfx), perm)
        for rule_method, prefixes, perm in RBAC_RULES
        if rule_method == normalized_method
        for pfx in prefixes
        if path == (pfx.rstrip("/") or "/")
        or path.startswith((pfx.rstrip("/") or "/") + "/")
    ]
    if not candidates:
        return "system.unclassified"
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _has_permission(principal: dict[str, Any], permission: str | None) -> bool:
    if not permission:
        return False
    perms = set(principal.get("permissions") or [])
    if permission == "system.unclassified":
        return permission in perms
    if "*" in perms or principal.get("role") == "SUPERADMIN":
        return True
    return permission in perms


def _csrf_valid(request: Request) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE_NAME, "")
    header = request.headers.get("X-CSRF-Token") or request.headers.get("X-CSRFToken") or ""
    return bool(cookie) and bool(header) and secrets.compare_digest(cookie, header)


def _add_security_headers(response: Response, request: Request) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.url.scheme == "https" or os.getenv("OMNI_ENV", "").lower() == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    method = request.method.upper()
    request.state.principal = {"authenticated": False}

    if path in _INTERNAL_API_EXCLUDED_PATHS or path in _AUTH_EXCLUDED_PATHS:
        response = await call_next(request)
        # ensure csrf cookie exists for browser sessions on safe responses to pages
        if method == "GET" and not path.startswith("/api/") and request.cookies.get(SESSION_COOKIE_NAME) and not request.cookies.get(CSRF_COOKIE_NAME):
            response.set_cookie(CSRF_COOKIE_NAME, _new_csrf_token(), max_age=SESSION_TTL_SECONDS, httponly=False, samesite="lax", secure=_cookie_secure(request), path="/")
        return _add_security_headers(response, request)

    principal = await _resolve_principal(request)
    request.state.principal = principal
    if not principal.get("authenticated"):
        if path.startswith("/api/"):
            return _add_security_headers(JSONResponse(status_code=401, content={"detail": "Unauthorized"}), request)
        return _add_security_headers(RedirectResponse(url="/login", status_code=303), request)

    # CSRF: enforce for browser-session authenticated state-changing requests
    if method in STATE_CHANGING_METHODS and path not in _CSRF_EXEMPT_PATHS and principal.get("via") != "api_key_header":
        # allow if no browser cookie/session context? principal came from session cookie in our resolver except api_key_header
        if not _csrf_valid(request):
            return _add_security_headers(JSONResponse(status_code=403, content={"detail": "CSRF token missing or invalid"}), request)

    # RBAC
    required = _required_permission(method, path)
    if not _has_permission(principal, required):
        if path.startswith("/api/"):
            return _add_security_headers(JSONResponse(status_code=403, content={"detail": "Forbidden", "required_permission": required}), request)
        return _add_security_headers(JSONResponse(status_code=403, content={"detail": "Forbidden"}), request)

    response = await call_next(request)
    # attach csrf cookie for authenticated browser GET pages if missing
    if method == "GET" and not path.startswith("/api/") and request.cookies.get(SESSION_COOKIE_NAME) and not request.cookies.get(CSRF_COOKIE_NAME):
        response.set_cookie(CSRF_COOKIE_NAME, _new_csrf_token(), max_age=SESSION_TTL_SECONDS, httponly=False, samesite="lax", secure=_cookie_secure(request), path="/")
    return _add_security_headers(response, request)




def require_permission(permission: str):
    async def _dep(principal: dict[str, Any] = Depends(get_current_principal)) -> dict[str, Any]:
        if not _has_permission(principal, permission):
            raise HTTPException(status_code=403, detail="Forbidden", headers={"X-Required-Permission": permission})
        return principal
    return _dep


def get_current_principal(request: Request) -> dict[str, Any]:
    p = getattr(request.state, "principal", None)
    if not p or not p.get("authenticated"):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return p


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """Render the control-plane login page."""
    return templates.TemplateResponse(request, "login.html", {"request": request})


class LoginRequest(BaseModel):
    api_key: str | None = Field(default=None, max_length=4096)
    email: str | None = Field(default=None, max_length=320)
    username: str | None = Field(default=None, max_length=320)
    password: str | None = Field(default=None, max_length=4096)


async def _bootstrap_iam_if_possible() -> None:
    try:
        from services.iam_core import bootstrap_defaults
        await bootstrap_defaults()
        # optional initial superadmin
        admin_email = (os.getenv("ADMIN_EMAIL") or "").strip().lower()
        admin_password = os.getenv("ADMIN_PASSWORD") or ""
        if admin_email and admin_password:
            from services.iam_core import hash_password
            existing = await iam_get_user_by_email(admin_email)
            if not existing:
                user = await iam_create_user(email=admin_email, password_hash=hash_password(admin_password), display_name=os.getenv("ADMIN_DISPLAY_NAME") or "Administrator", username=admin_email, is_superadmin=True)
                await iam_assign_role(user.get("id"), "SUPERADMIN")
    except Exception:
        logger.exception("IAM bootstrap skipped/failed")


@app.post("/api/auth/login")
async def login(request: Request, payload: LoginRequest, response: Response) -> dict[str, Any]:
    """Authenticate via internal API key (compat) or IAM user credentials."""
    if not _auth_secret():
        raise HTTPException(status_code=503, detail="SESSION_SECRET must contain at least 32 characters.")
    await _bootstrap_iam_if_possible()
    expected_api_key = os.getenv("INTERNAL_API_KEY", "").strip()

    # 1) Legacy/internal API key login (backward compatible)
    if payload.api_key:
        if not expected_api_key or not hmac.compare_digest(payload.api_key, expected_api_key):
            raise HTTPException(status_code=401, detail="Invalid API key")
        token = _issue_session_token(principal_type="api_key", user_id="internal-api-key", role="SUPERADMIN")
        _set_auth_cookies(request, response, token)
        await write_log("INFO", "auth_login", "Internal API key session login")
        return {"status": "ok", "message": "Authenticated", "principal": {"type": "api_key", "role": "SUPERADMIN"}}

    # 2) User credential login
    identifier = (payload.email or payload.username or "").strip().lower()
    if not identifier or not payload.password:
        raise HTTPException(status_code=400, detail="email/username and password or api_key required")
    user = await iam_get_user_by_email(identifier)
    if not user:
        # try username lookup by scanning (small table) fallback
        for u in await iam_list_users(limit=500):
            if str(u.get("username") or "").lower() == identifier:
                user = await iam_get_user(str(u.get("id")))
                break
    if not user or not bool(user.get("is_active", True)):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    from services.iam_core import verify_password
    if not verify_password(payload.password, str(user.get("password_hash") or "")):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    roles = await iam_get_user_roles(str(user.get("id")))
    if bool(user.get("is_superadmin")) and "SUPERADMIN" not in roles:
        roles.append("SUPERADMIN")
    role = roles[0] if roles else "VIEWER"
    sid = str(uuid.uuid4())
    token = _issue_session_token(principal_type="user", user_id=str(user.get("id")), role=role, sid=sid)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc).replace(microsecond=0).timestamp() + SESSION_TTL_SECONDS
    expires_iso = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
    await iam_create_session(session_id=sid, user_id=str(user.get("id")), session_token_hash=token_hash, ip_address=str(request.client.host if request.client else ""), user_agent=str(request.headers.get("user-agent", ""))[:500], expires_at=expires_iso)
    _set_auth_cookies(request, response, token)
    await write_log("INFO", "auth_login", f"User login: {user.get('email')}", {"user_id": user.get("id"), "role": role})
    return {"status": "ok", "message": "Authenticated", "principal": {"type": "user", "user_id": user.get("id"), "email": user.get("email"), "role": role, "roles": roles}}


@app.post("/api/auth/logout")
async def logout_api(request: Request, response: Response) -> dict[str, str]:
    """Clear browser session and revoke DB session if present."""
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    payload = _decode_session_token(token) or {}
    sid = str(payload.get("sid") or "")
    if sid and sid != "legacy":
        await iam_revoke_session(sid)
    _clear_auth_cookies(response)
    return {"status": "ok"}


@app.get("/api/auth/me")
async def auth_me(principal: dict[str, Any] = Depends(get_current_principal)) -> dict[str, Any]:
    return {"authenticated": True, "principal": {k: v for k, v in principal.items() if k != "session"}}


@app.get("/api/auth/csrf")
async def auth_csrf(request: Request, response: Response) -> dict[str, str]:
    token = request.cookies.get(CSRF_COOKIE_NAME) or _new_csrf_token()
    response.set_cookie(CSRF_COOKIE_NAME, token, max_age=SESSION_TTL_SECONDS, httponly=False, samesite="lax", secure=_cookie_secure(request), path="/")
    return {"csrf_token": token}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Render the unified Omni-Core Control Center."""
    return templates.TemplateResponse(request, "control_center.html",
        {
            "request": request,
            "metrics": await count_dashboard_metrics(),
            "import_runs": await recent_import_runs(5),
            "logs": await recent_logs(10),
            "uptime": runtime.uptime_seconds,
            "queue_size": runtime.jobs.qsize(),
            "database_backend": "postgresql" if DATABASE_URL else "sqlite-wal",
        },
    )


@app.get("/admin/products", response_class=HTMLResponse)
async def products_page(request: Request) -> HTMLResponse:
    """Render product administration."""
    return templates.TemplateResponse(request, "products.html", {"request": request, "products": await list_products(100)})


@app.get("/admin/orders", response_class=HTMLResponse)
async def orders_page(request: Request) -> HTMLResponse:
    """Render order administration."""
    return templates.TemplateResponse(request, "orders.html", {"request": request, "orders": await list_orders(100)})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """Render settings with database-masked secrets."""
    return templates.TemplateResponse(request, "settings.html", {"request": request, "settings": await list_settings()})


@app.get("/healthz")
async def liveness_check() -> dict[str, str]:
    """Public liveness endpoint with no dependency or configuration details."""
    return {"status": "ok"}


@app.get("/api/health")
async def health_check() -> dict[str, Any]:
    """Authenticated readiness and dependency health endpoint."""
    try:
        await count_dashboard_metrics()
        database_healthy = True
    except Exception:
        logger.exception("Database health check failed")
        database_healthy = False

    if runtime.omniroute is not None:
        started = time.monotonic()
        runtime.omniroute_healthy = await runtime.omniroute.check_health()
        runtime.omniroute_latency_ms = int((time.monotonic() - started) * 1000)

    status = "healthy" if database_healthy else "degraded"
    return {
        "status": status,
        "database": "postgresql" if DATABASE_URL else "sqlite-wal",
        "database_healthy": database_healthy,
        "omniroute_healthy": runtime.omniroute_healthy,
        "omniroute_latency_ms": runtime.omniroute_latency_ms,
        "queue_size": runtime.jobs.qsize(),
        "uptime_seconds": runtime.uptime_seconds,
    }


@app.get("/api/stats")
async def stats() -> dict[str, Any]:
    """Return live dashboard counters."""
    return {
        **await count_dashboard_metrics(),
        "queue_size": runtime.jobs.qsize(),
        "uptime_seconds": runtime.uptime_seconds,
        "omniroute_healthy": runtime.omniroute_healthy,
        "omniroute_latency_ms": runtime.omniroute_latency_ms,
    }


@app.get("/api/settings")
async def get_settings() -> dict[str, str]:
    """Return configuration with all secret values masked."""
    return await list_settings()


@app.post("/api/settings")
async def save_setting(payload: SettingsUpdate) -> dict[str, str]:
    """Persist one validated setting and refresh the OmniRoute client if needed."""
    await set_setting(payload.key, payload.value, is_secret=is_secret_key(payload.key))
    if payload.key in {"omniroute_url", "omniroute_api_key", "omni_route_base_url", "omni_route_api_key"}:
        await configure_omniroute()
    await write_log("INFO", "setting_updated", f"Configuration updated: {payload.key}")
    return {"status": "success", "message": "Setting saved."}


@app.get("/api/products")
async def products(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    """Return products with bounded pagination."""
    return await list_products(limit, offset)


@app.post("/api/products", status_code=201)
async def create_product(payload: ProductCreate) -> dict[str, Any]:
    """Create or update a manually managed product idempotently (incl. extended fields)."""
    data = payload.model_dump(exclude_none=True)
    sq = data.get("stock_quantity") if data.get("stock_quantity") is not None else data.get("stock", 0)
    data["stock_quantity"] = int(sq or 0)
    data["stock"] = int(data.get("stock", data["stock_quantity"]))
    source_hash = hashlib.sha256(json.dumps({"manual": True, **data}, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
    data["source_hash"] = source_hash
    res = await upsert_product_aggregate(data, sync_rules={"mode":"full","preserve_manual_seo": False, "preserve_marketplace_mappings": False})
    await refresh_all_bundle_stocks()
    return {"status": "success", "sku": payload.sku, "result": res}


@app.get("/api/products/{sku}")
async def product(sku: str) -> dict[str, Any]:
    """Return one product or a 404 response."""
    item = await fetch_product(sku)
    if item is None:
        raise HTTPException(status_code=404, detail="Product not found.")
    return item

@app.put("/api/products/{sku}")
async def update_product(sku: str, payload: ProductCreate) -> dict[str, Any]:
    """Create or update a product by SKU (idempotent upsert, extended)."""
    if payload.sku != sku:
        raise HTTPException(status_code=422, detail="Path SKU must match the product SKU in the request body.")
    data = payload.model_dump(exclude_none=True)
    sq = data.get("stock_quantity") if data.get("stock_quantity") is not None else data.get("stock", 0)
    data["stock_quantity"] = int(sq or 0)
    data["stock"] = int(data.get("stock", data["stock_quantity"]))
    source_hash = hashlib.sha256(json.dumps({"manual": True, **data}, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
    data["source_hash"] = source_hash
    res = await upsert_product_aggregate(data, sync_rules={"mode":"full","preserve_manual_seo": False, "preserve_marketplace_mappings": False})
    await refresh_all_bundle_stocks()
    return {"status": "success", "sku": sku, "result": res}


@app.delete("/api/products/{sku}")
async def remove_product(sku: str) -> dict[str, str]:
    ok = await delete_product(sku)
    if not ok:
        raise HTTPException(status_code=404, detail="Product not found.")
    await refresh_all_bundle_stocks()
    return {"status": "deleted", "sku": sku}


class BundleCreate(BaseModel):
    bundle_sku: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=512)
    component_skus: list[str] = Field(default_factory=list)


@app.post("/api/bundles", status_code=201)
async def create_bundle(payload: BundleCreate) -> dict[str, Any]:
    await upsert_bundle(payload.bundle_sku, payload.title, payload.component_skus)
    await refresh_all_bundle_stocks()
    return {"status": "success", "bundle_sku": payload.bundle_sku}


@app.delete("/api/bundles/{bundle_sku}")
async def remove_bundle(bundle_sku: str) -> dict[str, str]:
    ok = await delete_bundle(bundle_sku)
    if not ok:
        raise HTTPException(status_code=404, detail="Bundle not found.")
    return {"status": "deleted", "bundle_sku": bundle_sku}



@app.get("/api/bundles")
async def bundles() -> list[dict[str, Any]]:
    """Return all bundle definitions."""
    return await list_bundles()


@app.post("/api/orders", status_code=202)
async def create_channel_order(payload: OrderCreate) -> dict[str, str]:
    """Persist an order and queue it only after payment is verified."""
    await persist_order(
        order_id=payload.order_id,
        sku=payload.sku,
        quantity=payload.quantity,
        marketplace=payload.marketplace,
        customer_reference=payload.customer_reference,
    )
    order = await fetch_order(payload.order_id)
    if not order:
        raise RuntimeError(f"Persisted order {payload.order_id} could not be reloaded.")
    payment_status = str(order.get("payment_status") or "").lower()
    if payment_status != "paid" or bool(order.get("fulfillment_locked")):
        await write_log(
            "INFO",
            "order_waiting_for_payment",
            f"Order {payload.order_id} was persisted but not queued for fulfillment.",
            {"payment_status": payment_status, "fulfillment_locked": bool(order.get("fulfillment_locked"))},
        )
        return {"status": "awaiting_payment", "order_id": payload.order_id}
    if runtime.jobs.full():
        raise HTTPException(status_code=503, detail="Fulfillment queue is full; retry later.")
    await runtime.jobs.put({"type": "fulfillment", **payload.model_dump()})
    await write_log("INFO", "order_received", f"Order {payload.order_id} queued.", {"marketplace": payload.marketplace})
    return {"status": "queued", "order_id": payload.order_id}


@app.get("/api/orders")
async def orders(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    """Return channel orders with bounded pagination."""
    return await list_orders(limit, offset)


@app.get("/api/finance/overview")
async def finance_dashboard_metrics() -> dict[str, Any]:
    """Return persisted finance source totals grouped by currency and type."""
    return await finance_overview()


@app.post("/api/finance/contribution-margin")
async def finance_contribution_margin(
    payload: FinanceContributionMarginRequest,
) -> dict[str, Any]:
    """Calculate scenario contribution from supplied amounts; this is not actualized BI."""
    return calculate_contribution_margin(**payload.model_dump())


@app.post("/api/finance/documents")
async def finance_document_import(
    payload: FinanceDocumentRequest,
    principal: dict[str, Any] = Depends(get_current_principal),
) -> dict[str, Any]:
    """Idempotently import an invoice, credit note, supplier invoice, or expense."""
    record = await finance_document_upsert(payload.model_dump())
    await write_log(
        "INFO",
        "finance_document_imported",
        "Finance document imported or updated.",
        {
            "provider": payload.provider,
            "external_id": payload.external_id,
            "document_type": payload.document_type,
            "actor_id": principal.get("user_id"),
        },
    )
    return record


@app.get("/api/finance/documents")
async def finance_documents(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    return await finance_document_list(limit, offset)


@app.post("/api/finance/transactions")
async def finance_transaction_import(
    payload: FinanceTransactionRequest,
    principal: dict[str, Any] = Depends(get_current_principal),
) -> dict[str, Any]:
    """Idempotently import a payment-provider or marketplace transaction."""
    record = await finance_transaction_upsert(payload.model_dump())
    await write_log(
        "INFO",
        "finance_transaction_imported",
        "Finance transaction imported or updated.",
        {
            "provider": payload.provider,
            "external_id": payload.external_id,
            "transaction_type": payload.transaction_type,
            "actor_id": principal.get("user_id"),
        },
    )
    return record


@app.get("/api/finance/transactions")
async def finance_transactions(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    return await finance_transaction_list(limit, offset)


@app.post("/api/finance/settlements")
async def finance_settlement_import(
    payload: FinanceSettlementRequest,
    principal: dict[str, Any] = Depends(get_current_principal),
) -> dict[str, Any]:
    """Idempotently import a provider settlement without inferring accounting entries."""
    record = await finance_settlement_upsert(payload.model_dump())
    await write_log(
        "INFO",
        "finance_settlement_imported",
        "Finance settlement imported or updated.",
        {
            "provider": payload.provider,
            "external_id": payload.external_id,
            "actor_id": principal.get("user_id"),
        },
    )
    return record


@app.get("/api/finance/settlements")
async def finance_settlements(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    return await finance_settlement_list(limit, offset)


@app.post("/api/finance/reconcile")
async def finance_reconcile_payment(
    payload: FinanceReconcileRequest,
    principal: dict[str, Any] = Depends(get_current_principal),
) -> dict[str, Any]:
    """Match one payment to invoice records and persist the outcome for review."""
    transaction = await finance_transaction_get(payload.transaction_id)
    if transaction is None:
        raise HTTPException(status_code=404, detail="Finance transaction not found.")
    if transaction.get("transaction_type") != "payment":
        raise HTTPException(status_code=422, detail="Only payment transactions can be reconciled to receivable documents.")

    candidates = await finance_document_candidates(
        currency=str(transaction["currency"]),
        order_reference=str(transaction.get("order_reference") or ""),
        gross_amount=transaction["amount"],
    )
    if len(candidates) > 500:
        result = {
            "status": "ambiguous",
            "target_type": "",
            "target_id": "",
            "currency": str(transaction["currency"]),
            "matched_amount": "0",
            "candidate_ids": [str(candidate["id"]) for candidate in candidates],
        }
    else:
        result = reconcile_payment_to_documents(transaction, candidates)
    persisted = await finance_reconciliation_create(
        {
            "source_type": "transaction",
            "source_id": payload.transaction_id,
            "target_type": result["target_type"],
            "target_id": result["target_id"],
            "status": result["status"],
            "currency": result["currency"],
            "matched_amount": result["matched_amount"],
            "details_json": {"candidate_ids": result["candidate_ids"]},
        }
    )
    await write_log(
        "INFO" if result["status"] == "matched" else "WARNING",
        "finance_reconciliation_created",
        f"Payment reconciliation result: {result['status']}.",
        {
            "transaction_id": payload.transaction_id,
            "reconciliation_id": persisted["id"],
            "status": result["status"],
            "actor_id": principal.get("user_id"),
        },
    )
    return persisted


@app.get("/api/finance/reconciliations")
async def finance_reconciliations(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    return await finance_reconciliation_list(limit, offset)


@app.post("/api/import/matterhorn")
async def import_matterhorn(payload: MatterhornImportRequest | None = None) -> dict[str, Any]:
    """Run Matterhorn import (safe/partial/full/dry_run/stock_only/ek_only) or enqueue as job."""
    req = payload or MatterhornImportRequest()
    mode = (req.mode or "safe").lower()
    if req.async_job:
        job = await job_create(job_type="matterhorn_import", payload={"feed_url": req.feed_url, "mode": mode, "dry_run": req.dry_run or mode=="dry_run", "preview_id": req.preview_id, "confirm_full_without_preview": req.confirm_full_without_preview, "sync_rules": req.sync_rules, "filename": req.filename, "content": req.content}, created_by="api")
        return {"status": "queued", "job_id": job.get("id"), "mode": mode}
    result = await runtime.importer.import_products(feed_url=req.feed_url, mode=mode, dry_run=req.dry_run or mode=="dry_run", preview_id=req.preview_id, confirm_full_without_preview=req.confirm_full_without_preview, sync_rules=req.sync_rules, filename=req.filename, content=req.content)
    if result.get("status") == "blocked":
        raise HTTPException(status_code=409, detail=result)
    return result




@app.get("/api/categories")
async def api_categories(limit: int = Query(default=500, ge=1, le=2000)):
    from database import list_categories
    return {"items": await list_categories(limit=limit)}

@app.get("/api/import-runs")
async def import_runs(limit: int = Query(default=10, ge=1, le=50)) -> list[dict[str, Any]]:
    """Return recent import runs."""
    return await recent_import_runs(limit)


@app.post("/api/pricing/calculate")
async def calculate_price(payload: PricingRequest) -> dict[str, Any]:
    """Calculate a marketplace-ready sale price."""
    return runtime.pricing.calculate_price(payload.supplier_price)


@app.post("/api/ai/trend-analysis")
async def run_trend_analysis() -> dict[str, Any]:
    """Manually run the weekly AI trend process."""
    if runtime.omniroute is None:
        raise HTTPException(status_code=503, detail="OmniRoute is not configured.")
    return await TrendEngine(runtime.omniroute).run_weekly_trend_job()


@app.get("/api/logs")
async def logs(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
    """Return recent structured system logs."""
    return await recent_logs(limit)


@app.get("/api/export/shopify.csv")
async def export_shopify_csv() -> StreamingResponse:
    """Generate a Shopify-compatible CSV export."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Handle", "Title", "Body (HTML)", "Vendor", "Type", "Tags", "Published",
        "Option1 Name", "Option1 Value", "Variant Price", "Variant SKU", "Variant Inventory Qty",
    ])
    for item in await list_products(500):
        writer.writerow([
            item["sku"], item["ai_title"] or item["title"], item["ai_description_html"] or item["raw_description"],
            "Matterhorn", item["category"], ",".join(item["ai_tags_json"]) if isinstance(item["ai_tags_json"], list) else "",
            "TRUE" if item["status"] == "active" else "FALSE", "Title", "Default Title",
            f"{float(item['supplier_price']):.2f}", item["sku"], item["stock"],
        ])
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=shopify-products.csv"},
    )


@app.get("/api/export/google-shopping.xml")
async def export_google_shopping_xml() -> Response:
    """Generate a valid Google Shopping XML feed without string-concatenation injection."""
    rss = ET.Element("rss", {"version": "2.0", "xmlns:g": "http://base.google.com/ns/1.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Omni-Core Products"
    for item in await list_products(500):
        product_xml = ET.SubElement(channel, "item")
        ET.SubElement(product_xml, "{http://base.google.com/ns/1.0}id").text = str(item["sku"])
        ET.SubElement(product_xml, "title").text = str(item["ai_title"] or item["title"])
        ET.SubElement(product_xml, "{http://base.google.com/ns/1.0}product_type").text = str(item["category"])
        ET.SubElement(product_xml, "{http://base.google.com/ns/1.0}price").text = f"{float(item['supplier_price']):.2f} EUR"
        ET.SubElement(product_xml, "{http://base.google.com/ns/1.0}availability").text = "in stock" if item["stock"] > 0 else "out of stock"
        ET.SubElement(product_xml, "{http://base.google.com/ns/1.0}condition").text = "new"
    return Response(content=ET.tostring(rss, encoding="unicode", xml_declaration=False), media_type="application/xml")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, loop="uvloop")


# ── Persistent Jobs & Scheduler APIs ──────────────────────────────────────

@app.get("/api/jobs")
async def api_jobs(status: str | None = Query(default=None), type: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=500)):
    items = await job_list(limit=limit, status=status, job_type=type)
    # normalize json fields for UI
    out=[]
    for j in items:
        d=dict(j)
        for k in ("payload_json","result_json"):
            if isinstance(d.get(k), str):
                try: d[k]=json.loads(d[k])
                except Exception: pass
        out.append(d)
    return {"items": out, "count": len(out)}

@app.post("/api/jobs", status_code=201)
async def api_job_create(payload: JobCreateRequest):
    job = await job_create(job_type=payload.type, payload=payload.payload, run_at=payload.run_at, max_attempts=payload.max_attempts, created_by="api")
    await publish_runtime_event("job.created", {"job_id": job.get("id"), "type": payload.type}, source="api", entity_id=str(job.get("id")))
    return job

@app.get("/api/jobs/{job_id}")
async def api_job_get(job_id: str):
    job = await job_get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    d=dict(job)
    for k in ("payload_json","result_json"):
        if isinstance(d.get(k), str):
            try: d[k]=json.loads(d[k])
            except Exception: pass
    return d

@app.post("/api/jobs/{job_id}/cancel")
async def api_job_cancel(job_id: str):
    ok = await job_cancel(job_id)
    if not ok: raise HTTPException(status_code=404, detail="Job not found or not cancellable")
    return {"status": "ok", "job_id": job_id, "action": "cancelled"}

@app.post("/api/jobs/{job_id}/retry")
async def api_job_retry(job_id: str):
    ok = await job_retry(job_id)
    if not ok: raise HTTPException(status_code=404, detail="Job not found")
    return {"status": "ok", "job_id": job_id, "action": "retry"}

@app.get("/api/scheduler/config")
async def api_scheduler_config():
    items = await scheduler_list()
    out=[]
    for c in items:
        d=dict(c)
        if isinstance(d.get('payload_json'), str):
            try: d['payload_json']=json.loads(d['payload_json'])
            except Exception: pass
        out.append(d)
    return {"items": out, "count": len(out)}

@app.post("/api/scheduler/config")
async def api_scheduler_upsert(payload: SchedulerConfigRequest):
    cfg = await scheduler_upsert(job_type=payload.job_type, enabled=payload.enabled, interval_seconds=payload.interval_seconds, payload=payload.payload)
    await write_log("INFO", "scheduler_config_updated", f"Scheduler config updated: {payload.job_type}", {"enabled": payload.enabled, "interval_seconds": payload.interval_seconds})
    return cfg

# ── Matterhorn Connector APIs (config/feeds/mappings/preview/history) ────

@app.get("/api/matterhorn/config")
async def api_matterhorn_config_get():
    cfg = await matterhorn_get_config()
    # include effective settings hints
    cfg.setdefault("feed_url", {"value": await get_setting("matterhorn_feed_url") or ""})
    cfg.setdefault("buffer_percent", {"value": 10})
    return {"config": cfg}

@app.put("/api/matterhorn/config")
async def api_matterhorn_config_put(payload: MatterhornConfigPayload):
    for k,v in (payload.config or {}).items():
        if not isinstance(v, dict): v = {"value": v}
        await matterhorn_set_config(str(k), v)
        if k == "feed_url":
            val = v.get("value") if isinstance(v, dict) else v
            if val: await set_setting("matterhorn_feed_url", str(val))
    return {"status": "ok", "config": await matterhorn_get_config()}

@app.get("/api/matterhorn/status")
async def api_matterhorn_status():
    feeds = await matterhorn_list_feeds()
    runs = await recent_import_runs(10)
    previews = await matterhorn_list_previews(5)
    configured = bool(feeds) or bool(await get_setting("matterhorn_feed_url"))
    return {"configured": configured, "feeds_count": len(feeds), "recent_runs": runs, "recent_previews": previews, "buffer_percent": 10}

@app.get("/api/matterhorn/feeds")
async def api_matterhorn_feeds():
    return {"items": await matterhorn_list_feeds()}

@app.post("/api/matterhorn/feeds")
async def api_matterhorn_feed_upsert(payload: MatterhornFeedPayload):
    return await matterhorn_upsert_feed(payload.model_dump())

@app.delete("/api/matterhorn/feeds/{feed_id}")
async def api_matterhorn_feed_delete(feed_id: str):
    ok = await matterhorn_delete_feed(feed_id)
    if not ok: raise HTTPException(status_code=404, detail="Feed not found")
    return {"status": "deleted", "id": feed_id}

@app.get("/api/matterhorn/mappings")
async def api_matterhorn_mappings():
    return {"items": await matterhorn_list_mappings()}

@app.post("/api/matterhorn/mappings")
async def api_matterhorn_mapping_upsert(payload: MatterhornMappingPayload):
    return await matterhorn_upsert_mapping(payload.model_dump())

@app.delete("/api/matterhorn/mappings/{mapping_id}")
async def api_matterhorn_mapping_delete(mapping_id: str):
    ok = await matterhorn_delete_mapping(mapping_id)
    if not ok: raise HTTPException(status_code=404, detail="Mapping not found")
    return {"status": "deleted", "id": mapping_id}

@app.get("/api/import/matterhorn/preview")
async def api_matterhorn_preview(feed_url: str | None = Query(default=None), save: int = Query(default=1), limit: int = Query(default=50, ge=1, le=200), format: str | None = Query(default=None)):
    return await runtime.importer.preview_products(feed_url=feed_url, save=bool(save), limit=limit, format_hint=format)


@app.post("/api/import/matterhorn/preview-upload")
async def api_matterhorn_preview_upload(request: Request, save: int = Query(default=1), limit: int = Query(default=50, ge=1, le=200)):
    ct = (request.headers.get("content-type") or "").lower()
    filename = None
    content: str | bytes | None = None
    if "multipart/form-data" in ct:
        form = await request.form()
        upload = form.get("file") or form.get("upload")
        filename = getattr(upload, "filename", None) or str(form.get("filename") or "upload")
        if upload is None:
            raise HTTPException(status_code=422, detail="Datei-Upload fehlt (Feld: file).")
        content = await upload.read()
    else:
        try:
            body = await request.json()
        except Exception:
            body = {}
        filename = str(body.get("filename") or "upload")
        content = body.get("content")
        if content is None:
            raise HTTPException(status_code=422, detail="Upload-Inhalt fehlt (content).")
    return await runtime.importer.preview_products(save=bool(save), limit=limit, content=content, filename=filename)


@app.post("/api/import/matterhorn/upload")
async def api_matterhorn_import_upload(request: Request):
    ct = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in ct:
        form = await request.form()
        upload = form.get("file") or form.get("upload")
        filename = getattr(upload, "filename", None) or str(form.get("filename") or "upload")
        if upload is None:
            raise HTTPException(status_code=422, detail="Datei-Upload fehlt (Feld: file).")
        content = await upload.read()
        mode = str(form.get("mode") or "safe").lower()
        dry_run = str(form.get("dry_run") or "").lower() in {"1","true","yes"} or mode=="dry_run"
        sync_rules_raw = form.get("sync_rules")
        try:
            sync_rules = json.loads(sync_rules_raw) if isinstance(sync_rules_raw,str) and sync_rules_raw else {}
        except Exception:
            sync_rules = {}
    else:
        try:
            body = await request.json()
        except Exception:
            body = {}
        filename = str(body.get("filename") or "upload")
        content = body.get("content")
        mode = str(body.get("mode") or "safe").lower()
        dry_run = bool(body.get("dry_run")) or mode=="dry_run"
        sync_rules = body.get("sync_rules") or {}
        if content is None:
            raise HTTPException(status_code=422, detail="Upload-Inhalt fehlt (content).")
    result = await runtime.importer.import_products(mode=mode, dry_run=dry_run, content=content, filename=filename, sync_rules=sync_rules, confirm_full_without_preview=True)
    if result.get("status") == "blocked":
        raise HTTPException(status_code=409, detail=result)
    return result


@app.get("/api/matterhorn/previews")
async def api_matterhorn_previews(limit: int = Query(default=50, ge=1, le=200)):
    return {"items": await matterhorn_list_previews(limit=limit)}

@app.get("/api/matterhorn/previews/{preview_id}")
async def api_matterhorn_preview_get(preview_id: str):
    item = await matterhorn_get_preview(preview_id)
    if not item: raise HTTPException(status_code=404, detail="Preview not found")
    return item

@app.get("/api/matterhorn/history")
async def api_matterhorn_history(limit: int = Query(default=50, ge=1, le=200)):
    return {"runs": await recent_import_runs(limit), "previews": await matterhorn_list_previews(min(limit,50))}




# ── Audit / Analytics / News / Knowledge / Bundles Intelligence APIs ─────

@app.get("/api/audit-log")
async def api_audit_log(limit: int = Query(default=200, ge=1, le=1000)):
    return {"items": await audit_list(limit=limit), "count": min(limit, 1000)}

@app.get("/api/analytics")
async def api_analytics():
    return await analytics_overview()


# ── Scene Intelligence APIs ───────────────────────────────────────────────

@app.get("/api/scene-intelligence/overview")
async def api_scene_intelligence_overview(limit: int = Query(default=200, ge=1, le=1000), principal: dict[str, Any] = Depends(get_current_principal)):
    from services.scene_intelligence import overview
    return await overview(limit=limit)

@app.get("/api/scene-intelligence/pulse")
async def api_scene_intelligence_pulse(limit: int = Query(default=200, ge=1, le=1000), principal: dict[str, Any] = Depends(get_current_principal)):
    from services.scene_intelligence import overview
    data = await overview(limit=limit)
    return {"status": "ok", "generated_at": data.get("generated_at"), "pulse": data.get("pulse"), "data_basis": data.get("data_basis"), "notes": data.get("notes", [])}


@app.get("/api/bundles/intelligence")
async def api_bundles_intelligence():
    bundles_data = await list_bundles()
    products = await list_products(500)
    active_products = [p for p in products if not p.get('is_bundle') and str(p.get('status'))=='active' and int(p.get('stock') or 0)>0]
    by_cat: dict[str, list] = {}
    for p in active_products:
        by_cat.setdefault(str(p.get('category') or 'uncategorized'), []).append(p)
    suggestions=[]
    for cat, items in by_cat.items():
        if len(items) >= 2:
            suggestions.append({"category": cat, "candidate_skus": [i.get('sku') for i in items[:4]], "reason": "same_category_stock_available"})
    return {"bundles": bundles_data, "suggestions": suggestions[:20], "summary": {"bundle_count": len(bundles_data), "suggestion_count": len(suggestions[:20])}}

class NewsSourcePayload(BaseModel):
    id: str | None = None
    name: str
    url: str = ""
    category: str = "general"
    enabled: bool = True
    config_json: dict[str, Any] = Field(default_factory=dict)

class NewsItemPayload(BaseModel):
    id: str | None = None
    source_id: str = ""
    title: str
    url: str = ""
    summary: str = ""
    category: str = "general"
    published_at: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)

class KnowledgePayload(BaseModel):
    id: str | None = None
    title: str
    content: str = ""
    category: str = "general"
    tags: list[str] = Field(default_factory=list)
    source: str = ""
    metadata_json: dict[str, Any] = Field(default_factory=dict)

@app.get("/api/news/sources")
async def api_news_sources():
    return {"items": await news_list_sources()}

@app.post("/api/news/sources")
async def api_news_source_upsert(payload: NewsSourcePayload):
    return await news_upsert_source(payload.model_dump())

@app.get("/api/news/items")
async def api_news_items(limit: int = Query(default=100, ge=1, le=500), category: str | None = Query(default=None)):
    return {"items": await news_list_items(limit=limit, category=category)}

@app.post("/api/news/items")
async def api_news_item_create(payload: NewsItemPayload):
    return await news_add_item(payload.model_dump())

@app.get("/api/knowledge")
async def api_knowledge_list(limit: int = Query(default=100, ge=1, le=500)):
    return {"items": await knowledge_list(limit=limit)}

@app.post("/api/knowledge")
async def api_knowledge_upsert(payload: KnowledgePayload):
    d = payload.model_dump(); d['tags_json'] = d.pop('tags', [])
    return await knowledge_upsert(d)



# ---------------------------------------------------------------------------
# Sales API
# ---------------------------------------------------------------------------

@app.get("/api/sales/overview")
async def sales_overview():
    """Get sales overview with velocity and metrics."""
    
    return {
        "status": "ok",
        "message": "Sales overview endpoint - requires database integration",
    }

@app.get("/api/sales/product-scores")
async def product_scores():
    """Get product ABC classification and scores."""
    
    return {
        "status": "ok",
        "message": "Product scores endpoint - requires database integration",
    }

@app.get("/api/sales/pricing-recommendations")
async def pricing_recommendations():
    """Get smart pricing recommendations."""
    from services.sales_automation import calculate_smart_price
    
    result = calculate_smart_price(retail_price=100.0, cost_price=60.0)
    return {
        "status": "ok",
        "recommendation": result,
    }

# ---------------------------------------------------------------------------
# Marketing API
# ---------------------------------------------------------------------------

@app.get("/api/marketing/campaigns")
async def list_campaigns():
    """List marketing campaigns."""
    from database import get_connection
    
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT id, name, channel, campaign_type, status, created_at FROM marketing_campaigns"
        )
        rows = await cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        result = [dict(zip(columns, row)) for row in rows]
    
    return {
        "status": "ok",
        "campaigns": result,
    }

@app.post("/api/marketing/campaigns/generate")
async def generate_campaign():
    """Generate a new marketing campaign."""
    return {
        "status": "ok",
        "message": "Campaign generation endpoint - requires OmniRoute gateway",
    }

@app.post("/api/marketing/products/{product_sku}/generate-content")
async def generate_product_content(product_sku: str):
    """Generate marketing content for a product."""
    return {
        "status": "ok",
        "product_sku": product_sku,
        "message": "Product content generation endpoint",
    }

@app.get("/api/marketing/products/{product_sku}/content")
async def get_product_content(product_sku: str):
    """Get stored marketing content for a product."""
    from database import get_connection
    
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT content_type, content_json, channel FROM channel_content WHERE product_sku = ?",
            (product_sku,)
        )
        rows = await cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        result = [dict(zip(columns, row)) for row in rows]
    
    return {
        "status": "ok",
        "content": result,
    }

# ---------------------------------------------------------------------------
# Export API
# ---------------------------------------------------------------------------

@app.get("/api/export/channels")
async def list_export_channels():
    """List available export channels."""
    return {
        "status": "ok",
        "channels": ["shopify", "amazon", "google_shopping", "ebay", "zalando"],
    }

@app.get("/api/export/{channel}")
async def export_status(channel: str):
    """Get export status for a channel."""
    from services.channel_adapters import get_shopify_adapter, get_amazon_adapter, get_google_shopping_adapter, get_ebay_adapter, get_zalando_adapter
    
    adapter_map = {
        "shopify": get_shopify_adapter(),
        "amazon": get_amazon_adapter(),
        "google_shopping": get_google_shopping_adapter(),
        "ebay": get_ebay_adapter(),
        "zalando": get_zalando_adapter(),
    }
    
    adapter = adapter_map.get(channel)
    if adapter:
        status = await adapter.get_adapter_status()
        return {
            "status": "ok",
            "channel": channel,
            "adapter_status": status,
        }
    
    raise HTTPException(status_code=404, detail=f"Channel {channel} not supported")

@app.post("/api/export/{channel}/generate")
async def generate_export(channel: str):
    """Generate export for a channel."""
    from services.channel_adapters import get_shopify_adapter, get_amazon_adapter, get_google_shopping_adapter, get_ebay_adapter, get_zalando_adapter
    
    adapter_map = {
        "shopify": get_shopify_adapter(),
        "amazon": get_amazon_adapter(),
        "google_shopping": get_google_shopping_adapter(),
        "ebay": get_ebay_adapter(),
        "zalando": get_zalando_adapter(),
    }
    
    adapter = adapter_map.get(channel)
    if adapter:
        return {
            "status": "ok",
            "channel": channel,
            "message": f"Export generation for {channel} started",
        }
    
    raise HTTPException(status_code=404, detail=f"Channel {channel} not supported")

@app.get("/api/export/{channel}/status")
async def export_channel_status(channel: str):
    """Get export status for a channel."""
    from services.channel_adapters import get_shopify_adapter, get_amazon_adapter, get_google_shopping_adapter, get_ebay_adapter, get_zalando_adapter
    
    adapter_map = {
        "shopify": get_shopify_adapter(),
        "amazon": get_amazon_adapter(),
        "google_shopping": get_google_shopping_adapter(),
        "ebay": get_ebay_adapter(),
        "zalando": get_zalando_adapter(),
    }
    
    adapter = adapter_map.get(channel)
    if adapter:
        adapter_status = await adapter.get_adapter_status()
        return {
            "status": "ok",
            "channel": channel,
            "adapter_status": adapter_status,
        }
    
    raise HTTPException(status_code=404, detail=f"Channel {channel} not supported")

@app.get("/api/export/{channel}/download")
async def download_export(channel: str):
    """Download export file for a channel."""
    return {
        "status": "ok",
        "channel": channel,
        "message": "Download endpoint - file generation in progress",
    }

# ---------------------------------------------------------------------------
# Health / Integrations API
# ---------------------------------------------------------------------------

@app.get("/api/health/integrations")
async def integrations_health() -> dict[str, Any]:
    """Check integration health across configured subsystems."""
    try:
        await count_dashboard_metrics()
        db_ok = True
    except Exception:
        logger.exception("Database integration check failed")
        db_ok = False

    integrations: dict[str, Any] = {
        "database": {"connected": db_ok},
        "omniroute": {"healthy": runtime.omniroute_healthy, "latency_ms": runtime.omniroute_latency_ms},
        "shopware": {"configured_connections": len(runtime.shopware_connections)},
        "google_merchant": {"configured_accounts": len(runtime.google_merchant_clients)},
        "event_bus": runtime.event_bus.get_stats() if getattr(runtime, "event_bus", None) else {"running": False},
    }
    overall_ok = db_ok
    return {"status": "ok" if overall_ok else "degraded", "integrations": integrations}

# ---------------------------------------------------------------------------
# Shopware Integration API
# ---------------------------------------------------------------------------

class ShopwareConnectionCreate(BaseModel):
    shop_name: str
    shop_url: str
    shopware_version: str = "6.0"
    api_client_id: str
    api_client_secret: str
    sales_channel_id: str = ""
    default_language: str = "de-DE"
    default_currency: str = "EUR"
    default_tax_rate: float = 19.0
    default_supplier: str = ""

class ShopwareMappingCreate(BaseModel):
    connection_id: str
    matterhorn_field: str
    shopware_field: str
    mapping_type: str = MAPPING_DIRECT
    mapping_config: dict = {}
    priority: int = 0

class ShopwareExportCreate(BaseModel):
    connection_id: str
    product_sku: str
    export_mode: str = "publish"
    payload: dict = {}

@app.post("/api/shopware/connections")
async def create_shopware_connection(payload: ShopwareConnectionCreate):
    """Create a new Shopware connection."""
    from uuid import uuid4
    conn_id = str(uuid4())
    cm = ShopwareConnectionManager(
        shop_url=payload.shop_url,
        client_id=payload.api_client_id,
        client_secret=payload.api_client_secret,
        api_version=payload.shopware_version,
    )
    runtime.shopware_connections[conn_id] = cm
    runtime.shopware_mapping_engines[conn_id] = MappingEngine(
        get_default_mappings(conn_id)
    )
    await write_log("INFO", "shopware_connection_created", f"Shopware connection {payload.shop_name} created.", {"connection_id": conn_id})
    return {
        "status": "ok",
        "connection_id": conn_id,
        "shop_name": payload.shop_name,
    }

@app.get("/api/shopware/connections")
async def list_shopware_connections():
    """List all Shopware connections."""
    connections = []
    for conn_id, cm in runtime.shopware_connections.items():
        connections.append({
            "id": conn_id,
            "shop_url": cm.shop_url,
            "api_version": cm.api_version,
            "is_authenticated": cm.is_authenticated,
            "request_count": cm.request_count,
            "error_count": cm.error_count,
        })
    return {"status": "ok", "connections": connections}

@app.post("/api/shopware/connections/{conn_id}/test")
async def test_shopware_connection(conn_id: str):
    """Test a Shopware connection."""
    cm = runtime.shopware_connections.get(conn_id)
    if not cm:
        raise HTTPException(status_code=404, detail="Connection not found")
    health = await cm.check_health()
    return {"status": "ok", "connection_id": conn_id, "health": health}

@app.post("/api/shopware/connections/{conn_id}/mappings")
async def create_shopware_mapping(conn_id: str, payload: ShopwareMappingCreate):
    """Create a field mapping for a Shopware connection."""
    from uuid import uuid4
    mapping = FieldMapping(
        mapping_id=str(uuid4()),
        connection_id=conn_id,
        matterhorn_field=payload.matterhorn_field,
        shopware_field=payload.shopware_field,
        mapping_type=payload.mapping_type,
        mapping_config=payload.mapping_config,
        priority=payload.priority,
    )
    engine = runtime.shopware_mapping_engines.get(conn_id)
    if engine:
        engine.mappings.append(mapping)
    await write_log("INFO", "shopware_mapping_created", f"Mapping {payload.matterhorn_field} -> {payload.shopware_field} created.", {"connection_id": conn_id})
    return {"status": "ok", "mapping_id": mapping.mapping_id}

@app.get("/api/shopware/connections/{conn_id}/mappings")
async def list_shopware_mappings(conn_id: str):
    """List field mappings for a Shopware connection."""
    engine = runtime.shopware_mapping_engines.get(conn_id)
    if not engine:
        raise HTTPException(status_code=404, detail="Connection not found")
    return {
        "status": "ok",
        "connection_id": conn_id,
        "mappings": [m.to_dict() for m in engine.mappings],
        "summary": engine.get_mapping_summary(),
    }

@app.post("/api/shopware/export")
async def add_shopware_export(payload: ShopwareExportCreate):
    """Add an item to the Shopware export queue."""
    if not runtime.shopware_export_manager:
        raise HTTPException(status_code=503, detail="Export manager not initialized")
    queue_id = runtime.shopware_export_manager.add_to_queue(
        connection_id=payload.connection_id,
        product_sku=payload.product_sku,
        export_mode=payload.export_mode,
        payload=payload.payload,
    )
    return {"status": "ok", "queue_id": queue_id}

@app.get("/api/shopware/export/queue")
async def list_shopware_export_queue(status: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    """List Shopware export queue items."""
    if not runtime.shopware_export_manager:
        raise HTTPException(status_code=503, detail="Export manager not initialized")
    items = runtime.shopware_export_manager.list_queue(status=status, limit=limit)
    return {"status": "ok", "queue": [i.to_dict() for i in items]}

@app.get("/api/shopware/export/stats")
async def shopware_export_stats():
    """Get Shopware export queue statistics."""
    if not runtime.shopware_export_manager:
        raise HTTPException(status_code=503, detail="Export manager not initialized")
    return {"status": "ok", "stats": runtime.shopware_export_manager.get_queue_stats()}

@app.post("/api/shopware/export/process")
async def process_shopware_export(max_items: int = 10):
    """Process pending Shopware export queue items."""
    if not runtime.shopware_export_manager:
        raise HTTPException(status_code=503, detail="Export manager not initialized")
    result = await runtime.shopware_export_manager.process_queue(max_items=max_items)
    return {"status": "ok", "result": result}

# ---------------------------------------------------------------------------
# Shopware Description & SEO API
# ---------------------------------------------------------------------------

class ShopwareDescriptionRequest(BaseModel):
    product_sku: str
    template: str = "premium"
    ai_enhance: bool = False

@app.post("/api/shopware/description/generate")
async def generate_shopware_description(payload: ShopwareDescriptionRequest):
    """Generate a Shopware product description."""
    product = await fetch_product(payload.product_sku)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    from services.canonical_product_mapping import CanonicalProduct
    cp = CanonicalProduct.from_matterhorn("matterhorn", product)
    
    engine = runtime.shopware_description_engine
    if not engine:
        raise HTTPException(status_code=503, detail="Description engine not initialized")
    
    result = engine.generate_description(cp, template_name=payload.template)
    
    if payload.ai_enhance and runtime.omniroute:
        result = engine.enhance_with_ai(result, cp)
    
    return {"status": "ok", "description": result}

@app.post("/api/shopware/seo/generate")
async def generate_shopware_seo(payload: ShopwareDescriptionRequest):
    """Generate SEO data for a Shopware product."""
    product = await fetch_product(payload.product_sku)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    from services.canonical_product_mapping import CanonicalProduct
    cp = CanonicalProduct.from_matterhorn("matterhorn", product)
    
    seo_center = runtime.shopware_seo_center
    if not seo_center:
        raise HTTPException(status_code=503, detail="SEO center not initialized")
    
    result = seo_center.generate_seo_data(cp)
    return {"status": "ok", "seo": result}

# ---------------------------------------------------------------------------
# Competitor Intelligence API
# ---------------------------------------------------------------------------

class CompetitorSearchRequest(BaseModel):
    query: str
    provider: str = "example"
    limit: int = 10

class CompetitorMatchRequest(BaseModel):
    product_sku: str
    competitor_ids: list[str] = []
    ai_confirm: bool = False

class CompetitorPriceRequest(BaseModel):
    product_sku: str
    supplier_price_netto: float
    current_selling_price: float
    market_prices: list[float] = []
    brand: str = None
    category: str = None

@app.post("/api/competitor/search")
async def competitor_search(payload: CompetitorSearchRequest):
    """Search for competitor products."""
    results = await provider_registry.search_all(payload.query, limit_per_provider=payload.limit)
    all_results = []
    for provider_id, provider_results in results.items():
        for r in provider_results:
            all_results.append(r.to_dict())
    return {"status": "ok", "query": payload.query, "results": all_results}

@app.post("/api/competitor/match")
async def competitor_match(payload: CompetitorMatchRequest):
    """Match a product against competitors."""
    product = await fetch_product(payload.product_sku)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    from services.canonical_product_mapping import CanonicalProduct
    cp = CanonicalProduct.from_matterhorn("matterhorn", product)
    
    engine = runtime.competitor_matching_engine
    if not engine:
        raise HTTPException(status_code=503, detail="Matching engine not initialized")
    
    # Build competitor product list from search results or database
    competitor_products = []
    # Use search results if competitor_ids not specified
    if not payload.competitor_ids:
        search_results = await provider_registry.search_all(product.get("title", ""), limit_per_provider=5)
        for provider_id, provider_results in search_results.items():
            for r in provider_results:
                competitor_products.append(r.to_dict())
    
    matches = engine.match(cp, competitor_products, ai_confirm=payload.ai_confirm)
    return {
        "status": "ok",
        "product_sku": payload.product_sku,
        "matches": [m.to_dict() for m in matches],
    }

@app.post("/api/competitor/price/calculate")
async def competitor_price_calculate(payload: CompetitorPriceRequest):
    """Calculate a dynamic price recommendation."""
    engine = runtime.competitor_pricing_engine
    if not engine:
        raise HTTPException(status_code=503, detail="Pricing engine not initialized")
    
    result = engine.calculate_price(
        own_product_sku=payload.product_sku,
        supplier_price_netto=__import__("decimal").Decimal(str(payload.supplier_price_netto)),
        current_selling_price=__import__("decimal").Decimal(str(payload.current_selling_price)),
        market_prices=[__import__("decimal").Decimal(str(mp)) for mp in payload.market_prices],
        brand=payload.brand,
        category=payload.category,
    )
    return {"status": "ok", "pricing": result.to_dict()}

@app.get("/api/competitor/providers")
async def list_competitor_providers():
    """List all registered research providers."""
    providers = provider_registry.list()
    return {
        "status": "ok",
        "providers": [
            {
                "provider_id": p.provider_id,
                "name": p.name,
                "status": p.status,
                "rate_limit_per_second": p.rate_limit_per_second,
            }
            for p in providers
        ],
    }

# ---------------------------------------------------------------------------
# Dashboard Metrics Extension
# ---------------------------------------------------------------------------

@app.get("/api/stats/shopware")
async def shopware_stats():
    """Get Shopware integration statistics."""
    return {
        "status": "ok",
        "connections": len(runtime.shopware_connections),
        "mapping_engines": len(runtime.shopware_mapping_engines),
        "export_stats": runtime.shopware_export_manager.get_queue_stats() if runtime.shopware_export_manager else {},
    }

@app.get("/api/stats/competitor")
async def competitor_stats():
    """Get Competitor Intelligence statistics."""
    return {
        "status": "ok",
        "providers": len(provider_registry.providers),
        "connected_providers": len(provider_registry.list_by_status("connected")),
        "matching_engine": runtime.competitor_matching_engine is not None,
        "pricing_engine": runtime.competitor_pricing_engine is not None,
    }

# ---------------------------------------------------------------------------
# Google Merchant Center API
# ---------------------------------------------------------------------------

from pydantic import BaseModel, Field

class GoogleAccountCreate(BaseModel):
    merchant_id: str
    account_name: str
    google_cloud_project_id: str
    oauth_token_encrypted: str
    oauth_refresh_token_encrypted: str | None = None
    service_account_encrypted: str | None = None
    is_primary: bool = False

class GoogleProductExportRequest(BaseModel):
    account_id: str
    product_sku: str
    ai_title: str | None = None
    ai_description: str | None = None
    ai_image_url: str | None = None
    shop_url: str | None = None

class GoogleSyncRequest(BaseModel):
    account_id: str
    job_type: str = "full_sync"

@app.post("/api/google/accounts")
async def create_google_account(payload: GoogleAccountCreate):
    """Create a Google Merchant Center account connection."""
    from uuid import uuid4
    account_id = str(uuid4())
    
    from database import google_upsert_account
    await google_upsert_account(
        account_id=account_id,
        merchant_id=payload.merchant_id,
        account_name=payload.account_name,
        google_cloud_project_id=payload.google_cloud_project_id,
        oauth_token_encrypted=payload.oauth_token_encrypted,
        oauth_refresh_token_encrypted=payload.oauth_refresh_token_encrypted,
        service_account_encrypted=payload.service_account_encrypted,
        is_primary=payload.is_primary,
    )
    
    client = GoogleMerchantClient(merchant_id=payload.merchant_id)
    client.set_oauth2_token(
        access_token=payload.oauth_token_encrypted,
        expires_in=3600,
        refresh_token=payload.oauth_refresh_token_encrypted,
    )
    runtime.google_merchant_clients[account_id] = client
    
    mapper = GoogleProductMapper(country="DE", language="de", currency="EUR")
    validator = GoogleProductValidator(country="DE", language="de", currency="EUR")
    engine = AutonomousExportEngine(
        merchant_client=client,
        mapper=mapper,
        validator=validator,
        omniroute_client=runtime.omniroute,
    )
    runtime.google_mappers[account_id] = mapper
    runtime.google_validators[account_id] = validator
    runtime.google_export_engines[account_id] = engine
    
    await write_log("INFO", "google_account_created", f"Google account {payload.account_name} created.", {"account_id": account_id, "merchant_id": payload.merchant_id})
    return {
        "status": "ok",
        "account_id": account_id,
        "merchant_id": payload.merchant_id,
        "account_name": payload.account_name,
    }

@app.get("/api/google/accounts")
async def list_google_accounts():
    """List all Google Merchant Center accounts."""
    from database import google_list_accounts
    accounts = await google_list_accounts()
    result = []
    for acc in accounts:
        result.append({
            "id": acc["id"],
            "merchant_id": acc["merchant_id"],
            "account_name": acc["account_name"],
            "account_status": acc["account_status"],
            "is_primary": acc["is_primary"],
            "last_token_status": acc["last_token_status"],
            "is_authenticated": acc["id"] in runtime.google_merchant_clients and runtime.google_merchant_clients[acc["id"]].is_authenticated,
        })
    return {"status": "ok", "accounts": result}

@app.post("/api/google/accounts/{account_id}/test")
async def test_google_connection(account_id: str):
    """Test a Google Merchant Center connection."""
    client = runtime.google_merchant_clients.get(account_id)
    if not client:
        raise HTTPException(status_code=404, detail="Account not found or not initialized")
    health = await client.check_health()
    return {"status": "ok", "account_id": account_id, "health": health}

@app.post("/api/google/export")
async def google_export_product(payload: GoogleProductExportRequest):
    """Export a product to Google Merchant Center."""
    engine = runtime.google_export_engines.get(payload.account_id)
    if not engine:
        raise HTTPException(status_code=404, detail="Account not found or export engine not initialized")
    
    from services.canonical_product_mapping import CanonicalProduct
    from database import fetch_product
    
    product_data = await fetch_product(payload.product_sku)
    if not product_data:
        raise HTTPException(status_code=404, detail="Product not found")
    
    cp = CanonicalProduct.from_matterhorn("matterhorn", product_data)
    
    queue_id = await engine.on_quality_check_passed(
        product_sku=payload.product_sku,
        account_id=payload.account_id,
        canonical_product=cp,
        ai_title=payload.ai_title,
        ai_description=payload.ai_description,
        ai_image_url=payload.ai_image_url,
        shop_url=payload.shop_url,
    )
    
    return {"status": "ok", "export_id": queue_id, "product_sku": payload.product_sku}

@app.get("/api/google/export/queue")
async def list_google_export_queue(account_id: str | None = None, status: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    """List Google export queue items."""
    all_items = []
    for acc_id, engine in runtime.google_export_engines.items():
        if account_id and acc_id != account_id:
            continue
        items = engine.list_queue(status=status, account_id=acc_id, limit=limit)
        for item in items:
            all_items.append(item.to_dict())
    
    all_items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"status": "ok", "queue": all_items[:limit]}

@app.get("/api/google/export/stats")
async def google_export_stats(account_id: str | None = None):
    """Get Google export statistics."""
    all_stats = {"total": 0, "pending": 0, "processing": 0, "completed": 0, "errors": 0, "withdrawn": 0, "retry": 0, "dead_letter": 0, "avg_validation_score": 0}
    count = 0
    for acc_id, engine in runtime.google_export_engines.items():
        if account_id and acc_id != account_id:
            continue
        stats = engine.get_queue_stats()
        count += 1
        for key in all_stats:
            all_stats[key] += stats.get(key, 0)
    
    if count > 0:
        all_stats["avg_validation_score"] = round(all_stats["avg_validation_score"] / count, 1)
    
    return {"status": "ok", "stats": all_stats}

@app.post("/api/google/export/process")
async def process_google_export(account_id: str, max_items: int = 10):
    """Process pending Google export queue items."""
    engine = runtime.google_export_engines.get(account_id)
    if not engine:
        raise HTTPException(status_code=404, detail="Account not found or export engine not initialized")
    result = await engine.process_queue(max_items=max_items)
    return {"status": "ok", "account_id": account_id, "result": result}

@app.post("/api/google/export/retry")
async def retry_google_failed(account_id: str | None = None, export_id: str | None = None):
    """Retry failed Google export items."""
    retried = 0
    for acc_id, engine in runtime.google_export_engines.items():
        if account_id and acc_id != account_id:
            continue
        retried += await engine.retry_failed(account_id=acc_id, export_id=export_id)
    if export_id and retried == 0:
        raise HTTPException(status_code=404, detail="Failed export item not found.")
    return {"status": "ok", "retried": retried}

@app.post("/api/google/sync")
async def google_sync(payload: GoogleSyncRequest):
    """Trigger a Google sync job."""
    engine = runtime.google_export_engines.get(payload.account_id)
    if not engine:
        raise HTTPException(status_code=404, detail="Account not found or export engine not initialized")
    
    from uuid import uuid4
    from database import google_upsert_sync_job
    job_id = str(uuid4())
    await google_upsert_sync_job(
        job_id=job_id,
        account_id=payload.account_id,
        job_type=payload.job_type,
        total_products=0,
    )
    
    return {"status": "ok", "job_id": job_id, "job_type": payload.job_type, "account_id": payload.account_id}

@app.get("/api/google/export/health/{account_id}")
async def google_health(account_id: str):
    """Check Google Merchant Center connection health."""
    engine = runtime.google_export_engines.get(account_id)
    if not engine:
        raise HTTPException(status_code=404, detail="Account not found or export engine not initialized")
    health = await engine.health_check()
    return {"status": "ok", "account_id": account_id, "health": health}

# ---------------------------------------------------------------------------
# OmniRoute Control Center API
# ---------------------------------------------------------------------------

class OmniRouteProviderCreate(BaseModel):
    name: str
    provider_type: str = "text"
    api_endpoint: str
    api_key_encrypted: str
    auth_type: str = "bearer"
    priority: int = 100
    rate_limit_per_minute: int = 60
    timeout_seconds: int = 30
    max_retries: int = 3
    retry_delay_seconds: int = 1
    circuit_breaker_threshold: int = 5
    circuit_breaker_timeout_seconds: int = 300
    capabilities: list[str] = []

class OmniRouteModelCreate(BaseModel):
    provider_id: str
    model_id: str
    model_name: str
    model_version: str = ""
    model_category: str = "general"
    capabilities: list[str] = []
    context_window: int = 4096
    input_price_per_1k: float = 0.0
    output_price_per_1k: float = 0.0
    image_price_per_image: float = 0.0
    rate_limit_per_minute: int = 60
    is_active: bool = True

class OmniRouteRuleCreate(BaseModel):
    name: str
    strategy: str
    provider_priority: list[str] = []
    model_category: str = "general"
    min_quality_score: float = 0.0
    max_cost_per_1k: float | None = None
    max_latency_ms: int | None = None

class OmniRoutePolicyCreate(BaseModel):
    workflow_name: str
    rule_id: str
    fallback_rule_id: str | None = None

@app.post("/api/omniroute/providers")
async def create_omniroute_provider(payload: OmniRouteProviderCreate):
    """Create an AI provider."""
    from uuid import uuid4
    from database import ai_upsert_provider
    
    provider_id = str(uuid4())
    provider = AIProvider(
        provider_id=provider_id,
        name=payload.name,
        provider_type=payload.provider_type,
        api_endpoint=payload.api_endpoint,
        api_key_encrypted=payload.api_key_encrypted,
        auth_type=payload.auth_type,
        priority=payload.priority,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        timeout_seconds=payload.timeout_seconds,
        max_retries=payload.max_retries,
        retry_delay_seconds=payload.retry_delay_seconds,
        circuit_breaker_threshold=payload.circuit_breaker_threshold,
        circuit_breaker_timeout_seconds=payload.circuit_breaker_timeout_seconds,
        capabilities=payload.capabilities,
    )
    
    await ai_upsert_provider(
        provider_id=provider_id,
        name=payload.name,
        provider_type=payload.provider_type,
        api_endpoint=payload.api_endpoint,
        api_key_encrypted=payload.api_key_encrypted,
        auth_type=payload.auth_type,
        priority=payload.priority,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        timeout_seconds=payload.timeout_seconds,
        max_retries=payload.max_retries,
        retry_delay_seconds=payload.retry_delay_seconds,
        circuit_breaker_threshold=payload.circuit_breaker_threshold,
        circuit_breaker_timeout_seconds=payload.circuit_breaker_timeout_seconds,
        capabilities_json=json.dumps(payload.capabilities),
    )
    
    runtime.omniroute_provider_manager.add_provider(provider)
    
    await write_log("INFO", "omniroute_provider_created", f"AI provider {payload.name} created.", {"provider_id": provider_id, "type": payload.provider_type})
    return {
        "status": "ok",
        "provider_id": provider_id,
        "name": payload.name,
        "type": payload.provider_type,
    }

@app.get("/api/omniroute/providers")
async def list_omniroute_providers():
    """List all AI providers."""
    providers = runtime.omniroute_provider_manager.list_providers()
    return {
        "status": "ok",
        "providers": [p.to_dict() for p in providers],
    }

@app.post("/api/omniroute/providers/{provider_id}/test")
async def test_omniroute_provider_connection(provider_id: str):
    """Test connection to an AI provider."""
    result = await runtime.omniroute_provider_manager.test_connection(provider_id)
    return {"status": "ok", **result}

@app.post("/api/omniroute/providers/health")
async def omniroute_provider_health_all():
    """Health check all AI providers."""
    result = await runtime.omniroute_provider_manager.health_check_all()
    return {"status": "ok", **result}

@app.post("/api/omniroute/models")
async def create_omniroute_model(payload: OmniRouteModelCreate):
    """Register an AI model."""
    from uuid import uuid4
    from database import ai_upsert_model
    
    model_id = str(uuid4())
    model = AIModel(
        model_id=model_id,
        provider_id=payload.provider_id,
        model_id_str=payload.model_id,
        model_name=payload.model_name,
        model_category=payload.model_category,
        capabilities=payload.capabilities,
        context_window=payload.context_window,
        input_price_per_1k=payload.input_price_per_1k,
        output_price_per_1k=payload.output_price_per_1k,
        image_price_per_image=payload.image_price_per_image,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        is_active=payload.is_active,
    )
    
    await ai_upsert_model(
        model_id=model_id,
        provider_id=payload.provider_id,
        model_id_str=payload.model_id,
        model_name=payload.model_name,
        model_category=payload.model_category,
        context_window=payload.context_window,
        input_price_per_1k=payload.input_price_per_1k,
        output_price_per_1k=payload.output_price_per_1k,
        image_price_per_image=payload.image_price_per_image,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        capabilities_json=json.dumps(payload.capabilities),
        is_active=payload.is_active,
    )
    
    runtime.omniroute_model_registry.register_model(model)
    
    await write_log("INFO", "omniroute_model_created", f"AI model {payload.model_name} registered.", {"model_id": model_id, "provider_id": payload.provider_id})
    return {
        "status": "ok",
        "model_id": model_id,
        "model_name": payload.model_name,
        "provider_id": payload.provider_id,
    }

@app.get("/api/omniroute/models")
async def list_omniroute_models(provider_id: str | None = None, category: str | None = None):
    """List AI models."""
    models = runtime.omniroute_model_registry.list_models(
        provider_id=provider_id,
        category=category,
        active_only=True,
    )
    return {
        "status": "ok",
        "models": [m.to_dict() for m in models],
    }

@app.post("/api/omniroute/routing/rules")
async def create_omniroute_routing_rule(payload: OmniRouteRuleCreate):
    """Create a routing rule."""
    from uuid import uuid4
    rule_id = str(uuid4())
    rule = RoutingRule(
        rule_id=rule_id,
        name=payload.name,
        strategy=payload.strategy,
        provider_priority=payload.provider_priority,
        model_category=payload.model_category,
        min_quality_score=payload.min_quality_score,
        max_cost_per_1k=payload.max_cost_per_1k,
        max_latency_ms=payload.max_latency_ms,
    )
    runtime.omniroute_routing_engine.add_rule(rule)
    return {
        "status": "ok",
        "rule_id": rule_id,
        "name": payload.name,
        "strategy": payload.strategy,
    }

@app.get("/api/omniroute/routing/rules")
async def list_omniroute_routing_rules():
    """List routing rules."""
    return {
        "status": "ok",
        "rules": [r.to_dict() for r in runtime.omniroute_routing_engine.rules.values()],
    }

@app.post("/api/omniroute/routing/policies")
async def create_omniroute_routing_policy(payload: OmniRoutePolicyCreate):
    """Create a routing policy for a workflow."""
    from uuid import uuid4
    policy_id = str(uuid4())
    policy = RoutingPolicy(
        policy_id=policy_id,
        workflow_name=payload.workflow_name,
        rule_id=payload.rule_id,
        fallback_rule_id=payload.fallback_rule_id,
    )
    runtime.omniroute_routing_engine.add_policy(policy)
    return {
        "status": "ok",
        "policy_id": policy_id,
        "workflow_name": payload.workflow_name,
        "rule_id": payload.rule_id,
    }

@app.get("/api/omniroute/routing/policies")
async def list_omniroute_routing_policies():
    """List routing policies."""
    return {
        "status": "ok",
        "policies": [p.to_dict() for p in runtime.omniroute_routing_engine.policies.values()],
    }

@app.post("/api/omniroute/routing/select")
async def omniroute_select_provider(
    workflow_name: str,
    required_capability: str | None = None,
    max_cost: float | None = None,
):
    """Select the best provider and model for a workflow."""
    provider_id, model_id = runtime.omniroute_routing_engine.select_provider(
        workflow_name=workflow_name,
        required_capability=required_capability,
        max_cost=max_cost,
    )
    return {
        "status": "ok",
        "workflow_name": workflow_name,
        "provider_id": provider_id,
        "model_id": model_id,
    }

@app.get("/api/omniroute/cost/calculate")
async def omniroute_calculate_cost(
    input_tokens: int = 0,
    output_tokens: int = 0,
    input_price_per_1k: float = 0.0,
    output_price_per_1k: float = 0.0,
    num_images: int = 0,
    price_per_image: float = 0.0,
):
    """Calculate AI request cost."""
    text_cost = runtime.omniroute_cost_calculator.calculate_text_cost(
        input_tokens, output_tokens, input_price_per_1k, output_price_per_1k
    )
    image_cost = runtime.omniroute_cost_calculator.calculate_image_cost(
        num_images, price_per_image
    )
    total_cost = text_cost + image_cost
    return {
        "status": "ok",
        "text_cost_usd": text_cost,
        "image_cost_usd": image_cost,
        "total_cost_usd": total_cost,
    }

@app.get("/api/omniroute/stats")
async def omniroute_stats():
    """Get OmniRoute Control Center statistics."""
    providers = runtime.omniroute_provider_manager.list_providers()
    models = runtime.omniroute_model_registry.list_models(active_only=True)
    return {
        "status": "ok",
        "providers": len(providers),
        "active_providers": sum(1 for p in providers if p.is_active),
        "models": len(models),
        "active_models": sum(1 for m in models if m.is_active),
        "routing_rules": len(runtime.omniroute_routing_engine.rules),
        "routing_policies": len(runtime.omniroute_routing_engine.policies),
    }

# ---------------------------------------------------------------------------
# WebIF Pages - Google Merchant Center
# ---------------------------------------------------------------------------

@app.get("/google", response_class=HTMLResponse)
async def google_dashboard_page(request: Request):
    """Google Merchant Center dashboard page."""
    return templates.TemplateResponse(request, "google_dashboard.html", {"request": request})

@app.get("/google/export", response_class=HTMLResponse)
async def google_export_page(request: Request):
    """Google Merchant Center export page."""
    return templates.TemplateResponse(request, "google_export.html", {"request": request})

# ---------------------------------------------------------------------------
# WebIF Pages - OmniRoute Control Center
# ---------------------------------------------------------------------------

@app.get("/omniroute", response_class=HTMLResponse)
async def omniroute_dashboard_page(request: Request):
    """OmniRoute Control Center dashboard page."""
    return templates.TemplateResponse(request, "omniroute_dashboard.html", {"request": request})

# ---------------------------------------------------------------------------
# Integration Pipeline: Matterhorn → AI → Google Merchant Center
# ---------------------------------------------------------------------------

@app.post("/api/pipeline/matterhorn-to-google")
async def pipeline_matterhorn_to_google(account_id: str, product_sku: str):
    """Run the full pipeline: Matterhorn product → AI optimization → Google export."""
    from services.canonical_product_mapping import CanonicalProduct
    from database import fetch_product
    
    product_data = await fetch_product(product_sku)
    if not product_data:
        raise HTTPException(status_code=404, detail="Product not found")
    
    cp = CanonicalProduct.from_matterhorn("matterhorn", product_data)
    
    # Step 1: AI optimization via OmniRoute (if configured)
    ai_title = None
    ai_description = None
    ai_image_url = None
    
    if runtime.omniroute:
        try:
            # Route to text AI for title optimization
            provider_id, model_id = runtime.omniroute_routing_engine.select_provider(
                workflow_name="google_title_optimization",
                required_capability="text_generation",
            )
            if provider_id and model_id:
                ai_response = runtime.omniroute.generate_completion(
                    prompt=f"Optimiere folgenden Produkttitel für Google Shopping (max 150 Zeichen, Deutsch): {cp.product_name}",
                    model=model_id,
                    temperature=0.7,
                    max_tokens=200,
                )
                ai_title = ai_response.get("generated_text", cp.product_name)[:150]
        except Exception:
            pass  # Use original title if AI fails
    
    # Step 2: Export to Google Merchant Center
    engine = runtime.google_export_engines.get(account_id)
    if not engine:
        raise HTTPException(status_code=404, detail="Google export engine not initialized for account")
    
    queue_id = await engine.on_quality_check_passed(
        product_sku=product_sku,
        account_id=account_id,
        canonical_product=cp,
        ai_title=ai_title,
        ai_description=ai_description,
        ai_image_url=ai_image_url,
        shop_url=None,
    )
    
    return {
        "status": "ok",
        "pipeline": "matterhorn_to_google",
        "product_sku": product_sku,
        "export_id": queue_id,
        "ai_optimized": ai_title is not None,
    }

@app.post("/api/pipeline/auto-export-imported")
async def pipeline_auto_export_imported(account_id: str):
    """Auto-export all recently imported Matterhorn products to Google."""
    from database import list_products
    from services.canonical_product_mapping import CanonicalProduct
    
    exported = 0
    errors = 0
    
    # Get all active products
    products = await list_products(limit=500)
    
    for p in products:
        try:
            cp = CanonicalProduct.from_matterhorn("matterhorn", p)
            
            await runtime.google_export_engines[account_id].on_quality_check_passed(
                product_sku=p["sku"],
                account_id=account_id,
                canonical_product=cp,
                shop_url=None,
            )
            exported += 1
        except Exception as exc:
            errors += 1
            logger.warning("Auto-export failed for %s: %s", p["sku"], exc)
    
    return {
        "status": "ok",
        "exported": exported,
        "errors": errors,
        "account_id": account_id,
    }


# ---------------------------------------------------------------------------
# Global Control Center API
# ---------------------------------------------------------------------------

@app.get("/api/control-center/overview")
async def control_center_overview() -> dict[str, Any]:
    """Return Global Control Center executive overview."""
    if runtime.control_center is None:
        raise HTTPException(status_code=503, detail="Control center not initialized")
    return await runtime.control_center.get_executive_overview()


@app.get("/api/control-center/dashboard")
async def control_center_dashboard() -> dict[str, Any]:
    """Return aggregated Global Control Center dashboard data."""
    if runtime.control_center is None:
        raise HTTPException(status_code=503, detail="Control center not initialized")
    data = await runtime.control_center.get_full_dashboard()
    data["system_intelligence"]["uptime_seconds"] = runtime.uptime_seconds
    data["system_intelligence"]["database"] = "postgresql" if DATABASE_URL else "sqlite-wal"
    return data


@app.get("/control-center", response_class=HTMLResponse)
async def control_center_page(request: Request) -> HTMLResponse:
    """Render the unified global control center."""
    return templates.TemplateResponse(request, "control_center.html", {"request": request})


@app.get("/api/control-center/system")
async def control_center_system() -> dict[str, Any]:
    """Return system intelligence metrics."""
    if runtime.control_center is None:
        raise HTTPException(status_code=503, detail="Control center not initialized")
    return await runtime.control_center.get_system_intelligence()




# ── Complete WebUI navigation routes (pages & states) ────────────────────

@app.get("/ui/master-dashboard", response_class=HTMLResponse)
async def ui_master_dashboard(request: Request):
    return templates.TemplateResponse(request, "master_dashboard.html", {"request": request})

@app.get("/ui/products", response_class=HTMLResponse)
async def ui_products(request: Request):
    return templates.TemplateResponse(request, "products.html", {"request": request})

@app.get("/ui/inventory", response_class=HTMLResponse)
async def ui_inventory(request: Request):
    return templates.TemplateResponse(request, "inventory.html", {"request": request})

@app.get("/ui/orders", response_class=HTMLResponse)
async def ui_orders(request: Request):
    return templates.TemplateResponse(request, "orders.html", {"request": request})

@app.get("/ui/pricing-center", response_class=HTMLResponse)
async def ui_pricing_center(request: Request):
    return templates.TemplateResponse(request, "pricing_center.html", {"request": request})

@app.get("/ui/bundle-studio", response_class=HTMLResponse)
async def ui_bundle_studio(request: Request):
    return templates.TemplateResponse(request, "bundle_studio.html", {"request": request})

@app.get("/ui/erp-pim", response_class=HTMLResponse)
async def ui_erp_pim(request: Request):
    return templates.TemplateResponse(request, "erp_pim.html", {"request": request})

@app.get("/ui/matterhorn-import", response_class=HTMLResponse)
async def ui_matterhorn_import(request: Request):
    return templates.TemplateResponse(request, "matterhorn_import.html", {"request": request})

@app.get("/ui/shopware", response_class=HTMLResponse)
async def ui_shopware(request: Request):
    return templates.TemplateResponse(request, "shopware.html", {"request": request})

@app.get("/ui/integrations", response_class=HTMLResponse)
async def ui_integrations(request: Request):
    return templates.TemplateResponse(request, "integrations.html", {"request": request})

@app.get("/ui/marketplaces", response_class=HTMLResponse)
async def ui_marketplaces(request: Request):
    return templates.TemplateResponse(request, "marketplaces.html", {"request": request})

@app.get("/ui/ai-control", response_class=HTMLResponse)
async def ui_ai_control(request: Request):
    return templates.TemplateResponse(request, "ai_control.html", {"request": request})

@app.get("/ui/ai-photoshooting", response_class=HTMLResponse)
async def ui_ai_photoshooting(request: Request):
    return templates.TemplateResponse(request, "ai_photoshooting.html", {"request": request})

@app.get("/ui/model-management", response_class=HTMLResponse)
async def ui_model_management(request: Request):
    return templates.TemplateResponse(request, "model_management.html", {"request": request})

@app.get("/ui/competitor-intelligence", response_class=HTMLResponse)
async def ui_competitor_intelligence(request: Request):
    return templates.TemplateResponse(request, "competitor_intelligence.html", {"request": request})

@app.get("/ui/trend-intelligence", response_class=HTMLResponse)
async def ui_trend_intelligence(request: Request):
    return templates.TemplateResponse(request, "trend_intelligence.html", {"request": request})

@app.get("/ui/news-events", response_class=HTMLResponse)
async def ui_news_events(request: Request):
    return templates.TemplateResponse(request, "news_events.html", {"request": request})

@app.get("/ui/knowledge-base", response_class=HTMLResponse)
async def ui_knowledge_base(request: Request):
    return templates.TemplateResponse(request, "knowledge_base.html", {"request": request})

@app.get("/ui/jobs-automation", response_class=HTMLResponse)
async def ui_jobs_automation(request: Request):
    return templates.TemplateResponse(request, "jobs_automation.html", {"request": request})

@app.get("/ui/analytics", response_class=HTMLResponse)
async def ui_analytics(request: Request):
    return templates.TemplateResponse(request, "analytics.html", {"request": request})

@app.get("/ui/audit-log", response_class=HTMLResponse)
async def ui_audit_log(request: Request):
    return templates.TemplateResponse(request, "audit_log.html", {"request": request})

@app.get("/ui/settings", response_class=HTMLResponse)
async def ui_settings(request: Request):
    return templates.TemplateResponse(request, "settings.html", {"request": request})


# ── Event Bus & Internal Communication (Phase 4) ─────────────────────────

@app.get("/api/events/stats")
async def events_stats() -> dict[str, Any]:
    bus = getattr(runtime, "event_bus", None)
    if bus is None:
        return {"running": False, "message": "Event bus not initialized"}
    return bus.get_stats()


@app.get("/api/events/dead-letter")
async def events_dead_letter() -> list[dict[str, Any]]:
    bus = getattr(runtime, "event_bus", None)
    if bus is None:
        return []
    return [e.to_dict() for e in bus.get_dead_letter_queue()]


@app.post("/api/events/dead-letter/replay")
async def events_dead_letter_replay(event_id: str | None = Query(default=None)) -> dict[str, Any]:
    bus = getattr(runtime, "event_bus", None)
    if bus is None:
        raise HTTPException(status_code=503, detail="Event bus not initialized")
    count = await bus.replay_dead_letter(event_id=event_id)
    return {"status": "ok", "replayed": count}


@app.get("/api/events/recent")
async def events_recent(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
    logs = await recent_logs(limit=200)
    events = [l for l in logs if str(l.get("event", "")).startswith("event_")]
    return events[:limit]


@app.get("/api/control-center/modules")
async def control_center_modules() -> dict[str, Any]:
    cc = getattr(runtime, "control_center", None)
    modules = await cc._get_loaded_modules() if cc else []
    bus = getattr(runtime, "event_bus", None)
    return {
        "modules": modules,
        "event_bus": bus.get_stats() if bus else {"running": False},
        "subscribers": bus.get_subscriber_health() if bus else {},
    }


async def publish_runtime_event(event_type: str, payload: dict[str, Any], source: str = "api", entity_id: str | None = None, correlation_id: str | None = None) -> str | None:
    bus = getattr(runtime, "event_bus", None)
    if bus is None or not getattr(bus, "_running", False):
        return None
    try:
        return await bus.publish(event_type=event_type, source_module=source, payload=payload, entity_id=entity_id, correlation_id=correlation_id)
    except Exception:
        return None


# ── GTIN / Product Intelligence APIs ─────────────────────────────────────

class GtinLookupRequest(BaseModel):
    gtin: str = Field(..., min_length=1, max_length=32)


class GtinProviderPayload(BaseModel):
    id: str | None = None
    name: str
    endpoint: str = ""
    auth_ref: str = ""
    priority: int = 100
    rate_limit_per_min: int = 60
    license_info: str = ""
    supported_categories_json: list[str] | dict | None = None
    config_json: dict[str, Any] | None = None
    enabled: bool = False


@app.post("/api/gtin/lookup")
async def api_gtin_lookup(payload: GtinLookupRequest) -> dict[str, Any]:
    from services.gtin_intelligence import lookup_gtin
    result = await lookup_gtin(payload.gtin)
    await publish_runtime_event("gtin.lookup_completed", {"gtin": payload.gtin, "valid": result.get("valid"), "status": (result.get("reconciliation") or {}).get("status")}, source="gtin_intelligence", entity_id=payload.gtin)
    return result


@app.get("/api/gtin/providers")
async def api_gtin_providers() -> list[dict[str, Any]]:
    return await gtin_list_providers(enabled_only=False)


@app.post("/api/gtin/providers")
async def api_gtin_provider_upsert(payload: GtinProviderPayload) -> dict[str, Any]:
    return await gtin_upsert_provider(payload.model_dump())


@app.get("/api/gtin/lookups")
async def api_gtin_lookups(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
    return await gtin_list_lookups(limit=limit)


@app.get("/api/gtin/enrichments")
async def api_gtin_enrichments(entity_id: str | None = Query(default=None), limit: int = Query(default=200, ge=1, le=500)) -> list[dict[str, Any]]:
    return await gtin_list_enrichments(entity_id=entity_id, limit=limit)


@app.get("/api/gtin/conflicts")
async def api_gtin_conflicts(status: str | None = Query(default=None), limit: int = Query(default=200, ge=1, le=500)) -> list[dict[str, Any]]:
    return await gtin_list_conflicts(status=status, limit=limit)


# ── Multi-channel Content APIs ───────────────────────────────────────────

class ContentGenerateRequest(BaseModel):
    sku: str
    channels: list[str] = Field(default_factory=lambda: ["shopware"])
    model: str | None = None


@app.post("/api/content/generate")
async def api_content_generate(payload: ContentGenerateRequest) -> dict[str, Any]:
    product = await fetch_product(payload.sku)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    from services.content_engine import generate_and_store
    result = await generate_and_store(product, payload.channels or ["shopware"], omniroute_client=getattr(runtime, "omniroute", None), model=payload.model)
    await publish_runtime_event("content.generated", {"sku": payload.sku, "channels": payload.channels, "count": result.get("count", 0)}, source="content_engine", entity_id=payload.sku)
    return result


@app.get("/api/content/versions")
async def api_content_versions(sku: str | None = Query(default=None), channel: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
    return await content_version_list(product_sku=sku, channel=channel, limit=limit)


# ── IAM APIs (incremental, admin-managed) ────────────────────────────────

class IamUserCreate(BaseModel):
    email: str
    password: str = Field(default="", min_length=0)
    display_name: str = ""
    username: str = ""
    role: str = "VIEWER"
    is_superadmin: bool = False


@app.get("/api/iam/users")
async def api_iam_users() -> dict[str, Any]:
    from services.iam_core import bootstrap_defaults
    try:
        await bootstrap_defaults()
    except Exception:
        pass
    users = await iam_list_users()
    safe = []
    for u in users:
        u = dict(u); u.pop("password_hash", None); safe.append(u)
    roles = await iam_list_roles()
    perms = await iam_list_permissions()
    return {"users": safe, "roles": roles, "permissions": perms}


@app.post("/api/iam/users")
async def api_iam_create_user(payload: IamUserCreate) -> dict[str, Any]:
    from services.iam_core import hash_password, bootstrap_defaults
    await bootstrap_defaults()
    existing = await iam_get_user_by_email(payload.email)
    if existing:
        raise HTTPException(status_code=409, detail="User already exists")
    pw_hash = hash_password(payload.password) if payload.password else ""
    user = await iam_create_user(email=payload.email, password_hash=pw_hash, display_name=payload.display_name or payload.username or payload.email, username=payload.username or payload.email, is_superadmin=bool(payload.is_superadmin or payload.role == "SUPERADMIN"))
    await iam_assign_role(user.get("id"), payload.role or "VIEWER")
    user.pop("password_hash", None)
    await publish_runtime_event("iam.user_created", {"user_id": user.get("id"), "email": user.get("email"), "role": payload.role}, source="iam", entity_id=str(user.get("id")))
    return {"status": "created", "user": user}


@app.get("/api/iam/roles")
async def api_iam_roles() -> list[dict[str, Any]]:
    return await iam_list_roles()


@app.get("/api/iam/permissions")
async def api_iam_permissions(role: str | None = Query(default=None)) -> list[dict[str, Any]]:
    return await iam_list_permissions(role_name=role)


# ── UI routes for Phase 3/4 modules ───────────────────────────────────────

@app.get("/ui/gtin-intelligence", response_class=HTMLResponse)
async def ui_gtin_intelligence(request: Request):
    return templates.TemplateResponse(request, "gtin_intelligence.html", {"request": request})


@app.get("/ui/security", response_class=HTMLResponse)
async def ui_security_iam(request: Request):
    return templates.TemplateResponse(request, "security_iam.html", {"request": request})


@app.get("/ui/content-studio", response_class=HTMLResponse)
async def ui_content_studio(request: Request):
    return templates.TemplateResponse(request, "content_studio.html", {"request": request})


@app.get("/ui/internal-communication", response_class=HTMLResponse)
async def ui_internal_communication(request: Request):
    return templates.TemplateResponse(request, "internal_communication.html", {"request": request})


# ---------------------------------------------------------------------------
# AI Admin & Business Copilot (Control Agent)
# ---------------------------------------------------------------------------

class _AgentChatPayload(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    conversation_id: str | None = Field(default=None, max_length=128)


@app.post("/api/ai-agent/chat")
async def api_ai_agent_chat(payload: _AgentChatPayload, user: dict[str, Any] = Depends(require_permission("ai.configure"))) -> dict[str, Any]:
    """Process a natural-language request through OmniRoute tool calling (read or prepare action)."""
    from services.ai_agent_controller import chat_with_agent
    actor = str(user.get("email") or user.get("username") or user.get("id") or "admin")
    try:
        return await chat_with_agent(payload.message, payload.conversation_id, actor=actor, runtime=runtime)
    except Exception as exc:
        logger.exception("AI agent chat failed")
        raise HTTPException(status_code=502, detail=f"AI agent unavailable: {exc}") from exc


@app.post("/api/ai-agent/actions/{action_id}/confirm")
async def api_ai_agent_confirm(action_id: str, user: dict[str, Any] = Depends(require_permission("ai.configure"))) -> dict[str, Any]:
    """Execute one previously previewed write after explicit confirmation."""
    from services.ai_agent_controller import execute_confirmed
    actor = str(user.get("email") or user.get("username") or user.get("id") or "admin")
    try:
        result = await execute_confirmed(action_id, actor=actor, runtime=runtime)
        return {"status": "confirmed", "action_id": action_id, "result": result}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/ai-agent/actions/{action_id}/cancel")
async def api_ai_agent_cancel(action_id: str, user: dict[str, Any] = Depends(require_permission("ai.configure"))) -> dict[str, Any]:
    """Cancel a pending AI agent action without executing it."""
    from services.ai_agent_controller import cancel_action
    actor = str(user.get("email") or user.get("username") or user.get("id") or "admin")
    try:
        result = await cancel_action(action_id, actor=actor)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Multimodal Creative / Photoshooting APIs (foundation)
# ---------------------------------------------------------------------------

class _CreativeImagePlanPayload(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    negative_prompt: str = Field(default="", max_length=4000)
    variants: int = Field(default=1, ge=1, le=12)
    product_sku: str = Field(default="", max_length=128)
    model_profile_id: str = Field(default="", max_length=128)
    project_id: str = Field(default="", max_length=128)
    execute: bool = False


class _ProductPhotoPlanPayload(BaseModel):
    product_sku: str = Field(default="", max_length=128)
    models: list[str] = Field(default_factory=list)
    scene: str = Field(default="studio", max_length=128)
    shot_type: str = Field(default="product", max_length=128)
    variants: int = Field(default=2, ge=1, le=12)
    pose: str = Field(default="", max_length=128)
    camera: str = Field(default="", max_length=128)
    lighting: str = Field(default="", max_length=128)
    aspect_ratio: str = Field(default="4:5", max_length=32)
    project_id: str = Field(default="", max_length=128)
    execute: bool = False


@app.get("/api/creative/capabilities")
async def api_creative_capabilities(user: dict[str, Any] = Depends(require_permission("ai.configure"))) -> dict[str, Any]:
    from services.creative_foundation import CreativeCapabilityRegistry
    caps = CreativeCapabilityRegistry.from_runtime(runtime)
    routing = await CreativeCapabilityRegistry.route_with_fallback(runtime, "image_generation")
    return {"status": "ok", **caps, "routing": {"image_generation": routing}}


@app.post("/api/creative/models/seed")
async def api_creative_seed_models(user: dict[str, Any] = Depends(require_permission("ai.configure"))) -> dict[str, Any]:
    from services.creative_foundation import ModelIdentityManager
    seeded = await ModelIdentityManager.seed_canonical_profiles()
    return {"status": "ok", "count": len(seeded), "profiles": [s.get("slug") for s in seeded]}


@app.get("/api/creative/models")
async def api_creative_models(user: dict[str, Any] = Depends(require_permission("ai.configure"))) -> dict[str, Any]:
    from services.creative_foundation import ModelIdentityManager
    return {"status": "ok", "profiles": await ModelIdentityManager.list_profiles()}


@app.get("/api/creative/projects")
async def api_creative_projects(user: dict[str, Any] = Depends(require_permission("ai.configure"))) -> dict[str, Any]:
    from services.creative_foundation import CreativeJobService
    return {"status": "ok", "projects": await CreativeJobService.list_projects()}


@app.get("/api/creative/jobs")
async def api_creative_jobs(project_id: str | None = None, user: dict[str, Any] = Depends(require_permission("ai.configure"))) -> dict[str, Any]:
    from services.creative_foundation import CreativeJobService
    return {"status": "ok", "jobs": await CreativeJobService.list_jobs(project_id=project_id)}


@app.get("/api/creative/jobs/{job_id}")
async def api_creative_job(job_id: str, user: dict[str, Any] = Depends(require_permission("ai.configure"))) -> dict[str, Any]:
    from services.creative_foundation import CreativeJobService
    job = await CreativeJobService.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": "ok", "job": job}


@app.get("/api/creative/outputs")
async def api_creative_outputs(product_sku: str | None = None, job_id: str | None = None, user: dict[str, Any] = Depends(require_permission("ai.configure"))) -> dict[str, Any]:
    from services.creative_foundation import MediaDAM
    assets = await MediaDAM.list_assets(product_sku=product_sku, job_id=job_id)
    return {"status": "ok", "assets": assets}


@app.post("/api/creative/image/plan")
async def api_creative_image_plan(payload: _CreativeImagePlanPayload, user: dict[str, Any] = Depends(require_permission("ai.configure"))) -> dict[str, Any]:
    from services.ai_photoshooting import ImageGenerationAgent
    actor = str(user.get("email") or user.get("username") or user.get("id") or "admin")
    plan = await ImageGenerationAgent.plan(payload.prompt, negative_prompt=payload.negative_prompt, variants=payload.variants, product_sku=payload.product_sku, model_profile_id=payload.model_profile_id, runtime=runtime)
    if payload.execute:
        res = await ImageGenerationAgent.execute_plan(plan, actor=actor, project_id=payload.project_id or None, runtime=runtime)
        return {"status": "ok", "mode": "executed", "plan": plan, **res}
    return {"status": "ok", "mode": "planned", "plan": plan}


@app.post("/api/creative/photoshoot/plan")
async def api_creative_photoshoot_plan(payload: _ProductPhotoPlanPayload, user: dict[str, Any] = Depends(require_permission("ai.configure"))) -> dict[str, Any]:
    from services.ai_photoshooting import ProductPhotographyAgent
    actor = str(user.get("email") or user.get("username") or user.get("id") or "admin")
    plan = await ProductPhotographyAgent.plan(payload.product_sku, models=payload.models, scene=payload.scene, shot_type=payload.shot_type, variants=payload.variants, pose=payload.pose, camera=payload.camera, lighting=payload.lighting, aspect_ratio=payload.aspect_ratio, runtime=runtime)
    if payload.execute:
        res = await ProductPhotographyAgent.execute_plan(plan, actor=actor, project_id=payload.project_id or None, runtime=runtime)
        return {"status": "ok", "mode": "executed", "plan": plan, **res}
    return {"status": "ok", "mode": "planned", "plan": plan}


@app.post("/api/photoshoot/start")
async def api_photoshoot_start(payload: _ProductPhotoPlanPayload, user: dict[str, Any] = Depends(require_permission("ai.configure"))) -> dict[str, Any]:
    from services.ai_photoshooting import ProductPhotographyAgent
    actor = str(user.get("email") or user.get("username") or user.get("id") or "admin")
    plan = await ProductPhotographyAgent.plan(payload.product_sku, models=payload.models, scene=payload.scene, shot_type=payload.shot_type, variants=payload.variants, pose=payload.pose, camera=payload.camera, lighting=payload.lighting, aspect_ratio=payload.aspect_ratio, runtime=runtime)
    res = await ProductPhotographyAgent.execute_plan(plan, actor=actor, project_id=payload.project_id or None, runtime=runtime)
    job = res.get("job") or {}
    return {"status": "ok", "shoot_id": job.get("id"), "job_id": job.get("id"), "result": res, "plan": plan}


@app.get("/api/photoshoot/status/{job_id}")
async def api_photoshoot_status(job_id: str, user: dict[str, Any] = Depends(require_permission("ai.configure"))) -> dict[str, Any]:
    from services.creative_foundation import CreativeJobService
    if job_id == "demo":
        return {"status": "ok", "job": {"id": "demo", "status": "demo", "note": "No demo job executed; configure provider and start a real shoot."}}
    job = await CreativeJobService.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Shoot/job not found")
    return {"status": "ok", "job": job}


@app.get("/api/photoshoot/results/{job_id}")
async def api_photoshoot_results(job_id: str, user: dict[str, Any] = Depends(require_permission("ai.configure"))) -> dict[str, Any]:
    from services.creative_foundation import CreativeJobService, MediaDAM, ProductFidelityEngine
    job = await CreativeJobService.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Shoot/job not found")
    assets = await MediaDAM.list_assets(job_id=job_id)
    fidelity = await ProductFidelityEngine.list_checks(job_id=job_id)
    return {"status": "ok", "job": job, "assets": assets, "fidelity": fidelity, "output": job.get("output_json")}


@app.get("/api/integrations/status")
async def api_integrations_status() -> dict[str, Any]:
    """Aggregate integration/configuration status from real system state (no fabricated data)."""
    from services.connector_registry import ConnectorRegistry
    reg = ConnectorRegistry.get_instance()
    matterhorn_cfg = await matterhorn_get_config()
    matterhorn_feeds = await matterhorn_list_feeds()
    shopware = len(getattr(runtime, "shopware_connections", {}) or {})
    omniroute_url = await get_setting("omniroute_url") or await get_setting("omni_route_base_url")
    return {
        "status": "ok",
        "database": "postgresql" if DATABASE_URL else "sqlite-wal",
        "matterhorn": {
            "configured": bool(matterhorn_cfg) or bool(matterhorn_feeds) or bool(await get_setting("matterhorn_feed_url")),
            "feeds": len(matterhorn_feeds),
        },
        "shopware": {"configured_connections": shopware},
        "omniroute": {"configured": bool(omniroute_url), "healthy": bool(getattr(runtime, "omniroute_healthy", False))},
        "connectors": {"registered": len(reg.list_connectors())},
        "jobs": {"persistent_worker": runtime.persistent_job_task is not None and not runtime.persistent_job_task.done() if getattr(runtime, "persistent_job_task", None) else False,
                 "scheduler": runtime.scheduler_task is not None and not runtime.scheduler_task.done() if getattr(runtime, "scheduler_task", None) else False},
        "event_bus": (runtime.event_bus.get_stats() if getattr(runtime, "event_bus", None) else {"running": False}),
    }



# ---------------------------------------------------------------------------
# Omnichannel CRM & Communication Center APIs
# ---------------------------------------------------------------------------

class CrmIngestPayload(BaseModel):
    channel: str = Field(min_length=1, max_length=32)
    direction: str = Field(default="inbound", max_length=16)
    payload: dict[str, Any] | list[dict[str, Any]] = Field(default_factory=dict)

class CrmActionPayload(BaseModel):
    action: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)

class CrmAppointmentPayload(BaseModel):
    customer_id: str = ""
    thread_id: str = ""
    order_id: str = ""
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    starts_at: str = Field(min_length=1, max_length=64)
    ends_at: str | None = None
    status: str = Field(default="scheduled", max_length=32)
    channel: str = Field(default="email", max_length=32)
    location: str = Field(default="", max_length=256)
    metadata_json: dict[str, Any] = Field(default_factory=dict)

@app.get("/ui/crm", response_class=HTMLResponse)
async def ui_crm(request: Request):
    return templates.TemplateResponse(request, "crm_center.html", {"request": request})

@app.get("/api/crm/overview")
async def api_crm_overview(user: dict[str, Any] = Depends(require_permission("products.read"))):
    from services.crm_service import get_crm_overview
    return {"status": "ok", **await get_crm_overview()}

@app.get("/api/crm/customers")
async def api_crm_customers(limit: int = Query(default=100, ge=1, le=500), q: str | None = Query(default=None), user: dict[str, Any] = Depends(require_permission("products.read"))):
    from database import crm_list_customers
    return {"status": "ok", "items": await crm_list_customers(limit=limit, query=q)}

@app.get("/api/crm/threads")
async def api_crm_threads(limit: int = Query(default=100, ge=1, le=500), channel: str | None = Query(default=None), status: str | None = Query(default=None), user: dict[str, Any] = Depends(require_permission("products.read"))):
    from services.crm_service import list_threads_enriched
    return {"status": "ok", "items": await list_threads_enriched(limit=limit, channel=channel, status=status)}

@app.get("/api/crm/threads/{thread_id}/messages")
async def api_crm_thread_messages(thread_id: str, limit: int = Query(default=200, ge=1, le=1000), user: dict[str, Any] = Depends(require_permission("products.read"))):
    from database import crm_list_messages
    return {"status": "ok", "items": await crm_list_messages(thread_id=thread_id, limit=limit)}

@app.post("/api/crm/ingest")
async def api_crm_ingest(payload: CrmIngestPayload, user: dict[str, Any] = Depends(require_permission("system.configure"))):
    from services.crm_adapters import ingest_webhook
    res = await ingest_webhook(payload.channel, payload.payload, direction=payload.direction)
    return {"status": "ok", **res}

@app.post("/api/crm/sync/{channel}")
async def api_crm_sync(channel: str, user: dict[str, Any] = Depends(require_permission("system.configure"))):
    from services.crm_adapters import sync_channel
    return {"status": "ok", "result": await sync_channel(channel)}

@app.get("/api/crm/appointments")
async def api_crm_appointments(limit: int = Query(default=100, ge=1, le=500), user: dict[str, Any] = Depends(require_permission("products.read"))):
    from database import crm_list_appointments
    return {"status": "ok", "items": await crm_list_appointments(limit=limit)}

@app.post("/api/crm/appointments")
async def api_crm_appointments_create(payload: CrmAppointmentPayload, user: dict[str, Any] = Depends(require_permission("system.configure"))):
    from services.crm_service import create_appointment
    actor = str(user.get("email") or user.get("username") or user.get("id") or "admin")
    data = payload.model_dump(); data["created_by"] = actor
    appt = await create_appointment(data)
    return {"status": "ok", "appointment": appt}

@app.post("/api/crm/actions")
async def api_crm_action(payload: CrmActionPayload, user: dict[str, Any] = Depends(require_permission("system.configure"))):
    from services.crm_service import guarded_manual_action
    actor = str(user.get("email") or user.get("username") or user.get("id") or "admin")
    try:
        res = await guarded_manual_action(action=payload.action, order_id=payload.order_id, actor=actor, payload=payload.payload)
        return {"status": "ok", **res}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc



# ---------------------------------------------------------------------------
# Multi-Channel Connector Management Center APIs
# ---------------------------------------------------------------------------

@app.get("/ui/connectors", response_class=HTMLResponse)
async def ui_connectors(request: Request):
    """Render Multi-Channel Connector Management Center dashboard."""
    return templates.TemplateResponse(request, "connectors_dashboard.html", {"request": request})


@app.get("/api/connectors")
async def api_list_connectors(user: dict[str, Any] = Depends(require_permission("connectors.read"))):
    """List all registered platform connectors with capabilities."""
    from services.connector_registry import ConnectorRegistry
    reg = ConnectorRegistry.get_instance()
    return {
        "status": "ok",
        "connectors": reg.list_connectors(),
    }


@app.post("/api/connectors/{connector_id}/test")
async def api_test_connector(connector_id: str, user: dict[str, Any] = Depends(require_permission("connectors.configure"))):
    """Execute live connection test for a specific connector."""
    from services.connector_registry import ConnectorRegistry
    from database import connector_account_list
    reg = ConnectorRegistry.get_instance()
    accounts = await connector_account_list(connector_id)
    cfg = accounts[0].get("credentials_json", {}) if accounts else {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except Exception:
            cfg = {}
    conn = reg.instantiate(connector_id, config=cfg)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Connector {connector_id} not found in registry.")
    health = await conn.test_connection()
    return {
        "status": "ok",
        "connector_id": connector_id,
        "healthy": health.healthy,
        "state": health.status.value,
        "message": health.message,
        "details": health.details,
    }


@app.get("/api/connectors/accounts")
async def api_get_connector_accounts(user: dict[str, Any] = Depends(require_permission("connectors.read"))):
    """List all seller accounts."""
    from database import connector_account_list
    rows = await connector_account_list()
    return rows


@app.post("/api/connectors/accounts")
async def api_save_connector_account(account: dict[str, Any] = Body(...), user: dict[str, Any] = Depends(require_permission("connectors.configure"))):
    """Save or update a seller account."""
    from database import connector_account_upsert
    saved = await connector_account_upsert(account)
    return saved


@app.delete("/api/connectors/accounts/{account_id}")
async def api_delete_connector_account(account_id: str, user: dict[str, Any] = Depends(require_permission("connectors.configure"))):
    """Delete a seller account."""
    from database import connector_account_delete
    ok = await connector_account_delete(account_id)
    return {"status": "ok", "deleted": ok}


@app.get("/api/connectors/mappings")
async def api_get_connector_mappings(connector_id: str | None = None, user: dict[str, Any] = Depends(require_permission("connectors.read"))):
    """List field mappings."""
    from database import connector_mapping_list
    return await connector_mapping_list(connector_id)


@app.post("/api/connectors/mappings")
async def api_save_connector_mapping(mapping: dict[str, Any] = Body(...), user: dict[str, Any] = Depends(require_permission("connectors.configure"))):
    """Create or update a field mapping rule."""
    from database import connector_mapping_upsert
    return await connector_mapping_upsert(mapping)


@app.get("/api/connectors/exports")
async def api_get_connector_exports(connector_id: str | None = None, user: dict[str, Any] = Depends(require_permission("connectors.read"))):
    """Get export queue and audit history."""
    from database import connector_export_list
    return await connector_export_list(connector_id)


@app.post("/api/connectors/{connector_id}/export")
async def api_trigger_connector_export(connector_id: str, payload: dict[str, Any] = Body(...), user: dict[str, Any] = Depends(require_permission("connectors.configure"))):
    """Enqueue single SKU or bulk export to a platform connector."""
    from database import connector_export_enqueue, list_products
    sku = payload.get("sku")
    if sku:
        enqueued = await connector_export_enqueue(sku=sku, connector_id=connector_id)
        return {"status": "ok", "enqueued": [enqueued]}
    # Bulk export active products
    prods = await list_products(limit=50)
    enqueued_list = []
    for p in prods:
        item = await connector_export_enqueue(sku=p["sku"], connector_id=connector_id)
        enqueued_list.append(item)
    return {"status": "ok", "enqueued_count": len(enqueued_list)}
