"""Unified persistence layer for Omni-Core ERP & BI Middleware.

Production deployments use PostgreSQL through asyncpg. A SQLite WAL fallback is
kept for local development and edge installations. Public functions use the same
contract on either backend; write operations are short and transactional.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

import aiosqlite

try:
    import asyncpg
except ImportError:  # Allows SQLite-only local development before dependencies install.
    asyncpg = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "middleware.db"))
BUSY_TIMEOUT_MS = 5_000
POSTGRES_RETRY_ATTEMPTS = 30
POSTGRES_RETRY_DELAY_SECONDS = 2
_pg_pool: Any | None = None


class DatabaseError(RuntimeError):
    """Raised when a persistent state transition cannot be completed."""


class InsufficientStockError(DatabaseError):
    """Raised when an order cannot reserve inventory without overselling."""


class FulfillmentBlockedError(DatabaseError):
    """Raised when payment or an explicit lock prevents order fulfillment."""


def uses_postgres() -> bool:
    """Return whether the configured backend is PostgreSQL."""
    return DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp suitable for both supported databases."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _pg_ts(value: str | None):
    """Convert ISO timestamp string to datetime for asyncpg TIMESTAMPTZ params."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00'))
        except Exception:
            return value
    return value



POSTGRES_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT '',
        is_secret BOOLEAN NOT NULL DEFAULT FALSE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS products (
        sku TEXT PRIMARY KEY,
        external_matterhorn_id TEXT NOT NULL DEFAULT '',
        parent_sku TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL,
        brand TEXT NOT NULL DEFAULT '',
        manufacturer TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '',
        short_description TEXT NOT NULL DEFAULT '',
        raw_description TEXT NOT NULL DEFAULT '',
        ai_title TEXT NOT NULL DEFAULT '',
        ai_description_html TEXT NOT NULL DEFAULT '',
        ai_tags_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        seo_title TEXT NOT NULL DEFAULT '',
        meta_description TEXT NOT NULL DEFAULT '',
        slug TEXT NOT NULL DEFAULT '',
        canonical_url TEXT NOT NULL DEFAULT '',
        supplier_price NUMERIC(12,4) NOT NULL DEFAULT 0 CHECK (supplier_price >= 0),
        purchase_price NUMERIC(12,4) NOT NULL DEFAULT 0 CHECK (purchase_price >= 0),
        sale_price NUMERIC(12,4) NOT NULL DEFAULT 0 CHECK (sale_price >= 0),
        compare_at_price NUMERIC(12,4) NOT NULL DEFAULT 0 CHECK (compare_at_price >= 0),
        tax_rate NUMERIC(7,4) NOT NULL DEFAULT 0 CHECK (tax_rate >= 0),
        currency TEXT NOT NULL DEFAULT 'EUR',
        stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
        stock_quantity INTEGER NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0),
        reserved_quantity INTEGER NOT NULL DEFAULT 0 CHECK (reserved_quantity >= 0),
        available_quantity INTEGER NOT NULL DEFAULT 0 CHECK (available_quantity >= 0),
        reorder_level INTEGER NOT NULL DEFAULT 0 CHECK (reorder_level >= 0),
        lead_time_days INTEGER NOT NULL DEFAULT 0 CHECK (lead_time_days >= 0),
        supplier_stock INTEGER NOT NULL DEFAULT 0 CHECK (supplier_stock >= 0),
        warehouse_location TEXT NOT NULL DEFAULT '',
        bin_code TEXT NOT NULL DEFAULT '',
        ean TEXT NOT NULL DEFAULT '',
        barcode TEXT NOT NULL DEFAULT '',
        requires_serial_number BOOLEAN NOT NULL DEFAULT FALSE,
        is_fragile BOOLEAN NOT NULL DEFAULT FALSE,
        is_hazmat BOOLEAN NOT NULL DEFAULT FALSE,
        weight_net_kg NUMERIC(12,4) NOT NULL DEFAULT 0 CHECK (weight_net_kg >= 0),
        weight_gross_kg NUMERIC(12,4) NOT NULL DEFAULT 0 CHECK (weight_gross_kg >= 0),
        length_cm NUMERIC(12,4) NOT NULL DEFAULT 0 CHECK (length_cm >= 0),
        width_cm NUMERIC(12,4) NOT NULL DEFAULT 0 CHECK (width_cm >= 0),
        height_cm NUMERIC(12,4) NOT NULL DEFAULT 0 CHECK (height_cm >= 0),
        hs_code TEXT NOT NULL DEFAULT '',
        country_of_origin TEXT NOT NULL DEFAULT '',
        main_image_url TEXT NOT NULL DEFAULT '',
        gallery_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        videos_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        documents_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        source_system TEXT NOT NULL DEFAULT '',
        raw_source_data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        marketplace_mappings_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        is_bundle BOOLEAN NOT NULL DEFAULT FALSE,
        has_variants BOOLEAN NOT NULL DEFAULT FALSE,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('draft', 'active', 'paused', 'archived')),
        source_hash TEXT NOT NULL DEFAULT '',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_products_status_stock ON products(status, stock, is_bundle)",
    "CREATE INDEX IF NOT EXISTS idx_products_ai_tags_json ON products USING GIN (ai_tags_json)",
    "CREATE INDEX IF NOT EXISTS idx_products_external_matterhorn_id ON products(external_matterhorn_id)",
    "CREATE INDEX IF NOT EXISTS idx_products_parent_sku ON products(parent_sku)",
    "CREATE INDEX IF NOT EXISTS idx_products_ean ON products(ean)",
    """
    CREATE TABLE IF NOT EXISTS bundles (
        bundle_sku TEXT PRIMARY KEY REFERENCES products(sku) ON DELETE CASCADE,
        component_skus_json JSONB NOT NULL,
        title TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bundles_components_json ON bundles USING GIN (component_skus_json)",
    """
    CREATE TABLE IF NOT EXISTS channel_content (
        id BIGSERIAL PRIMARY KEY,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
        channel TEXT NOT NULL CHECK (channel IN ('shopware', 'ebay', 'amazon', 'kaufland', 'etsy')),
        content_type TEXT NOT NULL CHECK (content_type IN ('title', 'description', 'tags', 'image')),
        content_text TEXT NOT NULL DEFAULT '',
        metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        ai_generated BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(product_sku, channel, content_type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_channel_content_product_channel ON channel_content(product_sku, channel)",
    "CREATE INDEX IF NOT EXISTS idx_channel_content_metadata_json ON channel_content USING GIN (metadata_json)",
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        sku TEXT NOT NULL REFERENCES products(sku),
        quantity INTEGER NOT NULL CHECK (quantity > 0),
        marketplace TEXT NOT NULL,
        customer_reference TEXT NOT NULL DEFAULT '',
        payment_status TEXT NOT NULL DEFAULT 'pending' CHECK (payment_status IN ('pending', 'paid', 'failed', 'refunded')),
        fulfillment_locked BOOLEAN NOT NULL DEFAULT FALSE,
        status TEXT NOT NULL DEFAULT 'received' CHECK (status IN ('received', 'reserved', 'dispatching', 'dispatched', 'failed', 'cancelled')),
        tracking_number TEXT NOT NULL DEFAULT '',
        tracking_url TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_orders_status_updated ON orders(status, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS bi_metrics (
        id BIGSERIAL PRIMARY KEY,
        metric_name TEXT NOT NULL,
        metric_value NUMERIC(16,4) NOT NULL,
        metric_label TEXT NOT NULL DEFAULT '',
        dimensions_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bi_metrics_name_time ON bi_metrics(metric_name, recorded_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_bi_metrics_dimensions_json ON bi_metrics USING GIN (dimensions_json)",
    """
    CREATE TABLE IF NOT EXISTS system_logs (
        id BIGSERIAL PRIMARY KEY,
        level TEXT NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR')),
        event TEXT NOT NULL,
        message TEXT NOT NULL,
        context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_system_logs_created_at ON system_logs(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_system_logs_context_json ON system_logs USING GIN (context_json)",
    # ── Marketplace Command Center ────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS marketplaces (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        connector_type TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'inactive',
        config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        credentials_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        currency TEXT NOT NULL DEFAULT 'EUR',
        country TEXT NOT NULL DEFAULT 'DE',
        language TEXT NOT NULL DEFAULT 'de-DE',
        min_score NUMERIC(6,2) NOT NULL DEFAULT 70,
        target_products INTEGER NOT NULL DEFAULT 0,
        min_products INTEGER NOT NULL DEFAULT 0,
        max_products INTEGER NOT NULL DEFAULT 0,
        require_available_stock BOOLEAN NOT NULL DEFAULT TRUE,
        require_valid_images BOOLEAN NOT NULL DEFAULT TRUE,
        require_valid_pricing BOOLEAN NOT NULL DEFAULT TRUE,
        require_category_mapping BOOLEAN NOT NULL DEFAULT FALSE,
        sync_interval_minutes INTEGER NOT NULL DEFAULT 60,
        last_sync_at TIMESTAMPTZ,
        last_health_at TIMESTAMPTZ,
        last_health_status TEXT NOT NULL DEFAULT '',
        last_health_message TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_marketplaces_type ON marketplaces(connector_type)",
    "CREATE INDEX IF NOT EXISTS idx_marketplaces_status ON marketplaces(status)",
    """
    CREATE TABLE IF NOT EXISTS marketplace_connections (
        id TEXT PRIMARY KEY,
        marketplace_id TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'disconnected',
        config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        credentials_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        last_test_at TIMESTAMPTZ,
        last_test_status TEXT NOT NULL DEFAULT '',
        last_test_message TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_marketplace_connections_mp ON marketplace_connections(marketplace_id)",
    """
    CREATE TABLE IF NOT EXISTS marketplace_selections (
        id TEXT PRIMARY KEY,
        marketplace_id TEXT NOT NULL,
        product_sku TEXT NOT NULL,
        selection_status TEXT NOT NULL DEFAULT 'NOT_SELECTED',
        approval_status TEXT NOT NULL DEFAULT 'PENDING',
        score NUMERIC(6,2) NOT NULL DEFAULT 0,
        eligible BOOLEAN NOT NULL DEFAULT FALSE,
        reasons_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        blockers_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        recommendations_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        criteria_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        selected_by TEXT NOT NULL DEFAULT 'ai',
        selected_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(marketplace_id, product_sku)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_marketplace_selections_mp ON marketplace_selections(marketplace_id)",
    "CREATE INDEX IF NOT EXISTS idx_marketplace_selections_sku ON marketplace_selections(product_sku)",
    """
    CREATE TABLE IF NOT EXISTS marketplace_products (
        id TEXT PRIMARY KEY,
        marketplace_id TEXT NOT NULL,
        product_sku TEXT NOT NULL,
        external_product_id TEXT NOT NULL DEFAULT '',
        canonical_product_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        marketplace_content_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        marketplace_pricing_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        marketplace_category_id TEXT NOT NULL DEFAULT '',
        attribute_mapping_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        listing_status TEXT NOT NULL DEFAULT 'NOT_LISTED',
        sync_status TEXT NOT NULL DEFAULT 'never',
        last_error TEXT NOT NULL DEFAULT '',
        last_synced_at TIMESTAMPTZ,
        last_exported_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(marketplace_id, product_sku)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_marketplace_products_mp ON marketplace_products(marketplace_id)",
    "CREATE INDEX IF NOT EXISTS idx_marketplace_products_sku ON marketplace_products(product_sku)",
    "CREATE INDEX IF NOT EXISTS idx_marketplace_products_status ON marketplace_products(listing_status)",
    """
    CREATE TABLE IF NOT EXISTS marketplace_listings (
        id TEXT PRIMARY KEY,
        marketplace_id TEXT NOT NULL,
        product_sku TEXT NOT NULL,
        external_listing_id TEXT NOT NULL DEFAULT '',
        listing_status TEXT NOT NULL DEFAULT 'NOT_LISTED',
        last_status_check_at TIMESTAMPTZ,
        status_source TEXT NOT NULL DEFAULT '',
        raw_status_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        url TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(marketplace_id, product_sku)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_marketplace_listings_mp ON marketplace_listings(marketplace_id)",
    """
    CREATE TABLE IF NOT EXISTS marketplace_export_jobs (
        id TEXT PRIMARY KEY,
        marketplace_id TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'dry_run',
        status TEXT NOT NULL DEFAULT 'pending',
        requested_by TEXT NOT NULL DEFAULT '',
        approved_by TEXT NOT NULL DEFAULT '',
        approved_at TIMESTAMPTZ,
        total_items INTEGER NOT NULL DEFAULT 0,
        success_items INTEGER NOT NULL DEFAULT 0,
        failed_items INTEGER NOT NULL DEFAULT 0,
        skipped_items INTEGER NOT NULL DEFAULT 0,
        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        error_message TEXT NOT NULL DEFAULT '',
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_marketplace_export_jobs_mp ON marketplace_export_jobs(marketplace_id)",
    "CREATE INDEX IF NOT EXISTS idx_marketplace_export_jobs_status ON marketplace_export_jobs(status)",
    """
    CREATE TABLE IF NOT EXISTS marketplace_export_items (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        marketplace_id TEXT NOT NULL,
        product_sku TEXT NOT NULL,
        action TEXT NOT NULL DEFAULT 'export',
        status TEXT NOT NULL DEFAULT 'pending',
        external_id TEXT NOT NULL DEFAULT '',
        request_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        response_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        error_message TEXT NOT NULL DEFAULT '',
        idempotency_key TEXT NOT NULL DEFAULT '',
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_marketplace_export_items_job ON marketplace_export_items(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_marketplace_export_items_mp_sku ON marketplace_export_items(marketplace_id, product_sku)",
    """
    CREATE TABLE IF NOT EXISTS marketplace_sync_jobs (
        id TEXT PRIMARY KEY,
        marketplace_id TEXT NOT NULL,
        job_type TEXT NOT NULL DEFAULT 'sync',
        status TEXT NOT NULL DEFAULT 'pending',
        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        error_message TEXT NOT NULL DEFAULT '',
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_marketplace_sync_jobs_mp ON marketplace_sync_jobs(marketplace_id)",
    """
    CREATE TABLE IF NOT EXISTS marketplace_audit_events (
        id TEXT PRIMARY KEY,
        marketplace_id TEXT NOT NULL DEFAULT '',
        product_sku TEXT NOT NULL DEFAULT '',
        job_id TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'info',
        message TEXT NOT NULL DEFAULT '',
        details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_marketplace_audit_mp ON marketplace_audit_events(marketplace_id)",
    "CREATE INDEX IF NOT EXISTS idx_marketplace_audit_sku ON marketplace_audit_events(product_sku)",
    "CREATE INDEX IF NOT EXISTS idx_marketplace_audit_created ON marketplace_audit_events(created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS import_runs (
        id BIGSERIAL PRIMARY KEY,
        source TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT '',
        feed_url TEXT NOT NULL DEFAULT '',
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at TIMESTAMPTZ,
        imported INTEGER NOT NULL DEFAULT 0,
        updated INTEGER NOT NULL DEFAULT 0,
        errors INTEGER NOT NULL DEFAULT 0,
        skipped INTEGER NOT NULL DEFAULT 0,
        created_count INTEGER NOT NULL DEFAULT 0,
        updated_count INTEGER NOT NULL DEFAULT 0,
        variants_linked INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'success', 'failed', 'blocked', 'dry_run')),
        error_message TEXT NOT NULL DEFAULT '',
        summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        protocol_json JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    # ── Catalog / E-Commerce extensions (Matterhorn & channels) ───────────
    """
    CREATE TABLE IF NOT EXISTS categories (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        slug TEXT NOT NULL DEFAULT '',
        parent_id TEXT NOT NULL DEFAULT '',
        external_matterhorn_id TEXT NOT NULL DEFAULT '',
        path TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        source_system TEXT NOT NULL DEFAULT '',
        marketplace_mappings_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(name, parent_id, source_system)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_categories_external ON categories(external_matterhorn_id)",
    """
    CREATE TABLE IF NOT EXISTS product_categories (
        product_sku TEXT NOT NULL,
        category_id TEXT NOT NULL,
        is_primary BOOLEAN NOT NULL DEFAULT FALSE,
        marketplace_mappings_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY(product_sku, category_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_product_categories_category ON product_categories(category_id)",
    """
    CREATE TABLE IF NOT EXISTS product_variants (
        id TEXT PRIMARY KEY,
        parent_sku TEXT NOT NULL,
        sku TEXT NOT NULL UNIQUE,
        external_matterhorn_id TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        ean TEXT NOT NULL DEFAULT '',
        barcode TEXT NOT NULL DEFAULT '',
        supplier_price NUMERIC(12,4) NOT NULL DEFAULT 0,
        sale_price NUMERIC(12,4) NOT NULL DEFAULT 0,
        stock_quantity INTEGER NOT NULL DEFAULT 0,
        reserved_quantity INTEGER NOT NULL DEFAULT 0,
        available_quantity INTEGER NOT NULL DEFAULT 0,
        weight_net_kg NUMERIC(12,4) NOT NULL DEFAULT 0,
        weight_gross_kg NUMERIC(12,4) NOT NULL DEFAULT 0,
        length_cm NUMERIC(12,4) NOT NULL DEFAULT 0,
        width_cm NUMERIC(12,4) NOT NULL DEFAULT 0,
        height_cm NUMERIC(12,4) NOT NULL DEFAULT 0,
        attributes_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        raw_source_data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'active',
        source_hash TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_product_variants_parent ON product_variants(parent_sku)",
    "CREATE INDEX IF NOT EXISTS idx_product_variants_external ON product_variants(external_matterhorn_id)",
    """
    CREATE TABLE IF NOT EXISTS product_attributes (
        id TEXT PRIMARY KEY,
        product_sku TEXT NOT NULL,
        variant_sku TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL,
        value TEXT NOT NULL DEFAULT '',
        unit TEXT NOT NULL DEFAULT '',
        is_filterable BOOLEAN NOT NULL DEFAULT FALSE,
        is_variant BOOLEAN NOT NULL DEFAULT FALSE,
        source_field TEXT NOT NULL DEFAULT '',
        extra_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(product_sku, variant_sku, name, value)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_product_attributes_product ON product_attributes(product_sku)",
    "CREATE INDEX IF NOT EXISTS idx_product_attributes_name ON product_attributes(name)",
    """
    CREATE TABLE IF NOT EXISTS product_media (
        id TEXT PRIMARY KEY,
        product_sku TEXT NOT NULL,
        variant_sku TEXT NOT NULL DEFAULT '',
        media_type TEXT NOT NULL DEFAULT 'image',
        url TEXT NOT NULL,
        position INTEGER NOT NULL DEFAULT 0,
        title TEXT NOT NULL DEFAULT '',
        alt_text TEXT NOT NULL DEFAULT '',
        metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_product_media_product ON product_media(product_sku, media_type)",
    """
    CREATE TABLE IF NOT EXISTS matterhorn_import_previews (
        id TEXT PRIMARY KEY,
        feed_url TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'created',
        summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        items_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        validation_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        source_hash TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,

    # ── Google Merchant Center Tables ────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS google_accounts (
        id TEXT PRIMARY KEY,
        merchant_id TEXT NOT NULL UNIQUE,
        account_name TEXT NOT NULL,
        google_cloud_project_id TEXT NOT NULL,
        account_status TEXT NOT NULL DEFAULT 'pending' CHECK (account_status IN ('pending', 'active', 'suspended', 'disconnected')),
        is_primary BOOLEAN NOT NULL DEFAULT FALSE,
        oauth_token_encrypted TEXT NOT NULL,
        oauth_refresh_token_encrypted TEXT,
        service_account_encrypted TEXT,
        scopes_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        token_expires_at TIMESTAMPTZ,
        last_token_validation TIMESTAMPTZ,
        last_token_status TEXT NOT NULL DEFAULT 'unknown',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_credentials (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        credential_type TEXT NOT NULL CHECK (credential_type IN ('oauth2', 'service_account')),
        credential_name TEXT NOT NULL,
        encrypted_value TEXT NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_data_sources (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        data_source_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        source_type TEXT NOT NULL DEFAULT 'products',
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'error')),
        primary_country TEXT NOT NULL DEFAULT 'DE',
        primary_language TEXT NOT NULL DEFAULT 'de',
        primary_currency TEXT NOT NULL DEFAULT 'EUR',
        feed_label TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(account_id, data_source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT '',
        is_secret BOOLEAN NOT NULL DEFAULT FALSE,
        category TEXT NOT NULL DEFAULT 'general' CHECK (category IN ('general', 'export', 'sync', 'targeting', 'validation')),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_product_mappings (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        matterhorn_field TEXT NOT NULL,
        google_field TEXT NOT NULL,
        mapping_type TEXT NOT NULL CHECK (mapping_type IN ('direct', 'transform', 'fallback', 'conditional', 'concatenate', 'clean')),
        mapping_config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        priority INTEGER NOT NULL DEFAULT 0,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_product_exports (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
        google_product_id TEXT NOT NULL DEFAULT '',
        offer_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'validation', 'exporting', 'completed', 'error', 'withdrawn', 'retry')),
        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        response_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        error_message TEXT NOT NULL DEFAULT '',
        retry_count INTEGER NOT NULL DEFAULT 0,
        max_retries INTEGER NOT NULL DEFAULT 3,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        UNIQUE(product_sku, offer_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_sync_jobs (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        job_type TEXT NOT NULL CHECK (job_type IN ('inventory', 'price', 'full_sync', 'product_update', 'withdrawal')),
        status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'error')),
        total_products INTEGER NOT NULL DEFAULT 0,
        processed_products INTEGER NOT NULL DEFAULT 0,
        failed_products INTEGER NOT NULL DEFAULT 0,
        error_message TEXT NOT NULL DEFAULT '',
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_sync_errors (
        id BIGSERIAL PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
        error_type TEXT NOT NULL,
        error_code TEXT NOT NULL DEFAULT '',
        error_message TEXT NOT NULL DEFAULT '',
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        resolved BOOLEAN NOT NULL DEFAULT FALSE,
        resolved_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_product_diagnostics (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
        google_product_id TEXT NOT NULL DEFAULT '',
        diagnosis_status TEXT NOT NULL CHECK (diagnosis_status IN ('active', 'disapproved', 'pending', 'limited')),
        issues_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        last_checked TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_performance_metrics (
        id BIGSERIAL PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        metric_date DATE NOT NULL,
        impressions INTEGER NOT NULL DEFAULT 0,
        clicks INTEGER NOT NULL DEFAULT 0,
        ctr NUMERIC(6,4) NOT NULL DEFAULT 0,
        cpc NUMERIC(10,4) NOT NULL DEFAULT 0,
        cost NUMERIC(12,4) NOT NULL DEFAULT 0,
        conversions INTEGER NOT NULL DEFAULT 0,
        conversion_value NUMERIC(12,4) NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS google_audit_logs (
        id BIGSERIAL PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
        action TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT 'system',
        target_type TEXT NOT NULL DEFAULT '',
        target_id TEXT NOT NULL DEFAULT '',
        details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # ── OmniRoute Control Center Tables ─────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ai_providers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        provider_type TEXT NOT NULL CHECK (provider_type IN ('text', 'image', 'video', 'embedding', 'multimodal')),
        api_endpoint TEXT NOT NULL,
        api_key_encrypted TEXT NOT NULL,
        auth_type TEXT NOT NULL DEFAULT 'bearer' CHECK (auth_type IN ('bearer', 'api_key', 'oauth2')),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        priority INTEGER NOT NULL DEFAULT 100,
        rate_limit_per_minute INTEGER NOT NULL DEFAULT 60,
        timeout_seconds INTEGER NOT NULL DEFAULT 30,
        max_retries INTEGER NOT NULL DEFAULT 3,
        retry_delay_seconds INTEGER NOT NULL DEFAULT 1,
        circuit_breaker_threshold INTEGER NOT NULL DEFAULT 5,
        circuit_breaker_timeout_seconds INTEGER NOT NULL DEFAULT 300,
        capabilities_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        health_status TEXT NOT NULL DEFAULT 'unknown' CHECK (health_status IN ('healthy', 'degraded', 'unhealthy', 'unknown')),
        last_health_check TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_provider_credentials (
        id TEXT PRIMARY KEY,
        provider_id TEXT NOT NULL REFERENCES ai_providers(id) ON DELETE CASCADE,
        credential_name TEXT NOT NULL,
        encrypted_value TEXT NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_models (
        id TEXT PRIMARY KEY,
        provider_id TEXT NOT NULL REFERENCES ai_providers(id) ON DELETE CASCADE,
        model_id TEXT NOT NULL,
        model_name TEXT NOT NULL,
        model_version TEXT NOT NULL DEFAULT '',
        model_category TEXT NOT NULL DEFAULT 'general' CHECK (model_category IN ('text', 'image', 'video', 'embedding', 'multimodal')),
        capabilities_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        context_window INTEGER NOT NULL DEFAULT 4096,
        input_price_per_1k NUMERIC(10,6) NOT NULL DEFAULT 0,
        output_price_per_1k NUMERIC(10,6) NOT NULL DEFAULT 0,
        image_price_per_image NUMERIC(10,6) NOT NULL DEFAULT 0,
        rate_limit_per_minute INTEGER NOT NULL DEFAULT 60,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        is_discoverable BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(provider_id, model_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_model_capabilities (
        id TEXT PRIMARY KEY,
        model_id TEXT NOT NULL REFERENCES ai_models(id) ON DELETE CASCADE,
        capability_name TEXT NOT NULL,
        capability_value TEXT NOT NULL DEFAULT '',
        confidence NUMERIC(3,2) NOT NULL DEFAULT 1.0,
        source TEXT NOT NULL DEFAULT 'discovered' CHECK (source IN ('discovered', 'manual')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_routing_rules (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        strategy TEXT NOT NULL CHECK (strategy IN ('lowest_cost', 'highest_quality', 'lowest_latency', 'provider_priority', 'capability_matching', 'weighted_distribution')),
        provider_priority_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        model_category TEXT NOT NULL DEFAULT 'general',
        min_quality_score NUMERIC(3,2) NOT NULL DEFAULT 0.0,
        max_cost_per_1k NUMERIC(10,6),
        max_latency_ms INTEGER,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_routing_policies (
        id TEXT PRIMARY KEY,
        workflow_name TEXT NOT NULL,
        rule_id TEXT NOT NULL REFERENCES ai_routing_rules(id) ON DELETE CASCADE,
        fallback_rule_id TEXT REFERENCES ai_routing_rules(id),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(workflow_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_workflows (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        trigger_event TEXT NOT NULL DEFAULT '',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        max_executions_per_hour INTEGER NOT NULL DEFAULT 1000,
        budget_per_execution NUMERIC(10,4) NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_workflow_configs (
        id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL REFERENCES ai_workflows(id) ON DELETE CASCADE,
        step_name TEXT NOT NULL,
        model_id TEXT REFERENCES ai_models(id),
        provider_id TEXT REFERENCES ai_providers(id),
        prompt_template TEXT NOT NULL DEFAULT '',
        parameters_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        step_order INTEGER NOT NULL DEFAULT 0,
        is_required BOOLEAN NOT NULL DEFAULT TRUE,
        on_failure TEXT NOT NULL DEFAULT 'stop' CHECK (on_failure IN ('stop', 'continue', 'retry', 'fallback')),
        max_retries INTEGER NOT NULL DEFAULT 3,
        timeout_seconds INTEGER NOT NULL DEFAULT 30,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_usage_records (
        id BIGSERIAL PRIMARY KEY,
        provider_id TEXT NOT NULL REFERENCES ai_providers(id),
        model_id TEXT REFERENCES ai_models(id),
        workflow_id TEXT REFERENCES ai_workflows(id),
        request_type TEXT NOT NULL,
        tokens_input INTEGER NOT NULL DEFAULT 0,
        tokens_output INTEGER NOT NULL DEFAULT 0,
        images_generated INTEGER NOT NULL DEFAULT 0,
        videos_generated INTEGER NOT NULL DEFAULT 0,
        cost_usd NUMERIC(10,6) NOT NULL DEFAULT 0,
        latency_ms INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'success' CHECK (status IN ('success', 'error', 'retry', 'fallback')),
        error_message TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_cost_records (
        id BIGSERIAL PRIMARY KEY,
        provider_id TEXT NOT NULL REFERENCES ai_providers(id),
        model_id TEXT REFERENCES ai_models(id),
        workflow_id TEXT REFERENCES ai_workflows(id),
        date DATE NOT NULL,
        total_cost_usd NUMERIC(12,4) NOT NULL DEFAULT 0,
        request_count INTEGER NOT NULL DEFAULT 0,
        token_count INTEGER NOT NULL DEFAULT 0,
        image_count INTEGER NOT NULL DEFAULT 0,
        video_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_budgets (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        budget_type TEXT NOT NULL CHECK (budget_type IN ('daily', 'monthly', 'provider', 'workflow')),
        target_id TEXT NOT NULL DEFAULT '',
        amount_usd NUMERIC(12,4) NOT NULL DEFAULT 0,
        warning_threshold_percent INTEGER NOT NULL DEFAULT 80,
        action_on_exceed TEXT NOT NULL DEFAULT 'warn' CHECK (action_on_exceed IN ('warn', 'pause', 'fallback', 'block')),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        reset_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_provider_health (
        id BIGSERIAL PRIMARY KEY,
        provider_id TEXT NOT NULL REFERENCES ai_providers(id),
        status TEXT NOT NULL CHECK (status IN ('healthy', 'degraded', 'unhealthy', 'unknown')),
        response_time_ms INTEGER NOT NULL DEFAULT 0,
        error_rate NUMERIC(5,4) NOT NULL DEFAULT 0,
        last_check TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        details_json JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_request_logs (
        id BIGSERIAL PRIMARY KEY,
        provider_id TEXT NOT NULL REFERENCES ai_providers(id),
        model_id TEXT REFERENCES ai_models(id),
        workflow_id TEXT REFERENCES ai_workflows(id),
        request_id TEXT NOT NULL,
        prompt_hash TEXT NOT NULL DEFAULT '',
        request_payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        response_payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        latency_ms INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'success',
        error_message TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_error_logs (
        id BIGSERIAL PRIMARY KEY,
        provider_id TEXT REFERENCES ai_providers(id),
        model_id TEXT REFERENCES ai_models(id),
        workflow_id TEXT REFERENCES ai_workflows(id),
        error_type TEXT NOT NULL,
        error_code TEXT NOT NULL DEFAULT '',
        error_message TEXT NOT NULL DEFAULT '',
        severity TEXT NOT NULL DEFAULT 'error' CHECK (severity IN ('debug', 'info', 'warning', 'error', 'critical')),
        resolved BOOLEAN NOT NULL DEFAULT FALSE,
        resolved_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_audit_logs (
        id BIGSERIAL PRIMARY KEY,
        actor TEXT NOT NULL DEFAULT 'system',
        action TEXT NOT NULL,
        target_type TEXT NOT NULL DEFAULT '',
        target_id TEXT NOT NULL DEFAULT '',
        details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL DEFAULT '',
    parent_id TEXT NOT NULL DEFAULT '',
    external_matterhorn_id TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    source_system TEXT NOT NULL DEFAULT '',
    marketplace_mappings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(name, parent_id, source_system)
);
CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id);
CREATE INDEX IF NOT EXISTS idx_categories_external ON categories(external_matterhorn_id);
CREATE TABLE IF NOT EXISTS product_categories (
    product_sku TEXT NOT NULL,
    category_id TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    marketplace_mappings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY(product_sku, category_id)
);
CREATE INDEX IF NOT EXISTS idx_product_categories_category ON product_categories(category_id);
CREATE TABLE IF NOT EXISTS product_variants (
    id TEXT PRIMARY KEY,
    parent_sku TEXT NOT NULL,
    sku TEXT NOT NULL UNIQUE,
    external_matterhorn_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    ean TEXT NOT NULL DEFAULT '',
    barcode TEXT NOT NULL DEFAULT '',
    supplier_price REAL NOT NULL DEFAULT 0,
    sale_price REAL NOT NULL DEFAULT 0,
    stock_quantity INTEGER NOT NULL DEFAULT 0,
    reserved_quantity INTEGER NOT NULL DEFAULT 0,
    available_quantity INTEGER NOT NULL DEFAULT 0,
    weight_net_kg REAL NOT NULL DEFAULT 0,
    weight_gross_kg REAL NOT NULL DEFAULT 0,
    length_cm REAL NOT NULL DEFAULT 0,
    width_cm REAL NOT NULL DEFAULT 0,
    height_cm REAL NOT NULL DEFAULT 0,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    raw_source_data_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    source_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_variants_parent ON product_variants(parent_sku);
CREATE INDEX IF NOT EXISTS idx_product_variants_external ON product_variants(external_matterhorn_id);
CREATE TABLE IF NOT EXISTS product_attributes (
    id TEXT PRIMARY KEY,
    product_sku TEXT NOT NULL,
    variant_sku TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    unit TEXT NOT NULL DEFAULT '',
    is_filterable INTEGER NOT NULL DEFAULT 0,
    is_variant INTEGER NOT NULL DEFAULT 0,
    source_field TEXT NOT NULL DEFAULT '',
    extra_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(product_sku, variant_sku, name, value)
);
CREATE INDEX IF NOT EXISTS idx_product_attributes_product ON product_attributes(product_sku);
CREATE INDEX IF NOT EXISTS idx_product_attributes_name ON product_attributes(name);
CREATE TABLE IF NOT EXISTS product_media (
    id TEXT PRIMARY KEY,
    product_sku TEXT NOT NULL,
    variant_sku TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT 'image',
    url TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    alt_text TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_media_product ON product_media(product_sku, media_type);

CREATE TABLE IF NOT EXISTS shopware_connections (
        id TEXT PRIMARY KEY,
        shop_name TEXT NOT NULL,
        shop_url TEXT NOT NULL,
        shopware_version TEXT NOT NULL DEFAULT '6.0',
        api_client_id TEXT NOT NULL,
        api_client_secret_encrypted TEXT NOT NULL,
        sales_channel_id TEXT NOT NULL DEFAULT '',
        default_language TEXT NOT NULL DEFAULT 'de-DE',
        default_currency TEXT NOT NULL DEFAULT 'EUR',
        default_tax_rate NUMERIC(5,2) NOT NULL DEFAULT 19.00,
        default_supplier TEXT NOT NULL DEFAULT '',
        default_shipping_config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        last_health_check TIMESTAMPTZ,
        last_health_status TEXT NOT NULL DEFAULT 'unknown',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shopware_product_mappings (
        id TEXT PRIMARY KEY,
        connection_id TEXT NOT NULL REFERENCES shopware_connections(id) ON DELETE CASCADE,
        matterhorn_field TEXT NOT NULL,
        shopware_field TEXT NOT NULL,
        mapping_type TEXT NOT NULL CHECK (mapping_type IN ('direct', 'transform', 'fallback', 'conditional', 'concatenate', 'clean', 'translate')),
        mapping_config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        priority INTEGER NOT NULL DEFAULT 0,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        version INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(connection_id, matterhorn_field, shopware_field, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shopware_export_queue (
        id TEXT PRIMARY KEY,
        connection_id TEXT NOT NULL REFERENCES shopware_connections(id) ON DELETE CASCADE,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
        export_mode TEXT NOT NULL CHECK (export_mode IN ('publish', 'update', 'variant_update', 'price_update', 'stock_update', 'image_update', 'description_update', 'seo_update', 'category_update', 'full_sync')),
        status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'validation', 'release_required', 'exporting', 'completed', 'error', 'retry')),
        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        response_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        error_message TEXT NOT NULL DEFAULT '',
        retry_count INTEGER NOT NULL DEFAULT 0,
        max_retries INTEGER NOT NULL DEFAULT 3,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS competitors (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        domain TEXT NOT NULL,
        shop_url TEXT NOT NULL DEFAULT '',
        marketplace TEXT NOT NULL DEFAULT '',
        country TEXT NOT NULL DEFAULT 'DE',
        currency TEXT NOT NULL DEFAULT 'EUR',
        detected_categories_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        detected_brands_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        last_successful_research TIMESTAMPTZ,
        products_found INTEGER NOT NULL DEFAULT 0,
        average_price NUMERIC(12,2),
        price_positioning TEXT NOT NULL DEFAULT 'unknown' CHECK (price_positioning IN ('cheap', 'average', 'expensive', 'unknown')),
        availability_status TEXT NOT NULL DEFAULT 'unknown' CHECK (availability_status IN ('good', 'limited', 'poor', 'unknown')),
        data_source TEXT NOT NULL DEFAULT '',
        reliability_score NUMERIC(3,2) NOT NULL DEFAULT 0.0 CHECK (reliability_score >= 0 AND reliability_score <= 1.0),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS competitor_products (
        id TEXT PRIMARY KEY,
        competitor_id TEXT NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
        source_url TEXT NOT NULL,
        source_name TEXT NOT NULL DEFAULT '',
        ean TEXT NOT NULL DEFAULT '',
        sku TEXT NOT NULL DEFAULT '',
        product_name TEXT NOT NULL DEFAULT '',
        brand TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '',
        price NUMERIC(12,2) NOT NULL DEFAULT 0,
        shipping_cost NUMERIC(10,2) NOT NULL DEFAULT 0,
        total_price NUMERIC(12,2) NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'EUR',
        availability TEXT NOT NULL DEFAULT '',
        last_checked TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        data_source TEXT NOT NULL DEFAULT '',
        match_score INTEGER NOT NULL DEFAULT 0 CHECK (match_score >= 0 AND match_score <= 100),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_matches (
        id TEXT PRIMARY KEY,
        own_product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
        competitor_product_id TEXT NOT NULL REFERENCES competitor_products(id) ON DELETE CASCADE,
        match_score INTEGER NOT NULL CHECK (match_score >= 0 AND match_score <= 100),
        match_stage TEXT NOT NULL CHECK (match_stage IN ('exact_ean', 'sku_model', 'brand_name_attrs', 'image_similarity', 'semantic', 'manual')),
        match_reason TEXT NOT NULL DEFAULT '',
        ai_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
        ai_confidence NUMERIC(3,2),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS competitor_price_history (
        id BIGSERIAL PRIMARY KEY,
        competitor_product_id TEXT NOT NULL REFERENCES competitor_products(id) ON DELETE CASCADE,
        price NUMERIC(12,2) NOT NULL DEFAULT 0,
        shipping_cost NUMERIC(10,2) NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'EUR',
        availability TEXT NOT NULL DEFAULT '',
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        data_source TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS monitoring_jobs (
        id TEXT PRIMARY KEY,
        competitor_id TEXT NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
        competitor_product_id TEXT REFERENCES competitor_products(id),
        interval_hours INTEGER NOT NULL CHECK (interval_hours IN (6, 12, 24, 168)),
        next_run TIMESTAMPTZ NOT NULL,
        last_run TIMESTAMPTZ,
        last_price NUMERIC(12,2),
        last_availability TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'error', 'disabled')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_alerts (
        id TEXT PRIMARY KEY,
        alert_type TEXT NOT NULL CHECK (alert_type IN ('competitor_lower', 'competitor_higher', 'out_of_stock', 'new_competitor', 'price_above_market', 'margin_below_min', 'special_offer')),
        own_product_sku TEXT REFERENCES products(sku) ON DELETE CASCADE,
        competitor_id TEXT REFERENCES competitors(id) ON DELETE CASCADE,
        competitor_product_id TEXT REFERENCES competitor_products(id) ON DELETE CASCADE,
        threshold_value NUMERIC(12,2),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        last_triggered TIMESTAMPTZ,
        notification_channels_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pricing_recommendations (
        id TEXT PRIMARY KEY,
        own_product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
        current_price NUMERIC(12,2) NOT NULL DEFAULT 0,
        recommended_price NUMERIC(12,2) NOT NULL DEFAULT 0,
        market_average NUMERIC(12,2) NOT NULL DEFAULT 0,
        market_min NUMERIC(12,2) NOT NULL DEFAULT 0,
        expected_margin NUMERIC(5,4) NOT NULL DEFAULT 0,
        contribution_margin NUMERIC(5,4) NOT NULL DEFAULT 0,
        price_deviation NUMERIC(5,4) NOT NULL DEFAULT 0,
        strategy TEXT NOT NULL CHECK (strategy IN ('premium_positioning', 'market_average', 'competitive_pricing', 'margin_optimization', 'clearance')),
        justification TEXT NOT NULL DEFAULT '',
        data_quality TEXT NOT NULL DEFAULT '',
        risk_indicator TEXT NOT NULL DEFAULT '',
        is_approved BOOLEAN NOT NULL DEFAULT FALSE,
        applied_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_runs (
        id TEXT PRIMARY KEY,
        query TEXT NOT NULL,
        provider TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
        products_found INTEGER NOT NULL DEFAULT 0,
        matches_created INTEGER NOT NULL DEFAULT 0,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        error_message TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pricing_rules (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        rule_type TEXT NOT NULL CHECK (rule_type IN ('global', 'brand', 'category', 'product', 'sales_channel')),
        target_entity_id TEXT NOT NULL DEFAULT '',
        min_margin NUMERIC(5,4) NOT NULL DEFAULT 0,
        target_margin NUMERIC(5,4),
        markup_percent NUMERIC(5,2),
        min_selling_price NUMERIC(12,2),
        max_selling_price NUMERIC(12,2),
        strategy TEXT NOT NULL DEFAULT 'market_average',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS gtin_providers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        endpoint TEXT NOT NULL DEFAULT '',
        auth_ref TEXT NOT NULL DEFAULT '',
        priority INTEGER NOT NULL DEFAULT 100,
        rate_limit_per_min INTEGER NOT NULL DEFAULT 60,
        license_info TEXT NOT NULL DEFAULT '',
        supported_categories_json JSONB NOT NULL DEFAULT '[]',
        config_json JSONB NOT NULL DEFAULT '{}',
        enabled BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """CREATE TABLE IF NOT EXISTS gtin_lookups (id TEXT PRIMARY KEY, gtin TEXT NOT NULL, result_json JSONB NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'done', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS gtin_enrichments (id TEXT PRIMARY KEY, entity_type TEXT NOT NULL DEFAULT 'product', entity_id TEXT NOT NULL, field_name TEXT NOT NULL, original_value TEXT NOT NULL DEFAULT '', normalized_value TEXT NOT NULL DEFAULT '', source_provider TEXT NOT NULL DEFAULT '', source_record_id TEXT NOT NULL DEFAULT '', confidence NUMERIC(5,4) NOT NULL DEFAULT 0.0, trust_level TEXT NOT NULL DEFAULT 'C', verification_state TEXT NOT NULL DEFAULT 'adopted', license_info TEXT NOT NULL DEFAULT '', metadata_json JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS gtin_conflicts (id TEXT PRIMARY KEY, entity_type TEXT NOT NULL DEFAULT 'product', entity_id TEXT NOT NULL, field_name TEXT NOT NULL, candidates_json JSONB NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'open', resolution_json JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS content_versions (id TEXT PRIMARY KEY, product_sku TEXT NOT NULL DEFAULT '', channel TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', model TEXT NOT NULL DEFAULT '', prompt_version TEXT NOT NULL DEFAULT '', content_json JSONB NOT NULL DEFAULT '{}', validation_json JSONB NOT NULL DEFAULT '{}', approved_by TEXT NOT NULL DEFAULT '', approved_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS iam_users (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, username TEXT NOT NULL DEFAULT '', password_hash TEXT NOT NULL DEFAULT '', display_name TEXT NOT NULL DEFAULT '', is_active BOOLEAN NOT NULL DEFAULT TRUE, is_superadmin BOOLEAN NOT NULL DEFAULT FALSE, mfa_required BOOLEAN NOT NULL DEFAULT FALSE, last_login_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS iam_roles (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS iam_permissions (id TEXT PRIMARY KEY, role_name TEXT NOT NULL, permission TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE(role_name, permission))""",
    """CREATE TABLE IF NOT EXISTS iam_user_roles (user_id TEXT NOT NULL, role_name TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE(user_id, role_name))""",
    """CREATE TABLE IF NOT EXISTS iam_sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_token_hash TEXT NOT NULL, ip_address TEXT NOT NULL DEFAULT '', user_agent TEXT NOT NULL DEFAULT '', expires_at TIMESTAMPTZ, revoked BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_seen_at TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS news_sources (id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'general', enabled BOOLEAN NOT NULL DEFAULT TRUE, config_json JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS news_items (id TEXT PRIMARY KEY, source_id TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'general', published_at TIMESTAMPTZ, metadata_json JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS knowledge_entries (id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'general', tags_json JSONB NOT NULL DEFAULT '[]', source TEXT NOT NULL DEFAULT '', metadata_json JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    # ── Job Queue & Scheduler ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','succeeded','failed','cancelled','retry')),
        payload_json JSONB NOT NULL DEFAULT '{}',
        result_json JSONB NOT NULL DEFAULT '{}',
        error_message TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        locked_by TEXT NOT NULL DEFAULT '',
        locked_at TIMESTAMPTZ,
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        created_by TEXT NOT NULL DEFAULT '',
        correlation_id TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status_run_at ON jobs(status, run_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_type_created ON jobs(type, created_at)",
    """
    CREATE TABLE IF NOT EXISTS scheduler_config (
        id TEXT PRIMARY KEY,
        job_type TEXT NOT NULL UNIQUE,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        interval_seconds INTEGER NOT NULL DEFAULT 3600,
        payload_json JSONB NOT NULL DEFAULT '{}',
        last_run_at TIMESTAMPTZ,
        next_run_at TIMESTAMPTZ,
        last_status TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # ── Matterhorn Config ────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS matterhorn_config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT '',
        is_secret BOOLEAN NOT NULL DEFAULT FALSE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS matterhorn_feeds (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        feed_type TEXT NOT NULL DEFAULT 'xml',
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        auth_header TEXT NOT NULL DEFAULT '',
        config_json JSONB NOT NULL DEFAULT '{}',
        last_imported_at TIMESTAMPTZ,
        last_status TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS matterhorn_mappings (
        id TEXT PRIMARY KEY,
        feed_id TEXT NOT NULL DEFAULT '',
        source_field TEXT NOT NULL,
        target_field TEXT NOT NULL,
        transform TEXT NOT NULL DEFAULT 'direct',
        config_json JSONB NOT NULL DEFAULT '{}',
        priority INTEGER NOT NULL DEFAULT 0,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # ── Pricing & Competitor ─────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS pricing_rules (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        rule_type TEXT NOT NULL DEFAULT 'margin',
        config_json JSONB NOT NULL DEFAULT '{}',
        priority INTEGER NOT NULL DEFAULT 0,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pricing_recommendations (
        id TEXT PRIMARY KEY,
        product_sku TEXT NOT NULL,
        recommended_price NUMERIC(12,2) NOT NULL,
        base_price NUMERIC(12,2) NOT NULL DEFAULT 0,
        margin_pct NUMERIC(6,2) NOT NULL DEFAULT 0,
        confidence NUMERIC(4,2) NOT NULL DEFAULT 0,
        reasoning TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_alerts (
        id TEXT PRIMARY KEY,
        product_sku TEXT NOT NULL,
        alert_type TEXT NOT NULL DEFAULT 'price_drop',
        threshold_value NUMERIC(12,2) NOT NULL DEFAULT 0,
        current_value NUMERIC(12,2) NOT NULL DEFAULT 0,
        message TEXT NOT NULL DEFAULT '',
        resolved BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        resolved_at TIMESTAMPTZ
    )
    """,
    # ── Shopware Connector ───────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL DEFAULT '',
    parent_id TEXT NOT NULL DEFAULT '',
    external_matterhorn_id TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    source_system TEXT NOT NULL DEFAULT '',
    marketplace_mappings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(name, parent_id, source_system)
);
CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id);
CREATE INDEX IF NOT EXISTS idx_categories_external ON categories(external_matterhorn_id);
CREATE TABLE IF NOT EXISTS product_categories (
    product_sku TEXT NOT NULL,
    category_id TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    marketplace_mappings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY(product_sku, category_id)
);
CREATE INDEX IF NOT EXISTS idx_product_categories_category ON product_categories(category_id);
CREATE TABLE IF NOT EXISTS product_variants (
    id TEXT PRIMARY KEY,
    parent_sku TEXT NOT NULL,
    sku TEXT NOT NULL UNIQUE,
    external_matterhorn_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    ean TEXT NOT NULL DEFAULT '',
    barcode TEXT NOT NULL DEFAULT '',
    supplier_price REAL NOT NULL DEFAULT 0,
    sale_price REAL NOT NULL DEFAULT 0,
    stock_quantity INTEGER NOT NULL DEFAULT 0,
    reserved_quantity INTEGER NOT NULL DEFAULT 0,
    available_quantity INTEGER NOT NULL DEFAULT 0,
    weight_net_kg REAL NOT NULL DEFAULT 0,
    weight_gross_kg REAL NOT NULL DEFAULT 0,
    length_cm REAL NOT NULL DEFAULT 0,
    width_cm REAL NOT NULL DEFAULT 0,
    height_cm REAL NOT NULL DEFAULT 0,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    raw_source_data_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    source_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_variants_parent ON product_variants(parent_sku);
CREATE INDEX IF NOT EXISTS idx_product_variants_external ON product_variants(external_matterhorn_id);
CREATE TABLE IF NOT EXISTS product_attributes (
    id TEXT PRIMARY KEY,
    product_sku TEXT NOT NULL,
    variant_sku TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    unit TEXT NOT NULL DEFAULT '',
    is_filterable INTEGER NOT NULL DEFAULT 0,
    is_variant INTEGER NOT NULL DEFAULT 0,
    source_field TEXT NOT NULL DEFAULT '',
    extra_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(product_sku, variant_sku, name, value)
);
CREATE INDEX IF NOT EXISTS idx_product_attributes_product ON product_attributes(product_sku);
CREATE INDEX IF NOT EXISTS idx_product_attributes_name ON product_attributes(name);
CREATE TABLE IF NOT EXISTS product_media (
    id TEXT PRIMARY KEY,
    product_sku TEXT NOT NULL,
    variant_sku TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT 'image',
    url TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    alt_text TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_media_product ON product_media(product_sku, media_type);

CREATE TABLE IF NOT EXISTS shopware_connections (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        base_url TEXT NOT NULL,
        client_id TEXT NOT NULL DEFAULT '',
        client_secret TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'disconnected' CHECK (status IN ('connected','disconnected','error','testing')),
        last_checked_at TIMESTAMPTZ,
        last_error TEXT NOT NULL DEFAULT '',
        config_json JSONB NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shopware_product_mappings (
        id TEXT PRIMARY KEY,
        connection_id TEXT NOT NULL,
        product_sku TEXT NOT NULL,
        shopware_product_id TEXT NOT NULL DEFAULT '',
        shopware_variant_id TEXT NOT NULL DEFAULT '',
        last_synced_at TIMESTAMPTZ,
        sync_status TEXT NOT NULL DEFAULT 'pending',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(connection_id, product_sku)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shopware_export_queue (
        id TEXT PRIMARY KEY,
        connection_id TEXT NOT NULL,
        product_sku TEXT NOT NULL,
        operation TEXT NOT NULL DEFAULT 'upsert',
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','completed','failed')),
        payload_json JSONB NOT NULL DEFAULT '{}',
        error_message TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # ── Competitor Intelligence ──────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS competitors (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        base_url TEXT NOT NULL DEFAULT '',
        scrape_config_json JSONB NOT NULL DEFAULT '{}',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        last_scraped_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS competitor_products (
        id TEXT PRIMARY KEY,
        competitor_id TEXT NOT NULL,
        product_sku TEXT NOT NULL DEFAULT '',
        competitor_sku TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        current_price NUMERIC(12,2) NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'EUR',
        url TEXT NOT NULL DEFAULT '',
        in_stock BOOLEAN NOT NULL DEFAULT TRUE,
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS competitor_price_history (
        id BIGSERIAL PRIMARY KEY,
        competitor_product_id TEXT NOT NULL,
        price NUMERIC(12,2) NOT NULL,
        currency TEXT NOT NULL DEFAULT 'EUR',
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_matches (
        id TEXT PRIMARY KEY,
        our_sku TEXT NOT NULL,
        competitor_product_id TEXT NOT NULL,
        match_score NUMERIC(4,2) NOT NULL DEFAULT 0,
        match_method TEXT NOT NULL DEFAULT 'ean',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(our_sku, competitor_product_id)
    )
    """,
    # ── Research ─────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS research_runs (
        id TEXT PRIMARY KEY,
        query TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'web',
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed')),
        result_json JSONB NOT NULL DEFAULT '{}',
        error_message TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at TIMESTAMPTZ
    )
    """,
    # ── Multi-Channel Connector Management Center ────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS connector_accounts (
        id TEXT PRIMARY KEY,
        connector_id TEXT NOT NULL,
        account_name TEXT NOT NULL,
        marketplace TEXT NOT NULL DEFAULT 'DE',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        is_sandbox BOOLEAN NOT NULL DEFAULT FALSE,
        credentials_json JSONB NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS connector_mappings (
        id TEXT PRIMARY KEY,
        connector_id TEXT NOT NULL,
        canonical_field TEXT NOT NULL,
        channel_field TEXT NOT NULL,
        mapping_type TEXT NOT NULL DEFAULT 'direct',
        config_json JSONB NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS connector_exports (
        id TEXT PRIMARY KEY,
        connector_id TEXT NOT NULL,
        account_id TEXT NOT NULL DEFAULT '',
        sku TEXT NOT NULL,
        remote_listing_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        payload_json JSONB NOT NULL DEFAULT '{}',
        error_message TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """    ,
    """
    CREATE TABLE IF NOT EXISTS crm_customers (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL DEFAULT '',
        display_name TEXT NOT NULL DEFAULT '',
        shopware_id TEXT NOT NULL DEFAULT '',
        amazon_id TEXT NOT NULL DEFAULT '',
        ebay_id TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '',
        metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_crm_customers_email ON crm_customers(email)",
    """
    CREATE TABLE IF NOT EXISTS crm_threads (
        id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL,
        channel TEXT NOT NULL CHECK (channel IN ('shopware','amazon','ebay','email','messenger')),
        external_thread_id TEXT NOT NULL DEFAULT '',
        subject TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','pending','resolved','closed')),
        last_message_at TIMESTAMPTZ,
        order_id TEXT NOT NULL DEFAULT '',
        metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(channel, external_thread_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_crm_threads_customer ON crm_threads(customer_id, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS crm_messages (
        id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        customer_id TEXT NOT NULL,
        channel TEXT NOT NULL CHECK (channel IN ('shopware','amazon','ebay','email','messenger')),
        direction TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
        external_message_id TEXT NOT NULL DEFAULT '',
        idempotency_key TEXT NOT NULL,
        subject TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        sender TEXT NOT NULL DEFAULT '',
        recipient TEXT NOT NULL DEFAULT '',
        attachments_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        status TEXT NOT NULL DEFAULT 'received',
        metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(channel, idempotency_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_crm_messages_thread ON crm_messages(thread_id, sent_at ASC)",
    """
    CREATE TABLE IF NOT EXISTS crm_appointments (
        id TEXT PRIMARY KEY,
        customer_id TEXT,
        thread_id TEXT,
        order_id TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        starts_at TIMESTAMPTZ NOT NULL,
        ends_at TIMESTAMPTZ,
        status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','confirmed','completed','cancelled','no_show')),
        channel TEXT NOT NULL DEFAULT 'email',
        location TEXT NOT NULL DEFAULT '',
        metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_by TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_agent_actions (
        id TEXT PRIMARY KEY,
        actor TEXT NOT NULL DEFAULT '',
        prompt TEXT NOT NULL DEFAULT '',
        tool_name TEXT NOT NULL DEFAULT '',
        args_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        preview_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','executed','cancelled','failed')),
        requires_confirmation BOOLEAN NOT NULL DEFAULT TRUE,
        result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        error_message TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        executed_at TIMESTAMPTZ
    )
    """,
    # ── Multimodal Creative Engine (foundation) ─────────────────────────
    """
    CREATE TABLE IF NOT EXISTS creative_projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        project_type TEXT NOT NULL DEFAULT 'general',
        status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','planned','running','completed','failed','cancelled','review')),
        brief TEXT NOT NULL DEFAULT '',
        context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_by TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS creative_jobs (
        id TEXT PRIMARY KEY,
        project_id TEXT REFERENCES creative_projects(id) ON DELETE SET NULL,
        job_type TEXT NOT NULL DEFAULT 'image_generation',
        capability TEXT NOT NULL DEFAULT 'image_generation',
        status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','planning','running','polling','succeeded','failed','cancelled','review_required')),
        provider_id TEXT NOT NULL DEFAULT '',
        model_id TEXT NOT NULL DEFAULT '',
        prompt TEXT NOT NULL DEFAULT '',
        negative_prompt TEXT NOT NULL DEFAULT '',
        input_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        output_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        cost_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        error_message TEXT NOT NULL DEFAULT '',
        requires_review BOOLEAN NOT NULL DEFAULT FALSE,
        created_by TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_creative_jobs_project ON creative_jobs(project_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_creative_jobs_status ON creative_jobs(status, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS media_assets (
        id TEXT PRIMARY KEY,
        asset_type TEXT NOT NULL DEFAULT 'image' CHECK (asset_type IN ('image','video','audio','file')),
        source_kind TEXT NOT NULL DEFAULT 'generated' CHECK (source_kind IN ('original','generated','edited','reference','upload')),
        immutable_original BOOLEAN NOT NULL DEFAULT FALSE,
        storage_path TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL DEFAULT '',
        sha256 TEXT NOT NULL DEFAULT '',
        mime_type TEXT NOT NULL DEFAULT '',
        width INTEGER,
        height INTEGER,
        duration_seconds DOUBLE PRECISION,
        size_bytes BIGINT,
        product_sku TEXT NOT NULL DEFAULT '',
        model_profile_id TEXT NOT NULL DEFAULT '',
        job_id TEXT REFERENCES creative_jobs(id) ON DELETE SET NULL,
        parent_asset_id TEXT NOT NULL DEFAULT '',
        metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_media_assets_product ON media_assets(product_sku)",
    "CREATE INDEX IF NOT EXISTS idx_media_assets_sha ON media_assets(sha256)",
    "CREATE INDEX IF NOT EXISTS idx_media_assets_job ON media_assets(job_id)",
    """
    CREATE TABLE IF NOT EXISTS model_profiles (
        id TEXT PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        reference_images_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        identity_profile_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        body_profile_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        style_profile_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        preferred_locations_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        preferred_perspectives_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        posing_profiles_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        prompt_templates_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        rights_consent_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        usage_allowed BOOLEAN NOT NULL DEFAULT TRUE,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        history_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_fidelity_checks (
        id TEXT PRIMARY KEY,
        job_id TEXT REFERENCES creative_jobs(id) ON DELETE SET NULL,
        output_asset_id TEXT NOT NULL DEFAULT '',
        product_sku TEXT NOT NULL DEFAULT '',
        original_asset_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        scores_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        overall_score DOUBLE PRECISION NOT NULL DEFAULT 0,
        verdict TEXT NOT NULL DEFAULT 'pending' CHECK (verdict IN ('pending','pass','fail','manual_review')),
        critical_issues_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fidelity_job ON product_fidelity_checks(job_id)",
    """
    CREATE TABLE IF NOT EXISTS finance_documents (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        external_id TEXT NOT NULL,
        document_type TEXT NOT NULL CHECK (document_type IN ('invoice','credit_note','supplier_invoice','expense')),
        direction TEXT NOT NULL CHECK (direction IN ('receivable','payable')),
        order_reference TEXT NOT NULL DEFAULT '',
        currency TEXT NOT NULL CHECK (char_length(currency) = 3),
        net_amount NUMERIC(20,6) NOT NULL CHECK (net_amount >= 0),
        tax_amount NUMERIC(20,6) NOT NULL CHECK (tax_amount >= 0),
        gross_amount NUMERIC(20,6) NOT NULL CHECK (gross_amount >= 0),
        status TEXT NOT NULL DEFAULT 'issued',
        issued_at TIMESTAMPTZ,
        due_at TIMESTAMPTZ,
        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(provider, external_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_finance_documents_order ON finance_documents(order_reference, currency)",
    "CREATE INDEX IF NOT EXISTS idx_finance_documents_currency_amount ON finance_documents(currency, gross_amount)",
    "CREATE INDEX IF NOT EXISTS idx_finance_documents_issued ON finance_documents(issued_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS finance_transactions (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        external_id TEXT NOT NULL,
        transaction_type TEXT NOT NULL CHECK (transaction_type IN ('payment','refund','fee','payout','adjustment')),
        order_reference TEXT NOT NULL DEFAULT '',
        settlement_reference TEXT NOT NULL DEFAULT '',
        currency TEXT NOT NULL CHECK (char_length(currency) = 3),
        amount NUMERIC(20,6) NOT NULL CHECK (amount >= 0),
        occurred_at TIMESTAMPTZ,
        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(provider, external_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_finance_transactions_order ON finance_transactions(order_reference, currency)",
    "CREATE INDEX IF NOT EXISTS idx_finance_transactions_settlement ON finance_transactions(settlement_reference)",
    """
    CREATE TABLE IF NOT EXISTS finance_settlements (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        external_id TEXT NOT NULL,
        currency TEXT NOT NULL CHECK (char_length(currency) = 3),
        gross_amount NUMERIC(20,6) NOT NULL CHECK (gross_amount >= 0),
        fees_amount NUMERIC(20,6) NOT NULL CHECK (fees_amount >= 0),
        net_amount NUMERIC(20,6) NOT NULL,
        status TEXT NOT NULL DEFAULT 'received',
        settled_at TIMESTAMPTZ,
        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(provider, external_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_finance_settlements_date ON finance_settlements(settled_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS finance_reconciliations (
        id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL,
        source_id TEXT NOT NULL,
        target_type TEXT NOT NULL DEFAULT '',
        target_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK (status IN ('matched','partial_match','mismatch','missing_document','ambiguous','review_required')),
        currency TEXT NOT NULL,
        matched_amount NUMERIC(20,6) NOT NULL DEFAULT 0,
        details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_finance_reconciliations_source ON finance_reconciliations(source_type, source_id, created_at DESC)",
    # --- Catalog Intelligence / Selection / Export Dry-Run (Slice 1) ---
    """
    CREATE TABLE IF NOT EXISTS pi_scoring_configs (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        is_default BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pi_product_scores (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL DEFAULT '', sku TEXT NOT NULL, score NUMERIC(6,2) NOT NULL DEFAULT 0,
        eligible BOOLEAN NOT NULL DEFAULT FALSE, criteria_json JSONB NOT NULL DEFAULT '{}'::jsonb, exclusions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        reasons_json JSONB NOT NULL DEFAULT '[]'::jsonb, config_json JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pi_product_scores_sku_created ON pi_product_scores(sku, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_pi_product_scores_run ON pi_product_scores(run_id)",
    """
    CREATE TABLE IF NOT EXISTS pi_selection_runs (
        id TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'automatic', status TEXT NOT NULL DEFAULT 'completed',
        target_count INTEGER NOT NULL DEFAULT 600, min_count INTEGER NOT NULL DEFAULT 500, max_count INTEGER NOT NULL DEFAULT 750,
        selected_count INTEGER NOT NULL DEFAULT 0, eligible_count INTEGER NOT NULL DEFAULT 0, total_count INTEGER NOT NULL DEFAULT 0,
        config_json JSONB NOT NULL DEFAULT '{}'::jsonb, report_json JSONB NOT NULL DEFAULT '{}'::jsonb, error_message TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), completed_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pi_selection_runs_created ON pi_selection_runs(created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS pi_selection_items (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sku TEXT NOT NULL, rank INTEGER NOT NULL DEFAULT 0, score NUMERIC(6,2) NOT NULL DEFAULT 0,
        selected BOOLEAN NOT NULL DEFAULT FALSE, decision_reason TEXT NOT NULL DEFAULT '', exclusions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pi_selection_items_run ON pi_selection_items(run_id, selected, rank)",
    "CREATE INDEX IF NOT EXISTS idx_pi_selection_items_sku ON pi_selection_items(sku)",
    """
    CREATE TABLE IF NOT EXISTS pi_export_jobs (
        id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'dry_run', target TEXT NOT NULL DEFAULT 'shopify',
        status TEXT NOT NULL DEFAULT 'pending', selection_run_id TEXT NOT NULL DEFAULT '', requested_count INTEGER NOT NULL DEFAULT 0, processed_count INTEGER NOT NULL DEFAULT 0,
        success_count INTEGER NOT NULL DEFAULT 0, failed_count INTEGER NOT NULL DEFAULT 0, payload_json JSONB NOT NULL DEFAULT '{}'::jsonb, result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        error_message TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '', approved_by TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), completed_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pi_export_jobs_status ON pi_export_jobs(status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_pi_export_jobs_idem ON pi_export_jobs(idempotency_key)"



    """
    CREATE TABLE IF NOT EXISTS product_analysis_runs (
        run_id TEXT PRIMARY KEY,
        sku TEXT NOT NULL DEFAULT '',
        channel TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'completed',
        model_combo TEXT NOT NULL DEFAULT '',
        policy_version TEXT NOT NULL DEFAULT 'v1',
        summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_analysis_results (
        id BIGSERIAL PRIMARY KEY,
        run_id TEXT NOT NULL DEFAULT '',
        sku TEXT NOT NULL,
        channel TEXT NOT NULL DEFAULT 'shopware',
        decision TEXT NOT NULL DEFAULT 'review_required',
        total_score NUMERIC(8,2) NOT NULL DEFAULT 0,
        confidence NUMERIC(8,2) NOT NULL DEFAULT 0,
        scorecard_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        reasons_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        signals_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        modifications_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        expires_at TIMESTAMPTZ,
        source TEXT NOT NULL DEFAULT 'product_intelligence',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_export_approvals (
        sku TEXT NOT NULL,
        channel TEXT NOT NULL,
        decision TEXT NOT NULL DEFAULT 'review_required',
        score NUMERIC(8,2),
        reasons_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        modifications_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        source TEXT NOT NULL DEFAULT 'manual',
        actor TEXT NOT NULL DEFAULT 'system',
        run_id TEXT NOT NULL DEFAULT '',
        expires_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (sku, channel)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_modifications (
        id BIGSERIAL PRIMARY KEY,
        run_id TEXT NOT NULL DEFAULT '',
        sku TEXT NOT NULL,
        channel TEXT NOT NULL DEFAULT '',
        field TEXT NOT NULL DEFAULT '',
        before_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        after_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        action TEXT NOT NULL DEFAULT 'suggest',
        status TEXT NOT NULL DEFAULT 'suggested',
        reason TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL DEFAULT 'system',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_market_signals (
        id BIGSERIAL PRIMARY KEY,
        sku TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        metric TEXT NOT NULL DEFAULT '',
        value NUMERIC(14,4) NOT NULL DEFAULT 0,
        period TEXT NOT NULL DEFAULT '',
        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_ab_tests (
        id TEXT PRIMARY KEY,
        sku TEXT NOT NULL DEFAULT '',
        channel TEXT NOT NULL DEFAULT '',
        variant_a_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        variant_b_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'draft',
        winner TEXT NOT NULL DEFAULT '',
        metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,

)

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    is_secret INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0, 1)),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    external_matterhorn_id TEXT NOT NULL DEFAULT '',
    parent_sku TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    brand TEXT NOT NULL DEFAULT '',
    manufacturer TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    short_description TEXT NOT NULL DEFAULT '',
    raw_description TEXT NOT NULL DEFAULT '',
    ai_title TEXT NOT NULL DEFAULT '',
    ai_description_html TEXT NOT NULL DEFAULT '',
    ai_tags_json TEXT NOT NULL DEFAULT '[]',
    seo_title TEXT NOT NULL DEFAULT '',
    meta_description TEXT NOT NULL DEFAULT '',
    slug TEXT NOT NULL DEFAULT '',
    canonical_url TEXT NOT NULL DEFAULT '',
    supplier_price REAL NOT NULL DEFAULT 0,
    purchase_price REAL NOT NULL DEFAULT 0,
    sale_price REAL NOT NULL DEFAULT 0,
    compare_at_price REAL NOT NULL DEFAULT 0,
    tax_rate REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'EUR',
    stock INTEGER NOT NULL DEFAULT 0,
    stock_quantity INTEGER NOT NULL DEFAULT 0,
    reserved_quantity INTEGER NOT NULL DEFAULT 0,
    available_quantity INTEGER NOT NULL DEFAULT 0,
    reorder_level INTEGER NOT NULL DEFAULT 0,
    lead_time_days INTEGER NOT NULL DEFAULT 0,
    supplier_stock INTEGER NOT NULL DEFAULT 0,
    warehouse_location TEXT NOT NULL DEFAULT '',
    bin_code TEXT NOT NULL DEFAULT '',
    ean TEXT NOT NULL DEFAULT '',
    barcode TEXT NOT NULL DEFAULT '',
    requires_serial_number INTEGER NOT NULL DEFAULT 0,
    is_fragile INTEGER NOT NULL DEFAULT 0,
    is_hazmat INTEGER NOT NULL DEFAULT 0,
    weight_net_kg REAL NOT NULL DEFAULT 0,
    weight_gross_kg REAL NOT NULL DEFAULT 0,
    length_cm REAL NOT NULL DEFAULT 0,
    width_cm REAL NOT NULL DEFAULT 0,
    height_cm REAL NOT NULL DEFAULT 0,
    hs_code TEXT NOT NULL DEFAULT '',
    country_of_origin TEXT NOT NULL DEFAULT '',
    main_image_url TEXT NOT NULL DEFAULT '',
    gallery_json TEXT NOT NULL DEFAULT '[]',
    videos_json TEXT NOT NULL DEFAULT '[]',
    documents_json TEXT NOT NULL DEFAULT '[]',
    source_system TEXT NOT NULL DEFAULT '',
    raw_source_data_json TEXT NOT NULL DEFAULT '{}',
    marketplace_mappings_json TEXT NOT NULL DEFAULT '{}',
    is_bundle INTEGER NOT NULL DEFAULT 0,
    has_variants INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    source_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_status_stock ON products(status, stock, is_bundle);
CREATE TABLE IF NOT EXISTS bundles (
    bundle_sku TEXT PRIMARY KEY REFERENCES products(sku) ON DELETE CASCADE,
    component_skus_json TEXT NOT NULL,
    title TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    content_type TEXT NOT NULL,
    content_text TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    ai_generated INTEGER NOT NULL DEFAULT 0 CHECK (ai_generated IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(product_sku, channel, content_type)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL REFERENCES products(sku),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    marketplace TEXT NOT NULL,
    customer_reference TEXT NOT NULL DEFAULT '',
    payment_status TEXT NOT NULL DEFAULT 'pending' CHECK (payment_status IN ('pending', 'paid', 'failed', 'refunded')),
    fulfillment_locked INTEGER NOT NULL DEFAULT 0 CHECK (fulfillment_locked IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'received' CHECK (status IN ('received', 'reserved', 'dispatching', 'dispatched', 'failed', 'cancelled')),
    tracking_number TEXT NOT NULL DEFAULT '',
    tracking_url TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status_updated ON orders(status, updated_at DESC);
CREATE TABLE IF NOT EXISTS bi_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    metric_label TEXT NOT NULL DEFAULT '',
    dimensions_json TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR')),
    event TEXT NOT NULL,
    message TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_system_logs_created_at ON system_logs(created_at DESC);
-- ── Marketplace Command Center (SQLite) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS marketplaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    connector_type TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'inactive',
    config_json TEXT NOT NULL DEFAULT '{}',
    credentials_json TEXT NOT NULL DEFAULT '{}',
    currency TEXT NOT NULL DEFAULT 'EUR',
    country TEXT NOT NULL DEFAULT 'DE',
    language TEXT NOT NULL DEFAULT 'de-DE',
    min_score REAL NOT NULL DEFAULT 70,
    target_products INTEGER NOT NULL DEFAULT 0,
    min_products INTEGER NOT NULL DEFAULT 0,
    max_products INTEGER NOT NULL DEFAULT 0,
    require_available_stock INTEGER NOT NULL DEFAULT 1,
    require_valid_images INTEGER NOT NULL DEFAULT 1,
    require_valid_pricing INTEGER NOT NULL DEFAULT 1,
    require_category_mapping INTEGER NOT NULL DEFAULT 0,
    sync_interval_minutes INTEGER NOT NULL DEFAULT 60,
    last_sync_at TEXT,
    last_health_at TEXT,
    last_health_status TEXT NOT NULL DEFAULT '',
    last_health_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marketplaces_type ON marketplaces(connector_type);
CREATE INDEX IF NOT EXISTS idx_marketplaces_status ON marketplaces(status);
CREATE TABLE IF NOT EXISTS marketplace_connections (
    id TEXT PRIMARY KEY,
    marketplace_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'disconnected',
    config_json TEXT NOT NULL DEFAULT '{}',
    credentials_json TEXT NOT NULL DEFAULT '{}',
    last_test_at TEXT,
    last_test_status TEXT NOT NULL DEFAULT '',
    last_test_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marketplace_connections_mp ON marketplace_connections(marketplace_id);
CREATE TABLE IF NOT EXISTS marketplace_selections (
    id TEXT PRIMARY KEY,
    marketplace_id TEXT NOT NULL,
    product_sku TEXT NOT NULL,
    selection_status TEXT NOT NULL DEFAULT 'NOT_SELECTED',
    approval_status TEXT NOT NULL DEFAULT 'PENDING',
    score REAL NOT NULL DEFAULT 0,
    eligible INTEGER NOT NULL DEFAULT 0,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    blockers_json TEXT NOT NULL DEFAULT '[]',
    recommendations_json TEXT NOT NULL DEFAULT '[]',
    criteria_json TEXT NOT NULL DEFAULT '{}',
    selected_by TEXT NOT NULL DEFAULT 'ai',
    selected_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(marketplace_id, product_sku)
);
CREATE INDEX IF NOT EXISTS idx_marketplace_selections_mp ON marketplace_selections(marketplace_id);
CREATE INDEX IF NOT EXISTS idx_marketplace_selections_sku ON marketplace_selections(product_sku);
CREATE TABLE IF NOT EXISTS marketplace_products (
    id TEXT PRIMARY KEY,
    marketplace_id TEXT NOT NULL,
    product_sku TEXT NOT NULL,
    external_product_id TEXT NOT NULL DEFAULT '',
    canonical_product_json TEXT NOT NULL DEFAULT '{}',
    marketplace_content_json TEXT NOT NULL DEFAULT '{}',
    marketplace_pricing_json TEXT NOT NULL DEFAULT '{}',
    marketplace_category_id TEXT NOT NULL DEFAULT '',
    attribute_mapping_json TEXT NOT NULL DEFAULT '{}',
    listing_status TEXT NOT NULL DEFAULT 'NOT_LISTED',
    sync_status TEXT NOT NULL DEFAULT 'never',
    last_error TEXT NOT NULL DEFAULT '',
    last_synced_at TEXT,
    last_exported_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(marketplace_id, product_sku)
);
CREATE INDEX IF NOT EXISTS idx_marketplace_products_mp ON marketplace_products(marketplace_id);
CREATE INDEX IF NOT EXISTS idx_marketplace_products_sku ON marketplace_products(product_sku);
CREATE INDEX IF NOT EXISTS idx_marketplace_products_status ON marketplace_products(listing_status);
CREATE TABLE IF NOT EXISTS marketplace_listings (
    id TEXT PRIMARY KEY,
    marketplace_id TEXT NOT NULL,
    product_sku TEXT NOT NULL,
    external_listing_id TEXT NOT NULL DEFAULT '',
    listing_status TEXT NOT NULL DEFAULT 'NOT_LISTED',
    last_status_check_at TEXT,
    status_source TEXT NOT NULL DEFAULT '',
    raw_status_json TEXT NOT NULL DEFAULT '{}',
    url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(marketplace_id, product_sku)
);
CREATE INDEX IF NOT EXISTS idx_marketplace_listings_mp ON marketplace_listings(marketplace_id);
CREATE TABLE IF NOT EXISTS marketplace_export_jobs (
    id TEXT PRIMARY KEY,
    marketplace_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'dry_run',
    status TEXT NOT NULL DEFAULT 'pending',
    requested_by TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at TEXT,
    total_items INTEGER NOT NULL DEFAULT 0,
    success_items INTEGER NOT NULL DEFAULT 0,
    failed_items INTEGER NOT NULL DEFAULT 0,
    skipped_items INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marketplace_export_jobs_mp ON marketplace_export_jobs(marketplace_id);
CREATE INDEX IF NOT EXISTS idx_marketplace_export_jobs_status ON marketplace_export_jobs(status);
CREATE TABLE IF NOT EXISTS marketplace_export_items (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    marketplace_id TEXT NOT NULL,
    product_sku TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'export',
    status TEXT NOT NULL DEFAULT 'pending',
    external_id TEXT NOT NULL DEFAULT '',
    request_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marketplace_export_items_job ON marketplace_export_items(job_id);
CREATE INDEX IF NOT EXISTS idx_marketplace_export_items_mp_sku ON marketplace_export_items(marketplace_id, product_sku);
CREATE TABLE IF NOT EXISTS marketplace_sync_jobs (
    id TEXT PRIMARY KEY,
    marketplace_id TEXT NOT NULL,
    job_type TEXT NOT NULL DEFAULT 'sync',
    status TEXT NOT NULL DEFAULT 'pending',
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marketplace_sync_jobs_mp ON marketplace_sync_jobs(marketplace_id);
CREATE TABLE IF NOT EXISTS marketplace_audit_events (
    id TEXT PRIMARY KEY,
    marketplace_id TEXT NOT NULL DEFAULT '',
    product_sku TEXT NOT NULL DEFAULT '',
    job_id TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marketplace_audit_mp ON marketplace_audit_events(marketplace_id);
CREATE INDEX IF NOT EXISTS idx_marketplace_audit_sku ON marketplace_audit_events(product_sku);
CREATE INDEX IF NOT EXISTS idx_marketplace_audit_created ON marketplace_audit_events(created_at DESC);
CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT '',
    feed_url TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    imported INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    created_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    variants_linked INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    error_message TEXT NOT NULL DEFAULT '',
    summary_json TEXT NOT NULL DEFAULT '{}',
    protocol_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL DEFAULT '',
    parent_id TEXT NOT NULL DEFAULT '',
    external_matterhorn_id TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    source_system TEXT NOT NULL DEFAULT '',
    marketplace_mappings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(name, parent_id, source_system)
);
CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id);
CREATE INDEX IF NOT EXISTS idx_categories_external ON categories(external_matterhorn_id);
CREATE TABLE IF NOT EXISTS product_categories (
    product_sku TEXT NOT NULL,
    category_id TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    marketplace_mappings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY(product_sku, category_id)
);
CREATE INDEX IF NOT EXISTS idx_product_categories_category ON product_categories(category_id);
CREATE TABLE IF NOT EXISTS product_variants (
    id TEXT PRIMARY KEY,
    parent_sku TEXT NOT NULL,
    sku TEXT NOT NULL UNIQUE,
    external_matterhorn_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    ean TEXT NOT NULL DEFAULT '',
    barcode TEXT NOT NULL DEFAULT '',
    supplier_price REAL NOT NULL DEFAULT 0,
    sale_price REAL NOT NULL DEFAULT 0,
    stock_quantity INTEGER NOT NULL DEFAULT 0,
    reserved_quantity INTEGER NOT NULL DEFAULT 0,
    available_quantity INTEGER NOT NULL DEFAULT 0,
    weight_net_kg REAL NOT NULL DEFAULT 0,
    weight_gross_kg REAL NOT NULL DEFAULT 0,
    length_cm REAL NOT NULL DEFAULT 0,
    width_cm REAL NOT NULL DEFAULT 0,
    height_cm REAL NOT NULL DEFAULT 0,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    raw_source_data_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    source_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_variants_parent ON product_variants(parent_sku);
CREATE INDEX IF NOT EXISTS idx_product_variants_external ON product_variants(external_matterhorn_id);
CREATE TABLE IF NOT EXISTS product_attributes (
    id TEXT PRIMARY KEY,
    product_sku TEXT NOT NULL,
    variant_sku TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    unit TEXT NOT NULL DEFAULT '',
    is_filterable INTEGER NOT NULL DEFAULT 0,
    is_variant INTEGER NOT NULL DEFAULT 0,
    source_field TEXT NOT NULL DEFAULT '',
    extra_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(product_sku, variant_sku, name, value)
);
CREATE INDEX IF NOT EXISTS idx_product_attributes_product ON product_attributes(product_sku);
CREATE INDEX IF NOT EXISTS idx_product_attributes_name ON product_attributes(name);
CREATE TABLE IF NOT EXISTS product_media (
    id TEXT PRIMARY KEY,
    product_sku TEXT NOT NULL,
    variant_sku TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT 'image',
    url TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    alt_text TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_media_product ON product_media(product_sku, media_type);

CREATE TABLE IF NOT EXISTS shopware_connections (
    id TEXT PRIMARY KEY,
    shop_name TEXT NOT NULL,
    shop_url TEXT NOT NULL,
    shopware_version TEXT NOT NULL DEFAULT '6.0',
    api_client_id TEXT NOT NULL,
    api_client_secret_encrypted TEXT NOT NULL,
    sales_channel_id TEXT NOT NULL DEFAULT '',
    default_language TEXT NOT NULL DEFAULT 'de-DE',
    default_currency TEXT NOT NULL DEFAULT 'EUR',
    default_tax_rate REAL NOT NULL DEFAULT 19.00,
    default_supplier TEXT NOT NULL DEFAULT '',
    default_shipping_config_json TEXT NOT NULL DEFAULT '{}',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    last_health_check TEXT,
    last_health_status TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shopware_product_mappings (
    id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    matterhorn_field TEXT NOT NULL,
    shopware_field TEXT NOT NULL,
    mapping_type TEXT NOT NULL CHECK (mapping_type IN ('direct', 'transform', 'fallback', 'conditional', 'concatenate', 'clean', 'translate')),
    mapping_config_json TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shopware_export_queue (
    id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    product_sku TEXT NOT NULL,
    export_mode TEXT NOT NULL CHECK (export_mode IN ('publish', 'update', 'variant_update', 'price_update', 'stock_update', 'image_update', 'description_update', 'seo_update', 'category_update', 'full_sync')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'validation', 'release_required', 'exporting', 'completed', 'error', 'retry')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS competitors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    shop_url TEXT NOT NULL DEFAULT '',
    marketplace TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT 'DE',
    currency TEXT NOT NULL DEFAULT 'EUR',
    detected_categories_json TEXT NOT NULL DEFAULT '[]',
    detected_brands_json TEXT NOT NULL DEFAULT '[]',
    last_successful_research TEXT,
    products_found INTEGER NOT NULL DEFAULT 0,
    average_price REAL,
    price_positioning TEXT NOT NULL DEFAULT 'unknown' CHECK (price_positioning IN ('cheap', 'average', 'expensive', 'unknown')),
    availability_status TEXT NOT NULL DEFAULT 'unknown' CHECK (availability_status IN ('good', 'limited', 'poor', 'unknown')),
    data_source TEXT NOT NULL DEFAULT '',
    reliability_score REAL NOT NULL DEFAULT 0.0 CHECK (reliability_score >= 0 AND reliability_score <= 1.0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS competitor_products (
    id TEXT PRIMARY KEY,
    competitor_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_name TEXT NOT NULL DEFAULT '',
    ean TEXT NOT NULL DEFAULT '',
    sku TEXT NOT NULL DEFAULT '',
    product_name TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    price REAL NOT NULL DEFAULT 0,
    shipping_cost REAL NOT NULL DEFAULT 0,
    total_price REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'EUR',
    availability TEXT NOT NULL DEFAULT '',
    last_checked TEXT NOT NULL DEFAULT '',
    data_source TEXT NOT NULL DEFAULT '',
    match_score INTEGER NOT NULL DEFAULT 0 CHECK (match_score >= 0 AND match_score <= 100),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_matches (
    id TEXT PRIMARY KEY,
    own_product_sku TEXT NOT NULL,
    competitor_product_id TEXT NOT NULL,
    match_score INTEGER NOT NULL CHECK (match_score >= 0 AND match_score <= 100),
    match_stage TEXT NOT NULL CHECK (match_stage IN ('exact_ean', 'sku_model', 'brand_name_attrs', 'image_similarity', 'semantic', 'manual')),
    match_reason TEXT NOT NULL DEFAULT '',
    ai_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (ai_confirmed IN (0, 1)),
    ai_confidence REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS competitor_price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_product_id TEXT NOT NULL,
    price REAL NOT NULL DEFAULT 0,
    shipping_cost REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'EUR',
    availability TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL DEFAULT '',
    data_source TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS monitoring_jobs (
    id TEXT PRIMARY KEY,
    competitor_id TEXT NOT NULL,
    competitor_product_id TEXT,
    interval_hours INTEGER NOT NULL CHECK (interval_hours IN (6, 12, 24, 168)),
    next_run TEXT NOT NULL,
    last_run TEXT,
    last_price REAL,
    last_availability TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'error', 'disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_alerts (
    id TEXT PRIMARY KEY,
    alert_type TEXT NOT NULL CHECK (alert_type IN ('competitor_lower', 'competitor_higher', 'out_of_stock', 'new_competitor', 'price_above_market', 'margin_below_min', 'special_offer')),
    own_product_sku TEXT,
    competitor_id TEXT,
    competitor_product_id TEXT,
    threshold_value REAL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    last_triggered TEXT,
    notification_channels_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pricing_recommendations (
    id TEXT PRIMARY KEY,
    own_product_sku TEXT NOT NULL,
    current_price REAL NOT NULL DEFAULT 0,
    recommended_price REAL NOT NULL DEFAULT 0,
    market_average REAL NOT NULL DEFAULT 0,
    market_min REAL NOT NULL DEFAULT 0,
    expected_margin REAL NOT NULL DEFAULT 0,
    contribution_margin REAL NOT NULL DEFAULT 0,
    price_deviation REAL NOT NULL DEFAULT 0,
    strategy TEXT NOT NULL CHECK (strategy IN ('premium_positioning', 'market_average', 'competitive_pricing', 'margin_optimization', 'clearance')),
    justification TEXT NOT NULL DEFAULT '',
    data_quality TEXT NOT NULL DEFAULT '',
    risk_indicator TEXT NOT NULL DEFAULT '',
    is_approved INTEGER NOT NULL DEFAULT 0 CHECK (is_approved IN (0, 1)),
    applied_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_runs (
    id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    products_found INTEGER NOT NULL DEFAULT 0,
    matches_created INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pricing_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    rule_type TEXT NOT NULL CHECK (rule_type IN ('global', 'brand', 'category', 'product', 'sales_channel')),
    target_entity_id TEXT NOT NULL DEFAULT '',
    min_margin REAL NOT NULL DEFAULT 0,
    target_margin REAL,
    markup_percent REAL,
    min_selling_price REAL,
    max_selling_price REAL,
    strategy TEXT NOT NULL DEFAULT 'market_average',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- ── Google Merchant Center Tables ──
CREATE TABLE IF NOT EXISTS google_accounts (
    id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL UNIQUE,
    account_name TEXT NOT NULL,
    google_cloud_project_id TEXT NOT NULL,
    account_status TEXT NOT NULL DEFAULT 'pending' CHECK (account_status IN ('pending', 'active', 'suspended', 'disconnected')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    oauth_token_encrypted TEXT NOT NULL,
    oauth_refresh_token_encrypted TEXT,
    service_account_encrypted TEXT,
    scopes_json TEXT NOT NULL DEFAULT '[]',
    token_expires_at TEXT,
    last_token_validation TEXT,
    last_token_status TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS google_credentials (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    credential_type TEXT NOT NULL CHECK (credential_type IN ('oauth2', 'service_account')),
    credential_name TEXT NOT NULL,
    encrypted_value TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS google_data_sources (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    data_source_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'products',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'error')),
    primary_country TEXT NOT NULL DEFAULT 'DE',
    primary_language TEXT NOT NULL DEFAULT 'de',
    primary_currency TEXT NOT NULL DEFAULT 'EUR',
    feed_label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, data_source_id)
);
CREATE TABLE IF NOT EXISTS google_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    is_secret INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0, 1)),
    category TEXT NOT NULL DEFAULT 'general' CHECK (category IN ('general', 'export', 'sync', 'targeting', 'validation')),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS google_product_mappings (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    matterhorn_field TEXT NOT NULL,
    google_field TEXT NOT NULL,
    mapping_type TEXT NOT NULL CHECK (mapping_type IN ('direct', 'transform', 'fallback', 'conditional', 'concatenate', 'clean')),
    mapping_config_json TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS google_product_exports (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    product_sku TEXT NOT NULL,
    google_product_id TEXT NOT NULL DEFAULT '',
    offer_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'validation', 'exporting', 'completed', 'error', 'withdrawn', 'retry')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(product_sku, offer_id)
);
CREATE TABLE IF NOT EXISTS google_sync_jobs (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    job_type TEXT NOT NULL CHECK (job_type IN ('inventory', 'price', 'full_sync', 'product_update', 'withdrawal')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'error')),
    total_products INTEGER NOT NULL DEFAULT 0,
    processed_products INTEGER NOT NULL DEFAULT 0,
    failed_products INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    completed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS google_sync_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    product_sku TEXT NOT NULL,
    error_type TEXT NOT NULL,
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL DEFAULT '',
    resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS google_product_diagnostics (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    product_sku TEXT NOT NULL,
    google_product_id TEXT NOT NULL DEFAULT '',
    diagnosis_status TEXT NOT NULL CHECK (diagnosis_status IN ('active', 'disapproved', 'pending', 'limited')),
    issues_json TEXT NOT NULL DEFAULT '[]',
    last_checked TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS google_performance_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    metric_date TEXT NOT NULL,
    impressions INTEGER NOT NULL DEFAULT 0,
    clicks INTEGER NOT NULL DEFAULT 0,
    ctr REAL NOT NULL DEFAULT 0,
    cpc REAL NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    conversions INTEGER NOT NULL DEFAULT 0,
    conversion_value REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS google_audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    target_type TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
-- ── OmniRoute Control Center Tables ──
CREATE TABLE IF NOT EXISTS ai_providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider_type TEXT NOT NULL CHECK (provider_type IN ('text', 'image', 'video', 'embedding', 'multimodal')),
    api_endpoint TEXT NOT NULL,
    api_key_encrypted TEXT NOT NULL,
    auth_type TEXT NOT NULL DEFAULT 'bearer' CHECK (auth_type IN ('bearer', 'api_key', 'oauth2')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    priority INTEGER NOT NULL DEFAULT 100,
    rate_limit_per_minute INTEGER NOT NULL DEFAULT 60,
    timeout_seconds INTEGER NOT NULL DEFAULT 30,
    max_retries INTEGER NOT NULL DEFAULT 3,
    retry_delay_seconds INTEGER NOT NULL DEFAULT 1,
    circuit_breaker_threshold INTEGER NOT NULL DEFAULT 5,
    circuit_breaker_timeout_seconds INTEGER NOT NULL DEFAULT 300,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    health_status TEXT NOT NULL DEFAULT 'unknown' CHECK (health_status IN ('healthy', 'degraded', 'unhealthy', 'unknown')),
    last_health_check TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_provider_credentials (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    credential_name TEXT NOT NULL,
    encrypted_value TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_models (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL DEFAULT '',
    model_category TEXT NOT NULL DEFAULT 'general' CHECK (model_category IN ('text', 'image', 'video', 'embedding', 'multimodal')),
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    context_window INTEGER NOT NULL DEFAULT 4096,
    input_price_per_1k REAL NOT NULL DEFAULT 0,
    output_price_per_1k REAL NOT NULL DEFAULT 0,
    image_price_per_image REAL NOT NULL DEFAULT 0,
    rate_limit_per_minute INTEGER NOT NULL DEFAULT 60,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    is_discoverable INTEGER NOT NULL DEFAULT 1 CHECK (is_discoverable IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider_id, model_id)
);
CREATE TABLE IF NOT EXISTS ai_model_capabilities (
    id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    capability_name TEXT NOT NULL,
    capability_value TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'discovered' CHECK (source IN ('discovered', 'manual')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_routing_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    strategy TEXT NOT NULL CHECK (strategy IN ('lowest_cost', 'highest_quality', 'lowest_latency', 'provider_priority', 'capability_matching', 'weighted_distribution')),
    provider_priority_json TEXT NOT NULL DEFAULT '[]',
    model_category TEXT NOT NULL DEFAULT 'general',
    min_quality_score REAL NOT NULL DEFAULT 0.0,
    max_cost_per_1k REAL,
    max_latency_ms INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_routing_policies (
    id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    fallback_rule_id TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workflow_name)
);
CREATE TABLE IF NOT EXISTS ai_workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    trigger_event TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    max_executions_per_hour INTEGER NOT NULL DEFAULT 1000,
    budget_per_execution REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_workflow_configs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    step_name TEXT NOT NULL,
    model_id TEXT,
    provider_id TEXT,
    prompt_template TEXT NOT NULL DEFAULT '',
    parameters_json TEXT NOT NULL DEFAULT '{}',
    step_order INTEGER NOT NULL DEFAULT 0,
    is_required INTEGER NOT NULL DEFAULT 1 CHECK (is_required IN (0, 1)),
    on_failure TEXT NOT NULL DEFAULT 'stop' CHECK (on_failure IN ('stop', 'continue', 'retry', 'fallback')),
    max_retries INTEGER NOT NULL DEFAULT 3,
    timeout_seconds INTEGER NOT NULL DEFAULT 30,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    model_id TEXT,
    workflow_id TEXT,
    request_type TEXT NOT NULL,
    tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    images_generated INTEGER NOT NULL DEFAULT 0,
    videos_generated INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'success' CHECK (status IN ('success', 'error', 'retry', 'fallback')),
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_cost_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    model_id TEXT,
    workflow_id TEXT,
    date TEXT NOT NULL,
    total_cost_usd REAL NOT NULL DEFAULT 0,
    request_count INTEGER NOT NULL DEFAULT 0,
    token_count INTEGER NOT NULL DEFAULT 0,
    image_count INTEGER NOT NULL DEFAULT 0,
    video_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_budgets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    budget_type TEXT NOT NULL CHECK (budget_type IN ('daily', 'monthly', 'provider', 'workflow')),
    target_id TEXT NOT NULL DEFAULT '',
    amount_usd REAL NOT NULL DEFAULT 0,
    warning_threshold_percent INTEGER NOT NULL DEFAULT 80,
    action_on_exceed TEXT NOT NULL DEFAULT 'warn' CHECK (action_on_exceed IN ('warn', 'pause', 'fallback', 'block')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    reset_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_provider_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('healthy', 'degraded', 'unhealthy', 'unknown')),
    response_time_ms INTEGER NOT NULL DEFAULT 0,
    error_rate REAL NOT NULL DEFAULT 0,
    last_check TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS ai_request_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    model_id TEXT,
    workflow_id TEXT,
    request_id TEXT NOT NULL,
    prompt_hash TEXT NOT NULL DEFAULT '',
    request_payload_json TEXT NOT NULL DEFAULT '{}',
    response_payload_json TEXT NOT NULL DEFAULT '{}',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'success',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_error_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT,
    model_id TEXT,
    workflow_id TEXT,
    error_type TEXT NOT NULL,
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'error' CHECK (severity IN ('debug', 'info', 'warning', 'error', 'critical')),
    resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    resolved_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL DEFAULT 'system',
    action TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);


-- ── GTIN / Product Intelligence ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gtin_providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    endpoint TEXT NOT NULL DEFAULT '',
    auth_ref TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 100,
    rate_limit_per_min INTEGER NOT NULL DEFAULT 60,
    license_info TEXT NOT NULL DEFAULT '',
    supported_categories_json TEXT NOT NULL DEFAULT '[]',
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(name)
);
CREATE TABLE IF NOT EXISTS gtin_lookups (
    id TEXT PRIMARY KEY,
    gtin TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'done',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS gtin_enrichments (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL DEFAULT 'product',
    entity_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    original_value TEXT NOT NULL DEFAULT '',
    normalized_value TEXT NOT NULL DEFAULT '',
    source_provider TEXT NOT NULL DEFAULT '',
    source_record_id TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.0,
    trust_level TEXT NOT NULL DEFAULT 'C',
    verification_state TEXT NOT NULL DEFAULT 'adopted',
    license_info TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS gtin_conflicts (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL DEFAULT 'product',
    entity_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    candidates_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',
    resolution_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Multi-channel content versions ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS content_versions (
    id TEXT PRIMARY KEY,
    product_sku TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    model TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    content_json TEXT NOT NULL DEFAULT '{}',
    validation_json TEXT NOT NULL DEFAULT '{}',
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Core IAM / RBAC (incremental) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS iam_users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    is_superadmin INTEGER NOT NULL DEFAULT 0,
    mfa_required INTEGER NOT NULL DEFAULT 0,
    last_login_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS iam_roles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS iam_permissions (
    id TEXT PRIMARY KEY,
    role_name TEXT NOT NULL,
    permission TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(role_name, permission)
);
CREATE TABLE IF NOT EXISTS iam_user_roles (
    user_id TEXT NOT NULL,
    role_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, role_name)
);
CREATE TABLE IF NOT EXISTS iam_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_token_hash TEXT NOT NULL,
    ip_address TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    expires_at TEXT,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT
);


CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','succeeded','failed','cancelled','retry')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    run_at TEXT NOT NULL,
    locked_by TEXT NOT NULL DEFAULT '',
    locked_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_by TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_run_at ON jobs(status, run_at);
CREATE INDEX IF NOT EXISTS idx_jobs_type_created ON jobs(type, created_at);
CREATE TABLE IF NOT EXISTS scheduler_config (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_seconds INTEGER NOT NULL DEFAULT 3600,
    payload_json TEXT NOT NULL DEFAULT '{}',
    last_run_at TEXT,
    next_run_at TEXT,
    last_status TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS matterhorn_config (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS matterhorn_feeds (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    feed_url TEXT NOT NULL,
    feed_type TEXT NOT NULL DEFAULT 'xml',
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    last_preview_at TEXT,
    last_import_at TEXT,
    last_status TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS matterhorn_mappings (
    id TEXT PRIMARY KEY,
    source_field TEXT NOT NULL,
    target_field TEXT NOT NULL,
    transform TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS matterhorn_import_previews (
    id TEXT PRIMARY KEY,
    feed_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'created',
    summary_json TEXT NOT NULL DEFAULT '{}',
    items_json TEXT NOT NULL DEFAULT '[]',
    validation_json TEXT NOT NULL DEFAULT '{}',
    source_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_sources (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'general',
    enabled INTEGER NOT NULL DEFAULT 1, config_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_items (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'general', published_at TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_entries (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'general',
    tags_json TEXT NOT NULL DEFAULT '[]', source TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connector_accounts (
    id TEXT PRIMARY KEY, connector_id TEXT NOT NULL, account_name TEXT NOT NULL, marketplace TEXT NOT NULL DEFAULT 'DE',
    is_active INTEGER NOT NULL DEFAULT 1, is_sandbox INTEGER NOT NULL DEFAULT 0, credentials_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connector_mappings (
    id TEXT PRIMARY KEY, connector_id TEXT NOT NULL, canonical_field TEXT NOT NULL, channel_field TEXT NOT NULL,
    mapping_type TEXT NOT NULL DEFAULT 'direct', config_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connector_exports (
    id TEXT PRIMARY KEY, connector_id TEXT NOT NULL, account_id TEXT NOT NULL DEFAULT '', sku TEXT NOT NULL,
    remote_listing_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', payload_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS crm_customers (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    shopware_id TEXT NOT NULL DEFAULT '',
    amazon_id TEXT NOT NULL DEFAULT '',
    ebay_id TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crm_customers_email ON crm_customers(email);
CREATE TABLE IF NOT EXISTS crm_threads (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('shopware','amazon','ebay','email','messenger')),
    external_thread_id TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','pending','resolved','closed')),
    last_message_at TEXT,
    order_id TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(channel, external_thread_id)
);
CREATE TABLE IF NOT EXISTS crm_messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('shopware','amazon','ebay','email','messenger')),
    direction TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
    external_message_id TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    recipient TEXT NOT NULL DEFAULT '',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    sent_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'received',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(channel, idempotency_key)
);
CREATE TABLE IF NOT EXISTS crm_appointments (
    id TEXT PRIMARY KEY,
    customer_id TEXT,
    thread_id TEXT,
    order_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','confirmed','completed','cancelled','no_show')),
    channel TEXT NOT NULL DEFAULT 'email',
    location TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_agent_actions (
    id TEXT PRIMARY KEY,
    actor TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL DEFAULT '',
    args_json TEXT NOT NULL DEFAULT '{}',
    preview_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','executed','cancelled','failed')),
    requires_confirmation INTEGER NOT NULL DEFAULT 1,
    result_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    executed_at TEXT
);


-- ── Multimodal Creative Engine (foundation) ─────────────────────────────
CREATE TABLE IF NOT EXISTS creative_projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project_type TEXT NOT NULL DEFAULT 'general',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','planned','running','completed','failed','cancelled','review')),
    brief TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS creative_jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    job_type TEXT NOT NULL DEFAULT 'image_generation',
    capability TEXT NOT NULL DEFAULT 'image_generation',
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','planning','running','polling','succeeded','failed','cancelled','review_required')),
    provider_id TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    negative_prompt TEXT NOT NULL DEFAULT '',
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    cost_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    requires_review INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_creative_jobs_project ON creative_jobs(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_creative_jobs_status ON creative_jobs(status, created_at DESC);
CREATE TABLE IF NOT EXISTS media_assets (
    id TEXT PRIMARY KEY,
    asset_type TEXT NOT NULL DEFAULT 'image' CHECK (asset_type IN ('image','video','audio','file')),
    source_kind TEXT NOT NULL DEFAULT 'generated' CHECK (source_kind IN ('original','generated','edited','reference','upload')),
    immutable_original INTEGER NOT NULL DEFAULT 0,
    storage_path TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT '',
    width INTEGER,
    height INTEGER,
    duration_seconds REAL,
    size_bytes INTEGER,
    product_sku TEXT NOT NULL DEFAULT '',
    model_profile_id TEXT NOT NULL DEFAULT '',
    job_id TEXT,
    parent_asset_id TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_assets_product ON media_assets(product_sku);
CREATE INDEX IF NOT EXISTS idx_media_assets_sha ON media_assets(sha256);
CREATE INDEX IF NOT EXISTS idx_media_assets_job ON media_assets(job_id);
CREATE TABLE IF NOT EXISTS model_profiles (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    reference_images_json TEXT NOT NULL DEFAULT '[]',
    identity_profile_json TEXT NOT NULL DEFAULT '{}',
    body_profile_json TEXT NOT NULL DEFAULT '{}',
    style_profile_json TEXT NOT NULL DEFAULT '{}',
    preferred_locations_json TEXT NOT NULL DEFAULT '[]',
    preferred_perspectives_json TEXT NOT NULL DEFAULT '[]',
    posing_profiles_json TEXT NOT NULL DEFAULT '[]',
    prompt_templates_json TEXT NOT NULL DEFAULT '[]',
    rights_consent_json TEXT NOT NULL DEFAULT '{}',
    usage_allowed INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    history_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_fidelity_checks (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    output_asset_id TEXT NOT NULL DEFAULT '',
    product_sku TEXT NOT NULL DEFAULT '',
    original_asset_ids_json TEXT NOT NULL DEFAULT '[]',
    scores_json TEXT NOT NULL DEFAULT '{}',
    overall_score REAL NOT NULL DEFAULT 0,
    verdict TEXT NOT NULL DEFAULT 'pending' CHECK (verdict IN ('pending','pass','fail','manual_review')),
    critical_issues_json TEXT NOT NULL DEFAULT '[]',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fidelity_job ON product_fidelity_checks(job_id);

CREATE TABLE IF NOT EXISTS finance_documents (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    document_type TEXT NOT NULL CHECK (document_type IN ('invoice','credit_note','supplier_invoice','expense')),
    direction TEXT NOT NULL CHECK (direction IN ('receivable','payable')),
    order_reference TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL CHECK (length(currency) = 3),
    net_amount TEXT NOT NULL CHECK (CAST(net_amount AS REAL) >= 0),
    tax_amount TEXT NOT NULL CHECK (CAST(tax_amount AS REAL) >= 0),
    gross_amount TEXT NOT NULL CHECK (CAST(gross_amount AS REAL) >= 0),
    status TEXT NOT NULL DEFAULT 'issued',
    issued_at TEXT,
    due_at TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider, external_id)
);
CREATE INDEX IF NOT EXISTS idx_finance_documents_order ON finance_documents(order_reference, currency);
CREATE INDEX IF NOT EXISTS idx_finance_documents_currency_amount ON finance_documents(currency, gross_amount);
CREATE INDEX IF NOT EXISTS idx_finance_documents_issued ON finance_documents(issued_at DESC);
CREATE TABLE IF NOT EXISTS finance_transactions (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    transaction_type TEXT NOT NULL CHECK (transaction_type IN ('payment','refund','fee','payout','adjustment')),
    order_reference TEXT NOT NULL DEFAULT '',
    settlement_reference TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL CHECK (length(currency) = 3),
    amount TEXT NOT NULL CHECK (CAST(amount AS REAL) >= 0),
    occurred_at TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider, external_id)
);
CREATE INDEX IF NOT EXISTS idx_finance_transactions_order ON finance_transactions(order_reference, currency);
CREATE INDEX IF NOT EXISTS idx_finance_transactions_settlement ON finance_transactions(settlement_reference);
CREATE TABLE IF NOT EXISTS finance_settlements (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (length(currency) = 3),
    gross_amount TEXT NOT NULL CHECK (CAST(gross_amount AS REAL) >= 0),
    fees_amount TEXT NOT NULL CHECK (CAST(fees_amount AS REAL) >= 0),
    net_amount TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'received',
    settled_at TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider, external_id)
);
CREATE INDEX IF NOT EXISTS idx_finance_settlements_date ON finance_settlements(settled_at DESC);
CREATE TABLE IF NOT EXISTS finance_reconciliations (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('matched','partial_match','mismatch','missing_document','ambiguous','review_required')),
    currency TEXT NOT NULL,
    matched_amount TEXT NOT NULL DEFAULT '0',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_finance_reconciliations_source ON finance_reconciliations(source_type, source_id, created_at DESC);

-- Catalog Intelligence / Selection / Export Dry-Run (Slice 1)
CREATE TABLE IF NOT EXISTS pi_scoring_configs (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}', is_default INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pi_product_scores (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL DEFAULT '', sku TEXT NOT NULL, score REAL NOT NULL DEFAULT 0, eligible INTEGER NOT NULL DEFAULT 0,
    criteria_json TEXT NOT NULL DEFAULT '{}', exclusions_json TEXT NOT NULL DEFAULT '[]', reasons_json TEXT NOT NULL DEFAULT '[]', config_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pi_product_scores_sku_created ON pi_product_scores(sku, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pi_product_scores_run ON pi_product_scores(run_id);
CREATE TABLE IF NOT EXISTS pi_selection_runs (
    id TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'automatic', status TEXT NOT NULL DEFAULT 'completed', target_count INTEGER NOT NULL DEFAULT 600,
    min_count INTEGER NOT NULL DEFAULT 500, max_count INTEGER NOT NULL DEFAULT 750, selected_count INTEGER NOT NULL DEFAULT 0, eligible_count INTEGER NOT NULL DEFAULT 0,
    total_count INTEGER NOT NULL DEFAULT 0, config_json TEXT NOT NULL DEFAULT '{}', report_json TEXT NOT NULL DEFAULT '{}', error_message TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, completed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pi_selection_runs_created ON pi_selection_runs(created_at DESC);
CREATE TABLE IF NOT EXISTS pi_selection_items (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sku TEXT NOT NULL, rank INTEGER NOT NULL DEFAULT 0, score REAL NOT NULL DEFAULT 0, selected INTEGER NOT NULL DEFAULT 0,
    decision_reason TEXT NOT NULL DEFAULT '', exclusions_json TEXT NOT NULL DEFAULT '[]', snapshot_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pi_selection_items_run ON pi_selection_items(run_id, selected, rank);
CREATE INDEX IF NOT EXISTS idx_pi_selection_items_sku ON pi_selection_items(sku);
CREATE TABLE IF NOT EXISTS pi_export_jobs (
    id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'dry_run', target TEXT NOT NULL DEFAULT 'shopify',
    status TEXT NOT NULL DEFAULT 'pending', selection_run_id TEXT NOT NULL DEFAULT '', requested_count INTEGER NOT NULL DEFAULT 0, processed_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0, failed_count INTEGER NOT NULL DEFAULT 0, payload_json TEXT NOT NULL DEFAULT '{}', result_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '', approved_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pi_export_jobs_status ON pi_export_jobs(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pi_export_jobs_idem ON pi_export_jobs(idempotency_key);


CREATE TABLE IF NOT EXISTS product_analysis_runs (
    run_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'completed',
    model_combo TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL DEFAULT 'v1',
    summary_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS product_analysis_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL DEFAULT '',
    sku TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'shopware',
    decision TEXT NOT NULL DEFAULT 'review_required',
    total_score REAL NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    scorecard_json TEXT NOT NULL DEFAULT '{}',
    reasons_json TEXT NOT NULL DEFAULT '[]',
    signals_json TEXT NOT NULL DEFAULT '[]',
    modifications_json TEXT NOT NULL DEFAULT '[]',
    expires_at TEXT,
    source TEXT NOT NULL DEFAULT 'product_intelligence',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS product_export_approvals (
    sku TEXT NOT NULL,
    channel TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT 'review_required',
    score REAL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    modifications_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'manual',
    actor TEXT NOT NULL DEFAULT 'system',
    run_id TEXT NOT NULL DEFAULT '',
    expires_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (sku, channel)
);
CREATE TABLE IF NOT EXISTS product_modifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL DEFAULT '',
    sku TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    field TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    action TEXT NOT NULL DEFAULT 'suggest',
    status TEXT NOT NULL DEFAULT 'suggested',
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS product_market_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    metric TEXT NOT NULL DEFAULT '',
    value REAL NOT NULL DEFAULT 0,
    period TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS product_ab_tests (
    id TEXT PRIMARY KEY,
    sku TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    variant_a_json TEXT NOT NULL DEFAULT '{}',
    variant_b_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',
    winner TEXT NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

"""


async def get_pg_pool() -> Any:
    """Create a compact asyncpg pool, retrying while PostgreSQL starts."""
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    if asyncpg is None:
        raise DatabaseError("asyncpg is unavailable; install project requirements first.")
    if not uses_postgres():
        raise DatabaseError("DATABASE_URL must be postgresql://... for PostgreSQL mode.")

    for attempt in range(1, POSTGRES_RETRY_ATTEMPTS + 1):
        try:
            _pg_pool = await asyncpg.create_pool(
                dsn=DATABASE_URL,
                min_size=1,
                max_size=5,
                command_timeout=30,
                server_settings={"application_name": "omni-core"},
            )
            logger.info("PostgreSQL connection pool established on attempt %d.", attempt)
            return _pg_pool
        except Exception as exc:
            if attempt == POSTGRES_RETRY_ATTEMPTS:
                raise DatabaseError("PostgreSQL was not ready after %d attempts." % attempt) from exc
            logger.warning("PostgreSQL not ready (%d/%d): %s", attempt, POSTGRES_RETRY_ATTEMPTS, exc)
            await asyncio.sleep(POSTGRES_RETRY_DELAY_SECONDS)
    raise AssertionError("unreachable")


async def close_pg_pool() -> None:
    """Close the PostgreSQL pool if it has been created."""
    global _pg_pool
    if _pg_pool is not None:
        await _pg_pool.close()
        _pg_pool = None


async def _connect_sqlite() -> aiosqlite.Connection:
    """Open one configured SQLite WAL connection."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(DATABASE_PATH), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    await conn.execute("PRAGMA foreign_keys=ON")
    return conn


@asynccontextmanager
async def get_connection() -> AsyncIterator[aiosqlite.Connection]:
    """Yield a SQLite connection for compatibility-only SQLite-specific callers."""
    if uses_postgres():
        raise DatabaseError("SQLite connection requested while PostgreSQL is configured.")
    conn = await _connect_sqlite()
    try:
        yield conn
    finally:
        await conn.close()


async def close_connection(conn: aiosqlite.Connection | None) -> None:
    """Close a SQLite connection; retained for backwards compatibility."""
    if conn is not None:
        await conn.close()


async def init_db() -> None:
    """Run automatic schema migrations for the selected storage backend."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            for statement in POSTGRES_SCHEMA:
                try:
                    await conn.execute(statement)
                except Exception as exc:
                    # Tolerate already-applied/evolved-schema cases; compat migrations follow.
                    logger.debug("Schema statement skipped: %s (%s)", str(exc).splitlines()[0], statement[:60])
            # idempotent compatibility migrations for evolved schemas
            compat = [
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS external_matterhorn_id TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS parent_sku TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS brand TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS manufacturer TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS short_description TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS seo_title TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS meta_description TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS slug TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS canonical_url TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS purchase_price NUMERIC(12,4) NOT NULL DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS sale_price NUMERIC(12,4) NOT NULL DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS compare_at_price NUMERIC(12,4) NOT NULL DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS tax_rate NUMERIC(7,4) NOT NULL DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'EUR'",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS stock_quantity INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS reserved_quantity INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS available_quantity INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS reorder_level INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS lead_time_days INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS supplier_stock INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS warehouse_location TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS bin_code TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS ean TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS barcode TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS requires_serial_number BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS is_fragile BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS is_hazmat BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS weight_net_kg NUMERIC(12,4) NOT NULL DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS weight_gross_kg NUMERIC(12,4) NOT NULL DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS length_cm NUMERIC(12,4) NOT NULL DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS width_cm NUMERIC(12,4) NOT NULL DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS height_cm NUMERIC(12,4) NOT NULL DEFAULT 0",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS hs_code TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS country_of_origin TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS main_image_url TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS gallery_json JSONB NOT NULL DEFAULT '[]'::jsonb",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS videos_json JSONB NOT NULL DEFAULT '[]'::jsonb",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS documents_json JSONB NOT NULL DEFAULT '[]'::jsonb",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS source_system TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS raw_source_data_json JSONB NOT NULL DEFAULT '{}'::jsonb",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS marketplace_mappings_json JSONB NOT NULL DEFAULT '{}'::jsonb",
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS has_variants BOOLEAN NOT NULL DEFAULT FALSE",
                "UPDATE products SET stock_quantity = COALESCE(NULLIF(stock_quantity,0), stock, 0) WHERE stock_quantity = 0 AND stock <> 0",
                "UPDATE products SET supplier_price = COALESCE(NULLIF(supplier_price,0), 0), purchase_price = COALESCE(NULLIF(purchase_price,0), supplier_price, 0)",
                "UPDATE products SET available_quantity = GREATEST(stock_quantity - reserved_quantity, 0) WHERE available_quantity = 0",
                "ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS feed_url TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS skipped INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS created_count INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS updated_count INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS variants_linked INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS summary_json JSONB NOT NULL DEFAULT '{}'::jsonb",
                "ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS protocol_json JSONB NOT NULL DEFAULT '{}'::jsonb",
                "CREATE TABLE IF NOT EXISTS categories (id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL DEFAULT '', parent_id TEXT NOT NULL DEFAULT '', external_matterhorn_id TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', source_system TEXT NOT NULL DEFAULT '', marketplace_mappings_json JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
                "CREATE TABLE IF NOT EXISTS product_categories (product_sku TEXT NOT NULL, category_id TEXT NOT NULL, is_primary BOOLEAN NOT NULL DEFAULT FALSE, marketplace_mappings_json JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(product_sku, category_id))",
                "CREATE TABLE IF NOT EXISTS product_variants (id TEXT PRIMARY KEY, parent_sku TEXT NOT NULL, sku TEXT NOT NULL UNIQUE, external_matterhorn_id TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', ean TEXT NOT NULL DEFAULT '', barcode TEXT NOT NULL DEFAULT '', supplier_price NUMERIC(12,4) NOT NULL DEFAULT 0, sale_price NUMERIC(12,4) NOT NULL DEFAULT 0, stock_quantity INTEGER NOT NULL DEFAULT 0, reserved_quantity INTEGER NOT NULL DEFAULT 0, available_quantity INTEGER NOT NULL DEFAULT 0, weight_net_kg NUMERIC(12,4) NOT NULL DEFAULT 0, weight_gross_kg NUMERIC(12,4) NOT NULL DEFAULT 0, length_cm NUMERIC(12,4) NOT NULL DEFAULT 0, width_cm NUMERIC(12,4) NOT NULL DEFAULT 0, height_cm NUMERIC(12,4) NOT NULL DEFAULT 0, attributes_json JSONB NOT NULL DEFAULT '{}'::jsonb, raw_source_data_json JSONB NOT NULL DEFAULT '{}'::jsonb, status TEXT NOT NULL DEFAULT 'active', source_hash TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
                "CREATE TABLE IF NOT EXISTS product_attributes (id TEXT PRIMARY KEY, product_sku TEXT NOT NULL, variant_sku TEXT NOT NULL DEFAULT '', name TEXT NOT NULL, value TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT '', is_filterable BOOLEAN NOT NULL DEFAULT FALSE, is_variant BOOLEAN NOT NULL DEFAULT FALSE, source_field TEXT NOT NULL DEFAULT '', extra_json JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
                "CREATE TABLE IF NOT EXISTS product_media (id TEXT PRIMARY KEY, product_sku TEXT NOT NULL, variant_sku TEXT NOT NULL DEFAULT '', media_type TEXT NOT NULL DEFAULT 'image', url TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL DEFAULT '', alt_text TEXT NOT NULL DEFAULT '', metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
                "CREATE INDEX IF NOT EXISTS idx_products_external_matterhorn_id ON products(external_matterhorn_id)",
                "CREATE INDEX IF NOT EXISTS idx_products_parent_sku ON products(parent_sku)",
                "CREATE INDEX IF NOT EXISTS idx_products_ean ON products(ean)",
                "CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id)",
                "CREATE INDEX IF NOT EXISTS idx_categories_external ON categories(external_matterhorn_id)",
                "CREATE INDEX IF NOT EXISTS idx_product_categories_category ON product_categories(category_id)",
                "CREATE INDEX IF NOT EXISTS idx_product_variants_parent ON product_variants(parent_sku)",
                "CREATE INDEX IF NOT EXISTS idx_product_variants_external ON product_variants(external_matterhorn_id)",
                "CREATE INDEX IF NOT EXISTS idx_product_attributes_product ON product_attributes(product_sku)",
                "CREATE INDEX IF NOT EXISTS idx_product_attributes_name ON product_attributes(name)",
                "CREATE INDEX IF NOT EXISTS idx_product_media_product ON product_media(product_sku, media_type)",
                "ALTER TABLE matterhorn_feeds ADD COLUMN IF NOT EXISTS feed_url TEXT",
                "ALTER TABLE matterhorn_feeds ADD COLUMN IF NOT EXISTS auth_header TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE matterhorn_feeds ADD COLUMN IF NOT EXISTS last_imported_at TIMESTAMPTZ",
                "ALTER TABLE matterhorn_feeds ADD COLUMN IF NOT EXISTS last_status TEXT NOT NULL DEFAULT ''",
                "UPDATE matterhorn_feeds SET feed_url = COALESCE(feed_url, url) WHERE (feed_url IS NULL OR feed_url='') AND COALESCE(url,'') <> ''",
                "ALTER TABLE matterhorn_mappings ADD COLUMN IF NOT EXISTS feed_id TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE matterhorn_mappings ADD COLUMN IF NOT EXISTS transform TEXT NOT NULL DEFAULT 'direct'",
                "ALTER TABLE matterhorn_mappings ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE matterhorn_mappings ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE",
                "ALTER TABLE matterhorn_mappings ADD COLUMN IF NOT EXISTS config_json JSONB NOT NULL DEFAULT '{}'::jsonb",
                "ALTER TABLE matterhorn_config ADD COLUMN IF NOT EXISTS value_json JSONB",
                "UPDATE matterhorn_config SET value_json = jsonb_build_object('value', value) WHERE value_json IS NULL",
                """CREATE TABLE IF NOT EXISTS matterhorn_import_previews (
                    id TEXT PRIMARY KEY,
                    feed_url TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'created',
                    summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    items_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                    validation_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    source_hash TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""",
            ]
            for stmt in compat:
                try:
                    await conn.execute(stmt)
                except Exception:
                    pass
        logger.info("PostgreSQL schema migration complete.")
        return

    async with get_connection() as db:
        await db.executescript(SQLITE_SCHEMA)
        # idempotent column/table migrations for evolved SQLite schemas
        sqlite_compat = [
            "ALTER TABLE products ADD COLUMN external_matterhorn_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN parent_sku TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN brand TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN manufacturer TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN short_description TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN seo_title TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN meta_description TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN slug TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN canonical_url TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN purchase_price REAL NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN sale_price REAL NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN compare_at_price REAL NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN tax_rate REAL NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN currency TEXT NOT NULL DEFAULT 'EUR'",
            "ALTER TABLE products ADD COLUMN stock_quantity INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN reserved_quantity INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN available_quantity INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN reorder_level INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN lead_time_days INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN supplier_stock INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN warehouse_location TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN bin_code TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN ean TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN barcode TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN requires_serial_number INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN is_fragile INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN is_hazmat INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN weight_net_kg REAL NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN weight_gross_kg REAL NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN length_cm REAL NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN width_cm REAL NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN height_cm REAL NOT NULL DEFAULT 0",
            "ALTER TABLE products ADD COLUMN hs_code TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN country_of_origin TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN main_image_url TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN gallery_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE products ADD COLUMN videos_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE products ADD COLUMN documents_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE products ADD COLUMN source_system TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE products ADD COLUMN raw_source_data_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE products ADD COLUMN marketplace_mappings_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE products ADD COLUMN has_variants INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE import_runs ADD COLUMN mode TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE import_runs ADD COLUMN feed_url TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE import_runs ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE import_runs ADD COLUMN created_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE import_runs ADD COLUMN updated_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE import_runs ADD COLUMN variants_linked INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE import_runs ADD COLUMN summary_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE import_runs ADD COLUMN protocol_json TEXT NOT NULL DEFAULT '{}'",
            "CREATE TABLE IF NOT EXISTS categories (id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL DEFAULT '', parent_id TEXT NOT NULL DEFAULT '', external_matterhorn_id TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', source_system TEXT NOT NULL DEFAULT '', marketplace_mappings_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS product_categories (product_sku TEXT NOT NULL, category_id TEXT NOT NULL, is_primary INTEGER NOT NULL DEFAULT 0, marketplace_mappings_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, PRIMARY KEY(product_sku, category_id))",
            "CREATE TABLE IF NOT EXISTS product_variants (id TEXT PRIMARY KEY, parent_sku TEXT NOT NULL, sku TEXT NOT NULL UNIQUE, external_matterhorn_id TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', ean TEXT NOT NULL DEFAULT '', barcode TEXT NOT NULL DEFAULT '', supplier_price REAL NOT NULL DEFAULT 0, sale_price REAL NOT NULL DEFAULT 0, stock_quantity INTEGER NOT NULL DEFAULT 0, reserved_quantity INTEGER NOT NULL DEFAULT 0, available_quantity INTEGER NOT NULL DEFAULT 0, weight_net_kg REAL NOT NULL DEFAULT 0, weight_gross_kg REAL NOT NULL DEFAULT 0, length_cm REAL NOT NULL DEFAULT 0, width_cm REAL NOT NULL DEFAULT 0, height_cm REAL NOT NULL DEFAULT 0, attributes_json TEXT NOT NULL DEFAULT '{}', raw_source_data_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'active', source_hash TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS product_attributes (id TEXT PRIMARY KEY, product_sku TEXT NOT NULL, variant_sku TEXT NOT NULL DEFAULT '', name TEXT NOT NULL, value TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT '', is_filterable INTEGER NOT NULL DEFAULT 0, is_variant INTEGER NOT NULL DEFAULT 0, source_field TEXT NOT NULL DEFAULT '', extra_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS product_media (id TEXT PRIMARY KEY, product_sku TEXT NOT NULL, variant_sku TEXT NOT NULL DEFAULT '', media_type TEXT NOT NULL DEFAULT 'image', url TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL DEFAULT '', alt_text TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_products_external_matterhorn_id ON products(external_matterhorn_id)",
            "CREATE INDEX IF NOT EXISTS idx_products_parent_sku ON products(parent_sku)",
            "CREATE INDEX IF NOT EXISTS idx_products_ean ON products(ean)",
            "CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id)",
            "CREATE INDEX IF NOT EXISTS idx_product_categories_category ON product_categories(category_id)",
            "CREATE INDEX IF NOT EXISTS idx_product_variants_parent ON product_variants(parent_sku)",
            "CREATE INDEX IF NOT EXISTS idx_product_attributes_product ON product_attributes(product_sku)",
            "CREATE INDEX IF NOT EXISTS idx_product_media_product ON product_media(product_sku, media_type)",
        ]
        for stmt in sqlite_compat:
            try:
                await db.execute(stmt)
            except Exception:
                pass
        try:
            await db.execute("UPDATE products SET stock_quantity = CASE WHEN stock_quantity=0 THEN stock ELSE stock_quantity END WHERE stock<>0")
            await db.execute("UPDATE products SET purchase_price = CASE WHEN purchase_price=0 THEN supplier_price ELSE purchase_price END")
            await db.execute("UPDATE products SET available_quantity = MAX(stock_quantity - reserved_quantity, 0) WHERE available_quantity=0")
        except Exception:
            pass
        await db.commit()
    logger.info("SQLite WAL schema migration complete at %s.", DATABASE_PATH)


async def set_setting(key: str, value: str, *, is_secret: bool = False) -> None:
    """Idempotently store one setting."""
    if not key or not key.replace("_", "").isalnum():
        raise ValueError("Setting keys may only contain letters, digits, and underscores.")
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO settings(key, value, is_secret, updated_at) VALUES ($1, $2, $3, NOW())
                   ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value, is_secret = EXCLUDED.is_secret, updated_at = NOW()""",
                key, value, is_secret,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO settings(key, value, is_secret, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, is_secret = excluded.is_secret, updated_at = excluded.updated_at""",
            (key, value, int(is_secret), utc_now()),
        )
        await db.commit()


async def get_setting(key: str) -> str | None:
    """Get a raw setting for trusted server-side code only."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM settings WHERE key = $1", key)
            return str(row["value"]) if row else None
    async with get_connection() as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
        return str(row["value"]) if row else None


async def list_settings(*, reveal_secrets: bool = False) -> dict[str, str]:
    """List settings while masking secret values by default."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value, is_secret FROM settings ORDER BY key")
        return {str(r["key"]): str(r["value"]) if reveal_secrets or not r["is_secret"] else "********" for r in rows}
    async with get_connection() as db:
        cursor = await db.execute("SELECT key, value, is_secret FROM settings ORDER BY key")
        rows = await cursor.fetchall()
        await cursor.close()
        return {str(r["key"]): str(r["value"]) if reveal_secrets or not r["is_secret"] else "********" for r in rows}


def _default_ai_settings() -> dict[str, str]:
    mapping = {
        "copilot_agent": "omnicore-copilot",
        "chat_general": "auto/smart",
        "reasoning_complex": "auto/smart",
        "coding_rules": "auto/coding",
        "content_bulk": "omnicore-content-bulk",
        "seo_text": "auto/cheap",
        "product_description": "omnicore-content-quality",
        "translation": "auto/cheap",
        "classification_extraction": "auto/fast",
        "trend_analysis": "omnicore-trend-analysis",
        "catalog_analysis_long_context": "omnicore-longcontext",
        "vision_image_analysis": "auto/vision",
        "image_generation": "auto/vision",
        "photoshoot_product": "omnicore-photoshoot",
        "photoshoot_with_model_identity": "omnicore-photoshoot-identity",
        "critical_reliable": "omnicore-critical-fusion",
        "budget_subscription_first": "omnicore-subscription-thrifty",
        "realtime_fast": "auto/fast",
        "offline_quota": "auto/offline",
    }
    return {
        "omniroute_agent_model": "omnicore-copilot",
        "default_llm_model": "auto/smart",
        "default_image_model": "auto/vision",
        "ai_model_copilot": "omnicore-copilot",
        "ai_model_chat": "auto/smart",
        "ai_model_coding": "auto/coding",
        "ai_model_content_bulk": "omnicore-content-bulk",
        "ai_model_content_quality": "omnicore-content-quality",
        "ai_model_seo": "auto/cheap",
        "ai_model_translation": "auto/cheap",
        "ai_model_classification": "auto/fast",
        "ai_model_trend": "omnicore-trend-analysis",
        "ai_model_longcontext": "omnicore-longcontext",
        "ai_model_vision": "auto/vision",
        "ai_model_image": "auto/vision",
        "ai_model_photoshoot": "omnicore-photoshoot",
        "ai_model_photoshoot_identity": "omnicore-photoshoot-identity",
        "ai_model_reliable": "omnicore-critical-fusion",
        "ai_model_budget": "omnicore-subscription-thrifty",
        "ai_model_cache": "omnicore-cache-optimized",
        "omniroute_image_model": "omnicore-photoshoot",
        "ai_fallback_chain": json.dumps(["auto/smart", "auto", "auto/cheap"], ensure_ascii=False),
        "ai_default_strategy": "lkgp",
        "ai_task_model_mapping": json.dumps(mapping, ensure_ascii=False),
        "omniroute_combo_config_path": "config/omniroute/omnicore-auto-combos.json",
        "omniroute_combo_config_loaded": "true",
    }


async def ensure_default_settings() -> int:
    """Seed recommended non-secret default settings without overwriting existing values."""
    defaults = _default_ai_settings()
    created = 0
    for key, value in defaults.items():
        existing = await get_setting(key)
        if existing is None or str(existing).strip() == "":
            await set_setting(key, str(value), is_secret=False)
            created += 1
    return created


async def write_log(level: str, event: str, message: str, context: Mapping[str, Any] | None = None) -> None:
    """Persist one bounded structured log event."""
    safe_level = level.upper()
    if safe_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        safe_level = "ERROR"
    payload = json.dumps(context or {}, ensure_ascii=False, separators=(",", ":"))[:4096]
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO system_logs(level, event, message, context_json) VALUES ($1, $2, $3, $4::jsonb)",
                safe_level, event[:128], message[:2000], payload,
            )
        return
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO system_logs(level, event, message, context_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (safe_level, event[:128], message[:2000], payload, utc_now()),
        )
        await db.commit()


async def recent_logs(limit: int = 50) -> list[dict[str, Any]]:
    """Return recent bounded log records."""
    safe_limit = min(max(limit, 1), 200)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, level, event, message, context_json, created_at FROM system_logs ORDER BY id DESC LIMIT $1", safe_limit)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT id, level, event, message, context_json, created_at FROM system_logs ORDER BY id DESC LIMIT ?", (safe_limit,))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


def _json_dump(value: Any, default: Any) -> str:
    if value is None:
        value = default
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _coerce_num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return float(default)
        return float(str(value).replace(",", ".").strip())
    except Exception:
        return float(default)


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return int(default)
        return int(float(str(value).replace(",", ".").strip()))
    except Exception:
        return int(default)


async def _fetch_existing_product_row(sku: str) -> dict[str, Any] | None:
    if not sku:
        return None
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM products WHERE sku=$1", sku)
        return dict(row) if row else None
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM products WHERE sku=?", (sku,))
        row = await cur.fetchone(); await cur.close()
        return dict(row) if row else None


async def upsert_product(
    *,
    sku: str,
    title: str,
    category: str = "",
    supplier_price: float = 0.0,
    stock: int = 0,
    source_hash: str = "",
    raw_description: str = "",
    status: str = "active",
    **extra: Any,
) -> bool:
    """Idempotently persist a supplier/product record and report if its source changed.

    Backward-compatible with legacy callers while supporting extended catalog fields.
    For import sync rules, pass ``sync_rules`` in extra (stock_only/ek_only/full/preserve_manual)
    and ``manual_overrides`` flags to preserve curated fields.
    """
    if not sku or _coerce_num(supplier_price) < 0 or _coerce_int(stock) < 0:
        raise ValueError("SKU must be non-empty; price and stock cannot be negative.")
    if status not in {"active", "draft", "paused", "archived"}:
        raise ValueError("Product status must be active, draft, paused, or archived.")

    external_matterhorn_id = str(extra.get("external_matterhorn_id") or extra.get("external_id") or "").strip()
    existing: dict[str, Any] | None = None
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT * FROM products WHERE sku=$1", sku)
            if existing is None and external_matterhorn_id:
                existing = await conn.fetchrow("SELECT * FROM products WHERE external_matterhorn_id=$1 LIMIT 1", external_matterhorn_id)
        existing = dict(existing) if existing else None
    else:
        async with get_connection() as db:
            cur = await db.execute("SELECT * FROM products WHERE sku=?", (sku,))
            row = await cur.fetchone(); await cur.close()
            existing = dict(row) if row else None
        if existing is None and external_matterhorn_id:
            async with get_connection() as db:
                cur = await db.execute("SELECT * FROM products WHERE external_matterhorn_id=? LIMIT 1", (external_matterhorn_id,))
                row = await cur.fetchone(); await cur.close()
                existing = dict(row) if row else None

    target_sku = str((existing or {}).get("sku") or sku).strip() or sku
    if existing and existing.get("source_hash") == source_hash and source_hash:
        # still allow explicit status-only/manual changes? legacy behavior: unchanged
        if not extra.get("force_update"):
            return False

    sync_rules = extra.get("sync_rules") or {}
    if isinstance(sync_rules, str):
        try: sync_rules = json.loads(sync_rules)
        except Exception: sync_rules = {}
    mode = str(sync_rules.get("mode") or extra.get("sync_mode") or "full").lower()
    preserve_manual = bool(sync_rules.get("preserve_manual_seo", True))
    preserve_marketplace = bool(sync_rules.get("preserve_marketplace_mappings", True))
    manual_fields = set(sync_rules.get("preserve_fields") or extra.get("preserve_fields") or [])

    def pick(field: str, incoming: Any, current: Any = None):
        if mode in {"stock_only", "stock"} and field not in {"stock", "stock_quantity", "available_quantity", "reserved_quantity", "supplier_stock", "source_hash", "updated_at", "status"}:
            return current
        if mode in {"ek_only", "price_only", "purchase_only"} and field not in {"supplier_price", "purchase_price", "sale_price", "source_hash", "updated_at", "status", "currency", "tax_rate"}:
            return current
        if preserve_manual and field in {"seo_title", "meta_description", "slug", "canonical_url"} and str(current or "").strip():
            return current
        if preserve_marketplace and field == "marketplace_mappings_json" and str(current or "").strip() not in {"", "{}", "[]"}:
            return current
        if field in manual_fields and str(current or "").strip():
            return current
        return incoming

    cur = existing or {}
    # normalize/merge
    purchase_price = _coerce_num(extra.get("purchase_price", extra.get("ek_price", supplier_price)))
    supplier_price_n = _coerce_num(supplier_price)
    sale_price = _coerce_num(extra.get("sale_price", extra.get("retail_price", extra.get("price", 0))))
    stock_n = _coerce_int(stock)
    stock_quantity = _coerce_int(extra.get("stock_quantity", stock_n))
    reserved_quantity = _coerce_int(extra.get("reserved_quantity", cur.get("reserved_quantity", 0)))
    supplier_stock = _coerce_int(extra.get("supplier_stock", extra.get("raw_stock", stock_n)))
    available_quantity = max(stock_quantity - reserved_quantity, 0)
    payload = {
        "sku": target_sku,
        "external_matterhorn_id": pick("external_matterhorn_id", external_matterhorn_id or str(cur.get("external_matterhorn_id") or ""), cur.get("external_matterhorn_id", "")),
        "parent_sku": pick("parent_sku", str(extra.get("parent_sku") or cur.get("parent_sku") or ""), cur.get("parent_sku", "")),
        "title": pick("title", title, cur.get("title", "")),
        "brand": pick("brand", str(extra.get("brand") or cur.get("brand") or ""), cur.get("brand", "")),
        "manufacturer": pick("manufacturer", str(extra.get("manufacturer") or cur.get("manufacturer") or ""), cur.get("manufacturer", "")),
        "category": pick("category", category, cur.get("category", "")),
        "short_description": pick("short_description", str(extra.get("short_description") or cur.get("short_description") or ""), cur.get("short_description", "")),
        "raw_description": pick("raw_description", raw_description, cur.get("raw_description", "")),
        "ai_title": pick("ai_title", str(extra.get("ai_title") or cur.get("ai_title") or ""), cur.get("ai_title", "")),
        "ai_description_html": pick("ai_description_html", str(extra.get("ai_description_html") or cur.get("ai_description_html") or ""), cur.get("ai_description_html", "")),
        "ai_tags_json": pick("ai_tags_json", extra.get("ai_tags_json", cur.get("ai_tags_json", [])), cur.get("ai_tags_json", [])),
        "seo_title": pick("seo_title", str(extra.get("seo_title") or cur.get("seo_title") or ""), cur.get("seo_title", "")),
        "meta_description": pick("meta_description", str(extra.get("meta_description") or cur.get("meta_description") or ""), cur.get("meta_description", "")),
        "slug": pick("slug", str(extra.get("slug") or cur.get("slug") or ""), cur.get("slug", "")),
        "canonical_url": pick("canonical_url", str(extra.get("canonical_url") or cur.get("canonical_url") or ""), cur.get("canonical_url", "")),
        "supplier_price": pick("supplier_price", supplier_price_n, _coerce_num(cur.get("supplier_price", 0))),
        "purchase_price": pick("purchase_price", purchase_price, _coerce_num(cur.get("purchase_price", 0))),
        "sale_price": pick("sale_price", sale_price, _coerce_num(cur.get("sale_price", 0))),
        "compare_at_price": pick("compare_at_price", _coerce_num(extra.get("compare_at_price", cur.get("compare_at_price", 0))), _coerce_num(cur.get("compare_at_price", 0))),
        "tax_rate": pick("tax_rate", _coerce_num(extra.get("tax_rate", cur.get("tax_rate", 0))), _coerce_num(cur.get("tax_rate", 0))),
        "currency": pick("currency", str(extra.get("currency") or cur.get("currency") or "EUR"), cur.get("currency", "EUR")),
        "stock": pick("stock", stock_n, _coerce_int(cur.get("stock", 0))),
        "stock_quantity": pick("stock_quantity", stock_quantity, _coerce_int(cur.get("stock_quantity", cur.get("stock", 0)))),
        "reserved_quantity": reserved_quantity,
        "available_quantity": pick("available_quantity", _coerce_int(extra.get("available_quantity", available_quantity)), _coerce_int(cur.get("available_quantity", 0))),
        "reorder_level": pick("reorder_level", _coerce_int(extra.get("reorder_level", cur.get("reorder_level", 0))), _coerce_int(cur.get("reorder_level", 0))),
        "lead_time_days": pick("lead_time_days", _coerce_int(extra.get("lead_time_days", cur.get("lead_time_days", 0))), _coerce_int(cur.get("lead_time_days", 0))),
        "supplier_stock": pick("supplier_stock", supplier_stock, _coerce_int(cur.get("supplier_stock", 0))),
        "warehouse_location": pick("warehouse_location", str(extra.get("warehouse_location") or cur.get("warehouse_location") or ""), cur.get("warehouse_location", "")),
        "bin_code": pick("bin_code", str(extra.get("bin_code") or cur.get("bin_code") or ""), cur.get("bin_code", "")),
        "ean": pick("ean", str(extra.get("ean") or cur.get("ean") or ""), cur.get("ean", "")),
        "barcode": pick("barcode", str(extra.get("barcode") or cur.get("barcode") or extra.get("ean") or ""), cur.get("barcode", "")),
        "requires_serial_number": pick("requires_serial_number", _coerce_bool(extra.get("requires_serial_number", cur.get("requires_serial_number", False))), _coerce_bool(cur.get("requires_serial_number", False))),
        "is_fragile": pick("is_fragile", _coerce_bool(extra.get("is_fragile", cur.get("is_fragile", False))), _coerce_bool(cur.get("is_fragile", False))),
        "is_hazmat": pick("is_hazmat", _coerce_bool(extra.get("is_hazmat", cur.get("is_hazmat", False))), _coerce_bool(cur.get("is_hazmat", False))),
        "weight_net_kg": pick("weight_net_kg", _coerce_num(extra.get("weight_net_kg", cur.get("weight_net_kg", 0))), _coerce_num(cur.get("weight_net_kg", 0))),
        "weight_gross_kg": pick("weight_gross_kg", _coerce_num(extra.get("weight_gross_kg", cur.get("weight_gross_kg", 0))), _coerce_num(cur.get("weight_gross_kg", 0))),
        "length_cm": pick("length_cm", _coerce_num(extra.get("length_cm", cur.get("length_cm", 0))), _coerce_num(cur.get("length_cm", 0))),
        "width_cm": pick("width_cm", _coerce_num(extra.get("width_cm", cur.get("width_cm", 0))), _coerce_num(cur.get("width_cm", 0))),
        "height_cm": pick("height_cm", _coerce_num(extra.get("height_cm", cur.get("height_cm", 0))), _coerce_num(cur.get("height_cm", 0))),
        "hs_code": pick("hs_code", str(extra.get("hs_code") or cur.get("hs_code") or ""), cur.get("hs_code", "")),
        "country_of_origin": pick("country_of_origin", str(extra.get("country_of_origin") or cur.get("country_of_origin") or ""), cur.get("country_of_origin", "")),
        "main_image_url": pick("main_image_url", str(extra.get("main_image_url") or cur.get("main_image_url") or ""), cur.get("main_image_url", "")),
        "gallery_json": pick("gallery_json", extra.get("gallery_json", extra.get("images", cur.get("gallery_json", []))), cur.get("gallery_json", [])),
        "videos_json": pick("videos_json", extra.get("videos_json", cur.get("videos_json", [])), cur.get("videos_json", [])),
        "documents_json": pick("documents_json", extra.get("documents_json", cur.get("documents_json", [])), cur.get("documents_json", [])),
        "source_system": pick("source_system", str(extra.get("source_system") or cur.get("source_system") or ""), cur.get("source_system", "")),
        "raw_source_data_json": pick("raw_source_data_json", extra.get("raw_source_data_json", extra.get("raw_data", cur.get("raw_source_data_json", {}))), cur.get("raw_source_data_json", {})),
        "marketplace_mappings_json": pick("marketplace_mappings_json", extra.get("marketplace_mappings_json", cur.get("marketplace_mappings_json", {})), cur.get("marketplace_mappings_json", {})),
        "is_bundle": _coerce_bool(extra.get("is_bundle", cur.get("is_bundle", False))),
        "has_variants": _coerce_bool(extra.get("has_variants", cur.get("has_variants", False))),
        "status": status if (not cur.get("is_bundle") or mode in {"full", ""}) else str(cur.get("status") or status),
        "source_hash": source_hash or str(cur.get("source_hash") or ""),
    }

    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO products(
                    sku, external_matterhorn_id, parent_sku, title, brand, manufacturer, category, short_description, raw_description,
                    ai_title, ai_description_html, ai_tags_json, seo_title, meta_description, slug, canonical_url,
                    supplier_price, purchase_price, sale_price, compare_at_price, tax_rate, currency,
                    stock, stock_quantity, reserved_quantity, available_quantity, reorder_level, lead_time_days, supplier_stock,
                    warehouse_location, bin_code, ean, barcode, requires_serial_number, is_fragile, is_hazmat,
                    weight_net_kg, weight_gross_kg, length_cm, width_cm, height_cm, hs_code, country_of_origin,
                    main_image_url, gallery_json, videos_json, documents_json, source_system, raw_source_data_json, marketplace_mappings_json,
                    is_bundle, has_variants, status, source_hash, updated_at
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,
                    $23,$24,$25,$26,$27,$28,$29,$30,$31,$32,$33,$34,$35,$36,$37,$38,$39,$40,$41,$42,$43,
                    $44,$45::jsonb,$46::jsonb,$47::jsonb,$48,$49::jsonb,$50::jsonb,$51,$52,$53,$54,NOW()
                )
                ON CONFLICT(sku) DO UPDATE SET
                    external_matterhorn_id=EXCLUDED.external_matterhorn_id, parent_sku=EXCLUDED.parent_sku, title=EXCLUDED.title, brand=EXCLUDED.brand,
                    manufacturer=EXCLUDED.manufacturer, category=EXCLUDED.category, short_description=EXCLUDED.short_description, raw_description=EXCLUDED.raw_description,
                    ai_title=EXCLUDED.ai_title, ai_description_html=EXCLUDED.ai_description_html, ai_tags_json=EXCLUDED.ai_tags_json,
                    seo_title=EXCLUDED.seo_title, meta_description=EXCLUDED.meta_description, slug=EXCLUDED.slug, canonical_url=EXCLUDED.canonical_url,
                    supplier_price=EXCLUDED.supplier_price, purchase_price=EXCLUDED.purchase_price, sale_price=EXCLUDED.sale_price, compare_at_price=EXCLUDED.compare_at_price,
                    tax_rate=EXCLUDED.tax_rate, currency=EXCLUDED.currency, stock=EXCLUDED.stock, stock_quantity=EXCLUDED.stock_quantity, reserved_quantity=EXCLUDED.reserved_quantity,
                    available_quantity=EXCLUDED.available_quantity, reorder_level=EXCLUDED.reorder_level, lead_time_days=EXCLUDED.lead_time_days, supplier_stock=EXCLUDED.supplier_stock,
                    warehouse_location=EXCLUDED.warehouse_location, bin_code=EXCLUDED.bin_code, ean=EXCLUDED.ean, barcode=EXCLUDED.barcode,
                    requires_serial_number=EXCLUDED.requires_serial_number, is_fragile=EXCLUDED.is_fragile, is_hazmat=EXCLUDED.is_hazmat,
                    weight_net_kg=EXCLUDED.weight_net_kg, weight_gross_kg=EXCLUDED.weight_gross_kg, length_cm=EXCLUDED.length_cm, width_cm=EXCLUDED.width_cm, height_cm=EXCLUDED.height_cm,
                    hs_code=EXCLUDED.hs_code, country_of_origin=EXCLUDED.country_of_origin, main_image_url=EXCLUDED.main_image_url, gallery_json=EXCLUDED.gallery_json,
                    videos_json=EXCLUDED.videos_json, documents_json=EXCLUDED.documents_json, source_system=EXCLUDED.source_system, raw_source_data_json=EXCLUDED.raw_source_data_json,
                    marketplace_mappings_json=EXCLUDED.marketplace_mappings_json, is_bundle=EXCLUDED.is_bundle, has_variants=EXCLUDED.has_variants,
                    status=CASE WHEN products.is_bundle THEN products.status ELSE EXCLUDED.status END, source_hash=EXCLUDED.source_hash, updated_at=NOW()""",
                payload["sku"], payload["external_matterhorn_id"], payload["parent_sku"], payload["title"], payload["brand"], payload["manufacturer"], payload["category"], payload["short_description"], payload["raw_description"],
                payload["ai_title"], payload["ai_description_html"], _json_dump(payload["ai_tags_json"], []), payload["seo_title"], payload["meta_description"], payload["slug"], payload["canonical_url"],
                payload["supplier_price"], payload["purchase_price"], payload["sale_price"], payload["compare_at_price"], payload["tax_rate"], payload["currency"],
                payload["stock"], payload["stock_quantity"], payload["reserved_quantity"], payload["available_quantity"], payload["reorder_level"], payload["lead_time_days"], payload["supplier_stock"],
                payload["warehouse_location"], payload["bin_code"], payload["ean"], payload["barcode"], payload["requires_serial_number"], payload["is_fragile"], payload["is_hazmat"],
                payload["weight_net_kg"], payload["weight_gross_kg"], payload["length_cm"], payload["width_cm"], payload["height_cm"], payload["hs_code"], payload["country_of_origin"],
                payload["main_image_url"], _json_dump(payload["gallery_json"], []), _json_dump(payload["videos_json"], []), _json_dump(payload["documents_json"], []), payload["source_system"], _json_dump(payload["raw_source_data_json"], {}), _json_dump(payload["marketplace_mappings_json"], {}),
                payload["is_bundle"], payload["has_variants"], payload["status"], payload["source_hash"],
            )
        return existing is None or existing.get("source_hash") != payload["source_hash"]

    async with get_connection() as db:
        await db.execute(
            """INSERT INTO products(
                sku, external_matterhorn_id, parent_sku, title, brand, manufacturer, category, short_description, raw_description,
                ai_title, ai_description_html, ai_tags_json, seo_title, meta_description, slug, canonical_url,
                supplier_price, purchase_price, sale_price, compare_at_price, tax_rate, currency,
                stock, stock_quantity, reserved_quantity, available_quantity, reorder_level, lead_time_days, supplier_stock,
                warehouse_location, bin_code, ean, barcode, requires_serial_number, is_fragile, is_hazmat,
                weight_net_kg, weight_gross_kg, length_cm, width_cm, height_cm, hs_code, country_of_origin,
                main_image_url, gallery_json, videos_json, documents_json, source_system, raw_source_data_json, marketplace_mappings_json,
                is_bundle, has_variants, status, source_hash, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(sku) DO UPDATE SET
                external_matterhorn_id=excluded.external_matterhorn_id, parent_sku=excluded.parent_sku, title=excluded.title, brand=excluded.brand,
                manufacturer=excluded.manufacturer, category=excluded.category, short_description=excluded.short_description, raw_description=excluded.raw_description,
                ai_title=excluded.ai_title, ai_description_html=excluded.ai_description_html, ai_tags_json=excluded.ai_tags_json,
                seo_title=excluded.seo_title, meta_description=excluded.meta_description, slug=excluded.slug, canonical_url=excluded.canonical_url,
                supplier_price=excluded.supplier_price, purchase_price=excluded.purchase_price, sale_price=excluded.sale_price, compare_at_price=excluded.compare_at_price,
                tax_rate=excluded.tax_rate, currency=excluded.currency, stock=excluded.stock, stock_quantity=excluded.stock_quantity, reserved_quantity=excluded.reserved_quantity,
                available_quantity=excluded.available_quantity, reorder_level=excluded.reorder_level, lead_time_days=excluded.lead_time_days, supplier_stock=excluded.supplier_stock,
                warehouse_location=excluded.warehouse_location, bin_code=excluded.bin_code, ean=excluded.ean, barcode=excluded.barcode,
                requires_serial_number=excluded.requires_serial_number, is_fragile=excluded.is_fragile, is_hazmat=excluded.is_hazmat,
                weight_net_kg=excluded.weight_net_kg, weight_gross_kg=excluded.weight_gross_kg, length_cm=excluded.length_cm, width_cm=excluded.width_cm, height_cm=excluded.height_cm,
                hs_code=excluded.hs_code, country_of_origin=excluded.country_of_origin, main_image_url=excluded.main_image_url, gallery_json=excluded.gallery_json,
                videos_json=excluded.videos_json, documents_json=excluded.documents_json, source_system=excluded.source_system, raw_source_data_json=excluded.raw_source_data_json,
                marketplace_mappings_json=excluded.marketplace_mappings_json, is_bundle=excluded.is_bundle, has_variants=excluded.has_variants,
                status=CASE WHEN products.is_bundle = 1 THEN products.status ELSE excluded.status END, source_hash=excluded.source_hash, updated_at=excluded.updated_at""",
            (
                payload["sku"], payload["external_matterhorn_id"], payload["parent_sku"], payload["title"], payload["brand"], payload["manufacturer"], payload["category"], payload["short_description"], payload["raw_description"],
                payload["ai_title"], payload["ai_description_html"], _json_dump(payload["ai_tags_json"], []), payload["seo_title"], payload["meta_description"], payload["slug"], payload["canonical_url"],
                payload["supplier_price"], payload["purchase_price"], payload["sale_price"], payload["compare_at_price"], payload["tax_rate"], payload["currency"],
                payload["stock"], payload["stock_quantity"], payload["reserved_quantity"], payload["available_quantity"], payload["reorder_level"], payload["lead_time_days"], payload["supplier_stock"],
                payload["warehouse_location"], payload["bin_code"], payload["ean"], payload["barcode"], int(bool(payload["requires_serial_number"])), int(bool(payload["is_fragile"])), int(bool(payload["is_hazmat"])),
                payload["weight_net_kg"], payload["weight_gross_kg"], payload["length_cm"], payload["width_cm"], payload["height_cm"], payload["hs_code"], payload["country_of_origin"],
                payload["main_image_url"], _json_dump(payload["gallery_json"], []), _json_dump(payload["videos_json"], []), _json_dump(payload["documents_json"], []), payload["source_system"], _json_dump(payload["raw_source_data_json"], {}), _json_dump(payload["marketplace_mappings_json"], {}),
                int(bool(payload["is_bundle"])), int(bool(payload["has_variants"])), payload["status"], payload["source_hash"], utc_now(),
            ),
        )
        await db.commit()
        return existing is None or existing.get("source_hash") != payload["source_hash"]




def _decode_json_fields(d: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    for k in fields:
        v = d.get(k)
        if isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except Exception:
                pass
    return d

def _decode_product_row(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    d = dict(row)
    for k in ("ai_tags_json","gallery_json","videos_json","documents_json","raw_source_data_json","marketplace_mappings_json"):
        v = d.get(k)
        if isinstance(v, str):
            try: d[k] = json.loads(v)
            except Exception: pass
    for k in ("requires_serial_number","is_fragile","is_hazmat","is_bundle","has_variants"):
        if k in d and d[k] in (0,1):
            d[k] = bool(d[k])
    # convenience mirrors
    if d.get("stock_quantity") in (None, 0) and d.get("stock") not in (None, 0):
        d["stock_quantity"] = d.get("stock")
    if not d.get("available_quantity"):
        try:
            d["available_quantity"] = max(int(d.get("stock_quantity") or d.get("stock") or 0) - int(d.get("reserved_quantity") or 0), 0)
        except Exception:
            pass
    return d


async def list_products(
    limit: int = 100,
    offset: int = 0,
    q: str | None = None,
    status: str | None = None,
    category: str | None = None,
    brand: str | None = None,
    has_image: bool | None = None,
    in_stock: bool | None = None,
    **_kwargs: Any,
) -> list[dict[str, Any]]:
    """Return products with bounded pagination and optional search/filtering."""
    if _kwargs:
        # tolerate legacy/extra kwargs
        q = q if q is not None else _kwargs.get("q")
        status = status if status is not None else _kwargs.get("status")
        category = category if category is not None else _kwargs.get("category")
        brand = brand if brand is not None else _kwargs.get("brand")
        has_image = has_image if has_image is not None else _kwargs.get("has_image")
        in_stock = in_stock if in_stock is not None else _kwargs.get("in_stock")
    safe_limit, safe_offset = min(max(limit, 1), 500), max(offset, 0)
    where: list[str] = []
    params_pg: list[Any] = []
    params_sq: list[Any] = []

    def add(cond_pg: str, cond_sq: str, *vals: Any):
        where.append(cond_pg)
        # store tuple of (sq_cond, vals)
        params_pg.extend(vals)
        params_sq.extend(vals)

    # will build separately for clarity
    conds_pg=[]; conds_sq=[]; args_pg=[]; args_sq=[]
    if status:
        args_pg.append(status); args_sq.append(status)
        conds_pg.append(f"status = ${len(args_pg)}"); conds_sq.append("status = ?")
    if category:
        args_pg.append(f"%{category}%"); args_sq.append(f"%{category}%")
        conds_pg.append(f"category ILIKE ${len(args_pg)}" if uses_postgres() else f"category LIKE ${len(args_pg)}")
        conds_sq.append("LOWER(category) LIKE LOWER(?)")
    if brand:
        args_pg.append(f"%{brand}%"); args_sq.append(f"%{brand}%")
        conds_pg.append(f"brand ILIKE ${len(args_pg)}" if uses_postgres() else f"brand LIKE ${len(args_pg)}")
        conds_sq.append("LOWER(brand) LIKE LOWER(?)")
    if has_image is True:
        conds_pg.append("COALESCE(main_image_url,'') <> ''"); conds_sq.append("COALESCE(main_image_url,'') <> ''")
    elif has_image is False:
        conds_pg.append("COALESCE(main_image_url,'') = ''"); conds_sq.append("COALESCE(main_image_url,'') = ''")
    if in_stock is True:
        conds_pg.append("COALESCE(stock_quantity, stock, 0) > 0"); conds_sq.append("COALESCE(stock_quantity, stock, 0) > 0")
    elif in_stock is False:
        conds_pg.append("COALESCE(stock_quantity, stock, 0) <= 0"); conds_sq.append("COALESCE(stock_quantity, stock, 0) <= 0")
    if q and str(q).strip():
        term = f"%{str(q).strip()}%"
        # search core fields; variants/attributes via EXISTS for richer catalog search
        if uses_postgres():
            base_idx = len(args_pg) + 1
            args_pg.extend([term]*8)
            conds_pg.append(
                f"(sku ILIKE ${base_idx} OR title ILIKE ${base_idx+1} OR COALESCE(ai_title,'') ILIKE ${base_idx+2} OR COALESCE(brand,'') ILIKE ${base_idx+3} OR COALESCE(category,'') ILIKE ${base_idx+4} OR COALESCE(ean,'') ILIKE ${base_idx+5} OR COALESCE(external_matterhorn_id,'') ILIKE ${base_idx+6} OR COALESCE(raw_description,'') ILIKE ${base_idx+7}"
                f" OR EXISTS (SELECT 1 FROM product_variants v WHERE (v.parent_sku=products.sku OR v.sku=products.sku) AND (v.sku ILIKE ${base_idx} OR COALESCE(v.ean,'') ILIKE ${base_idx+5} OR COALESCE(v.title,'') ILIKE ${base_idx+1}))"
                f" OR EXISTS (SELECT 1 FROM product_attributes a WHERE a.product_sku=products.sku AND (a.name ILIKE ${base_idx+1} OR a.value ILIKE ${base_idx+7}))"
                f")"
            )
        args_sq.extend([term]*8)
        conds_sq.append(
            "(LOWER(sku) LIKE LOWER(?) OR LOWER(title) LIKE LOWER(?) OR LOWER(COALESCE(ai_title,'')) LIKE LOWER(?) OR LOWER(COALESCE(brand,'')) LIKE LOWER(?) OR LOWER(COALESCE(category,'')) LIKE LOWER(?) OR LOWER(COALESCE(ean,'')) LIKE LOWER(?) OR LOWER(COALESCE(external_matterhorn_id,'')) LIKE LOWER(?) OR LOWER(COALESCE(raw_description,'')) LIKE LOWER(?)"
            " OR EXISTS (SELECT 1 FROM product_variants v WHERE (v.parent_sku=products.sku OR v.sku=products.sku) AND (LOWER(v.sku) LIKE LOWER(?) OR LOWER(COALESCE(v.ean,'')) LIKE LOWER(?) OR LOWER(COALESCE(v.title,'')) LIKE LOWER(?)))"
            " OR EXISTS (SELECT 1 FROM product_attributes a WHERE a.product_sku=products.sku AND (LOWER(a.name) LIKE LOWER(?) OR LOWER(a.value) LIKE LOWER(?)))"
            ")"
        )
        # extra terms for sqlite exists params (re-use same term)
        args_sq.extend([term, term, term, term, term])

    where_pg = (" WHERE " + " AND ".join(conds_pg)) if conds_pg else ""
    where_sq = (" WHERE " + " AND ".join(conds_sq)) if conds_sq else ""

    if uses_postgres():
        pool = await get_pg_pool()
        qsql = (
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM product_variants v WHERE v.parent_sku=p.sku) AS variant_count, "
            "(SELECT COUNT(*) FROM product_attributes a WHERE a.product_sku=p.sku) AS attribute_count, "
            "(SELECT COUNT(*) FROM product_media m WHERE m.product_sku=p.sku) AS media_count "
            f"FROM products p{where_pg.replace('products.sku','p.sku').replace('products.brand','p.brand') if where_pg else ''} "
            f"ORDER BY p.updated_at DESC, p.sku ASC LIMIT ${len(args_pg)+1} OFFSET ${len(args_pg)+2}"
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(qsql, *args_pg, safe_limit, safe_offset)
        return [_decode_product_row(row) for row in rows]
    qsql = (
        "SELECT p.*, "
        "(SELECT COUNT(*) FROM product_variants v WHERE v.parent_sku=p.sku) AS variant_count, "
        "(SELECT COUNT(*) FROM product_attributes a WHERE a.product_sku=p.sku) AS attribute_count, "
        "(SELECT COUNT(*) FROM product_media m WHERE m.product_sku=p.sku) AS media_count "
        f"FROM products p{where_sq.replace('products.sku','p.sku')} "
        "ORDER BY p.updated_at DESC, p.sku ASC LIMIT ? OFFSET ?"
    )
    async with get_connection() as db:
        cursor = await db.execute(qsql, (*args_sq, safe_limit, safe_offset))
        rows = await cursor.fetchall()
        await cursor.close()
        return [_decode_product_row(row) for row in rows]


async def count_products(q: str | None = None, status: str | None = None, category: str | None = None, brand: str | None = None, has_image: bool | None = None, in_stock: bool | None = None) -> int:
    """Return total number of products matching optional filters/search."""
    conds_pg=[]; conds_sq=[]; args_pg=[]; args_sq=[]
    if status:
        args_pg.append(status); args_sq.append(status)
        conds_pg.append(f"status = ${len(args_pg)}"); conds_sq.append("status = ?")
    if category:
        args_pg.append(f"%{category}%"); args_sq.append(f"%{category}%")
        conds_pg.append(f"category ILIKE ${len(args_pg)}" if uses_postgres() else f"category LIKE ${len(args_pg)}")
        conds_sq.append("LOWER(category) LIKE LOWER(?)")
    if brand:
        args_pg.append(f"%{brand}%"); args_sq.append(f"%{brand}%")
        conds_pg.append(f"brand ILIKE ${len(args_pg)}" if uses_postgres() else f"brand LIKE ${len(args_pg)}")
        conds_sq.append("LOWER(brand) LIKE LOWER(?)")
    if has_image is True:
        conds_pg.append("COALESCE(main_image_url,'') <> ''"); conds_sq.append("COALESCE(main_image_url,'') <> ''")
    elif has_image is False:
        conds_pg.append("COALESCE(main_image_url,'') = ''"); conds_sq.append("COALESCE(main_image_url,'') = ''")
    if in_stock is True:
        conds_pg.append("COALESCE(stock_quantity, stock, 0) > 0"); conds_sq.append("COALESCE(stock_quantity, stock, 0) > 0")
    elif in_stock is False:
        conds_pg.append("COALESCE(stock_quantity, stock, 0) <= 0"); conds_sq.append("COALESCE(stock_quantity, stock, 0) <= 0")
    if q and str(q).strip():
        term = f"%{str(q).strip()}%"
        if uses_postgres():
            base_idx = len(args_pg) + 1
            args_pg.extend([term]*8)
            conds_pg.append(
                f"(sku ILIKE ${base_idx} OR title ILIKE ${base_idx+1} OR COALESCE(ai_title,'') ILIKE ${base_idx+2} OR COALESCE(brand,'') ILIKE ${base_idx+3} OR COALESCE(category,'') ILIKE ${base_idx+4} OR COALESCE(ean,'') ILIKE ${base_idx+5} OR COALESCE(external_matterhorn_id,'') ILIKE ${base_idx+6} OR COALESCE(raw_description,'') ILIKE ${base_idx+7}"
                f" OR EXISTS (SELECT 1 FROM product_variants v WHERE (v.parent_sku=products.sku OR v.sku=products.sku) AND (v.sku ILIKE ${base_idx} OR COALESCE(v.ean,'') ILIKE ${base_idx+5} OR COALESCE(v.title,'') ILIKE ${base_idx+1}))"
                f" OR EXISTS (SELECT 1 FROM product_attributes a WHERE a.product_sku=products.sku AND (a.name ILIKE ${base_idx+1} OR a.value ILIKE ${base_idx+7}))"
                f")"
            )
        args_sq.extend([term]*8)
        conds_sq.append(
            "(LOWER(sku) LIKE LOWER(?) OR LOWER(title) LIKE LOWER(?) OR LOWER(COALESCE(ai_title,'')) LIKE LOWER(?) OR LOWER(COALESCE(brand,'')) LIKE LOWER(?) OR LOWER(COALESCE(category,'')) LIKE LOWER(?) OR LOWER(COALESCE(ean,'')) LIKE LOWER(?) OR LOWER(COALESCE(external_matterhorn_id,'')) LIKE LOWER(?) OR LOWER(COALESCE(raw_description,'')) LIKE LOWER(?)"
            " OR EXISTS (SELECT 1 FROM product_variants v WHERE (v.parent_sku=products.sku OR v.sku=products.sku) AND (LOWER(v.sku) LIKE LOWER(?) OR LOWER(COALESCE(v.ean,'')) LIKE LOWER(?) OR LOWER(COALESCE(v.title,'')) LIKE LOWER(?)))"
            " OR EXISTS (SELECT 1 FROM product_attributes a WHERE a.product_sku=products.sku AND (LOWER(a.name) LIKE LOWER(?) OR LOWER(a.value) LIKE LOWER(?)))"
            ")"
        )
        args_sq.extend([term, term, term, term, term])
    where_pg = (" WHERE " + " AND ".join(conds_pg)) if conds_pg else ""
    where_sq = (" WHERE " + " AND ".join(conds_sq)) if conds_sq else ""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval(f"SELECT COUNT(*) FROM products{where_pg}", *args_pg)
        return int(val or 0)
    async with get_connection() as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM products{where_sq}", tuple(args_sq))
        row = await cur.fetchone(); await cur.close()
        return int((row[0] if row else 0) or 0)


async def fetch_product(sku: str) -> dict[str, Any] | None:
    """Return one product by SKU, enriched with categories/variants/attributes/media when available."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM products WHERE sku = $1", sku)
            if not row:
                return None
            out = _decode_product_row(row)
            try:
                cats = await conn.fetch("""SELECT c.*, pc.is_primary, pc.marketplace_mappings_json AS link_marketplace_mappings_json
                    FROM categories c JOIN product_categories pc ON pc.category_id=c.id WHERE pc.product_sku=$1 ORDER BY pc.is_primary DESC, c.name ASC""", sku)
                out["categories"] = [dict(r) for r in cats]
            except Exception:
                out.setdefault("categories", [])
            try:
                vars_ = await conn.fetch("SELECT * FROM product_variants WHERE parent_sku=$1 OR sku=$1 ORDER BY sku ASC", sku)
                out["variants"] = [ _decode_json_fields(dict(r), ("attributes_json","raw_source_data_json")) for r in vars_]
            except Exception:
                out.setdefault("variants", [])
            try:
                attrs = await conn.fetch("SELECT * FROM product_attributes WHERE product_sku=$1 ORDER BY name ASC, value ASC", sku)
                out["attributes"] = [ _decode_json_fields(dict(r), ("extra_json",)) for r in attrs]
            except Exception:
                out.setdefault("attributes", [])
            try:
                media = await conn.fetch("SELECT * FROM product_media WHERE product_sku=$1 ORDER BY position ASC, created_at ASC", sku)
                out["media"] = [ _decode_json_fields(dict(r), ("metadata_json",)) for r in media]
            except Exception:
                out.setdefault("media", [])
        return out
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM products WHERE sku = ?", (sku,))
        row = await cursor.fetchone(); await cursor.close()
        if not row:
            return None
        out = _decode_product_row(row)
        for key, q in (
            ("categories", "SELECT c.*, pc.is_primary, pc.marketplace_mappings_json AS link_marketplace_mappings_json FROM categories c JOIN product_categories pc ON pc.category_id=c.id WHERE pc.product_sku=? ORDER BY pc.is_primary DESC, c.name ASC"),
            ("variants", "SELECT * FROM product_variants WHERE parent_sku=? OR sku=? ORDER BY sku ASC"),
            ("attributes", "SELECT * FROM product_attributes WHERE product_sku=? ORDER BY name ASC, value ASC"),
            ("media", "SELECT * FROM product_media WHERE product_sku=? ORDER BY position ASC, created_at ASC"),
        ):
            try:
                if key == "variants":
                    cur2 = await db.execute(q, (sku, sku))
                else:
                    cur2 = await db.execute(q, (sku,))
                rows = await cur2.fetchall(); await cur2.close()
                items=[]
                for r in rows:
                    d=dict(r)
                    if key=="categories" and isinstance(d.get("marketplace_mappings_json"), str):
                        try: d["marketplace_mappings_json"]=json.loads(d["marketplace_mappings_json"])
                        except Exception: pass
                    if key=="categories" and isinstance(d.get("link_marketplace_mappings_json"), str):
                        try: d["link_marketplace_mappings_json"]=json.loads(d["link_marketplace_mappings_json"])
                        except Exception: pass
                    if key=="variants": d=_decode_json_fields(d, ("attributes_json","raw_source_data_json"))
                    if key=="attributes": d=_decode_json_fields(d, ("extra_json",))
                    if key=="media": d=_decode_json_fields(d, ("metadata_json",))
                    items.append(d)
                out[key]=items
            except Exception:
                out.setdefault(key, [])
        return out


async def list_bundles() -> list[dict[str, Any]]:
    """Return all bundle definitions."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM bundles ORDER BY bundle_sku")
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM bundles ORDER BY bundle_sku")
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def upsert_bundle(bundle_sku: str, title: str, component_skus: Sequence[str]) -> None:
    """Create/update a bundle definition; its stock is recalculated atomically."""
    components = [str(item) for item in component_skus if str(item)]
    if not components:
        raise ValueError("A bundle must contain at least one component.")
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO products(sku, title, category, supplier_price, stock, is_bundle, status, source_hash, updated_at)
                       VALUES ($1, $2, 'Bundle', 0, 0, TRUE, 'paused', $3, NOW())
                       ON CONFLICT(sku) DO UPDATE SET title=EXCLUDED.title, is_bundle=TRUE, updated_at=NOW()""",
                    bundle_sku, title, json.dumps(components, separators=(",", ":")),
                )
                await conn.execute(
                    """INSERT INTO bundles(bundle_sku, component_skus_json, title, updated_at) VALUES ($1, $2::jsonb, $3, NOW())
                       ON CONFLICT(bundle_sku) DO UPDATE SET component_skus_json=EXCLUDED.component_skus_json, title=EXCLUDED.title, updated_at=NOW()""",
                    bundle_sku, json.dumps(components), title,
                )
                await _refresh_bundle_stock_pg(conn, bundle_sku)
        return
    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                """INSERT INTO products(sku, title, category, supplier_price, stock, is_bundle, status, source_hash, updated_at)
                   VALUES (?, ?, 'Bundle', 0, 0, 1, 'paused', ?, ?)
                   ON CONFLICT(sku) DO UPDATE SET title=excluded.title, is_bundle=1, updated_at=excluded.updated_at""",
                (bundle_sku, title, json.dumps(components, separators=(",", ":")), utc_now()),
            )
            await db.execute(
                """INSERT INTO bundles(bundle_sku, component_skus_json, title, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(bundle_sku) DO UPDATE SET component_skus_json=excluded.component_skus_json, title=excluded.title, updated_at=excluded.updated_at""",
                (bundle_sku, json.dumps(components), title, utc_now()),
            )
            await _refresh_bundle_stock_sqlite(db, bundle_sku)
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def _refresh_bundle_stock_pg(conn: Any, bundle_sku: str) -> tuple[int, str]:
    bundle = await conn.fetchrow("SELECT component_skus_json FROM bundles WHERE bundle_sku = $1", bundle_sku)
    if not bundle:
        raise DatabaseError(f"Bundle {bundle_sku!r} does not exist.")
    components = [str(item) for item in bundle["component_skus_json"]]
    rows = await conn.fetch("SELECT sku, stock FROM products WHERE sku = ANY($1::text[])", components)
    stock_map = {str(row["sku"]): int(row["stock"]) for row in rows}
    stock = min((stock_map.get(component, 0) for component in components), default=0)
    status = "active" if stock > 0 else "paused"
    await conn.execute("UPDATE products SET stock=$1, status=$2, updated_at=NOW() WHERE sku=$3", stock, status, bundle_sku)
    return stock, status


async def _refresh_bundle_stock_sqlite(conn: aiosqlite.Connection, bundle_sku: str) -> tuple[int, str]:
    cursor = await conn.execute("SELECT component_skus_json FROM bundles WHERE bundle_sku = ?", (bundle_sku,))
    bundle = await cursor.fetchone()
    await cursor.close()
    if not bundle:
        raise DatabaseError(f"Bundle {bundle_sku!r} does not exist.")
    components = [str(item) for item in json.loads(str(bundle["component_skus_json"]))]
    placeholders = ",".join("?" for _ in components)
    cursor = await conn.execute(f"SELECT sku, stock FROM products WHERE sku IN ({placeholders})", tuple(components))
    rows = await cursor.fetchall()
    await cursor.close()
    stock_map = {str(row["sku"]): int(row["stock"]) for row in rows}
    stock = min((stock_map.get(component, 0) for component in components), default=0)
    status = "active" if stock > 0 else "paused"
    await conn.execute("UPDATE products SET stock=?, status=?, updated_at=? WHERE sku=?", (stock, status, utc_now(), bundle_sku))
    return stock, status


async def refresh_all_bundle_stocks() -> None:
    """Recalculate every bundle under one backend-specific write transaction."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch("SELECT bundle_sku FROM bundles")
                for row in rows:
                    await _refresh_bundle_stock_pg(conn, str(row["bundle_sku"]))
        return
    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT bundle_sku FROM bundles")
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                await _refresh_bundle_stock_sqlite(db, str(row["bundle_sku"]))
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def create_order(*, order_id: str, sku: str, quantity: int, marketplace: str, customer_reference: str = "") -> None:
    """Persist an idempotent incoming order in received state."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO orders(order_id, sku, quantity, marketplace, customer_reference, created_at, updated_at)
                   VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                   ON CONFLICT(order_id) DO UPDATE SET quantity=EXCLUDED.quantity, marketplace=EXCLUDED.marketplace,
                     customer_reference=EXCLUDED.customer_reference, updated_at=NOW()""",
                order_id, sku, quantity, marketplace, customer_reference,
            )
        return
    now = utc_now()
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO orders(order_id, sku, quantity, marketplace, customer_reference, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(order_id) DO UPDATE SET quantity=excluded.quantity, marketplace=excluded.marketplace,
                 customer_reference=excluded.customer_reference, updated_at=excluded.updated_at""",
            (order_id, sku, quantity, marketplace, customer_reference, now, now),
        )
        await db.commit()


async def reserve_order_stock(order_id: str, sku: str, quantity: int) -> None:
    """Atomically reserve product or all bundle component stock for an order."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    "SELECT status, payment_status, fulfillment_locked FROM orders WHERE order_id=$1 FOR UPDATE",
                    order_id,
                )
                if not existing:
                    raise DatabaseError(f"Order {order_id} must be persisted before fulfillment.")
                if str(existing["payment_status"]).lower() != "paid" or bool(existing["fulfillment_locked"]):
                    raise FulfillmentBlockedError(
                        f"Fulfillment is blocked until order {order_id} is paid and unlocked."
                    )
                if existing and existing["status"] in {"reserved", "dispatching", "dispatched"}:
                    return
                product = await conn.fetchrow("SELECT sku, stock, is_bundle, status FROM products WHERE sku=$1 FOR UPDATE", sku)
                if not product or product["status"] != "active" or int(product["stock"]) < quantity:
                    raise InsufficientStockError(f"Insufficient active stock for {sku}.")
                if product["is_bundle"]:
                    bundle = await conn.fetchrow("SELECT component_skus_json FROM bundles WHERE bundle_sku=$1", sku)
                    if not bundle:
                        raise DatabaseError(f"Bundle metadata missing for {sku}.")
                    for component in [str(item) for item in bundle["component_skus_json"]]:
                        changed = await conn.fetchval(
                            """UPDATE products SET stock=stock-$1, updated_at=NOW()
                               WHERE sku=$2 AND is_bundle=FALSE AND status='active' AND stock >= $1 RETURNING 1""",
                            quantity, component,
                        )
                        if changed != 1:
                            raise InsufficientStockError(f"Insufficient component stock for {component}.")
                else:
                    changed = await conn.fetchval(
                        "UPDATE products SET stock=stock-$1, updated_at=NOW() WHERE sku=$2 AND status='active' AND stock >= $1 RETURNING 1",
                        quantity, sku,
                    )
                    if changed != 1:
                        raise InsufficientStockError(f"Insufficient stock for {sku}.")
                for row in await conn.fetch("SELECT bundle_sku FROM bundles"):
                    await _refresh_bundle_stock_pg(conn, str(row["bundle_sku"]))
                await conn.execute("UPDATE orders SET status='reserved', last_error='', updated_at=NOW() WHERE order_id=$1", order_id)
        return

    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT status, payment_status, fulfillment_locked FROM orders WHERE order_id=?",
                (order_id,),
            )
            existing = await cursor.fetchone()
            await cursor.close()
            if not existing:
                raise DatabaseError(f"Order {order_id} must be persisted before fulfillment.")
            if str(existing["payment_status"]).lower() != "paid" or bool(existing["fulfillment_locked"]):
                raise FulfillmentBlockedError(
                    f"Fulfillment is blocked until order {order_id} is paid and unlocked."
                )
            if existing and existing["status"] in {"reserved", "dispatching", "dispatched"}:
                await db.commit()
                return
            cursor = await db.execute("SELECT sku, stock, is_bundle, status FROM products WHERE sku=?", (sku,))
            product = await cursor.fetchone()
            await cursor.close()
            if not product or product["status"] != "active" or int(product["stock"]) < quantity:
                raise InsufficientStockError(f"Insufficient active stock for {sku}.")
            components: list[str] = [sku]
            if product["is_bundle"]:
                cursor = await db.execute("SELECT component_skus_json FROM bundles WHERE bundle_sku=?", (sku,))
                bundle = await cursor.fetchone()
                await cursor.close()
                if not bundle:
                    raise DatabaseError(f"Bundle metadata missing for {sku}.")
                components = [str(item) for item in json.loads(str(bundle["component_skus_json"]))]
            for component in components:
                cursor = await db.execute(
                    """UPDATE products SET stock=stock-?, updated_at=?
                       WHERE sku=? AND status='active' AND stock >= ? AND (is_bundle=0 OR ?=?)""",
                    (quantity, utc_now(), component, quantity, component, sku),
                )
                changed = cursor.rowcount
                await cursor.close()
                if changed != 1:
                    raise InsufficientStockError(f"Insufficient component stock for {component}.")
            cursor = await db.execute("SELECT bundle_sku FROM bundles")
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                await _refresh_bundle_stock_sqlite(db, str(row["bundle_sku"]))
            await db.execute("UPDATE orders SET status='reserved', last_error='', updated_at=? WHERE order_id=?", (utc_now(), order_id))
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def update_order_status(order_id: str, status: str, *, tracking_number: str = "", tracking_url: str = "", error: str = "") -> None:
    """Update one order with fulfillment status, tracking data or error."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE orders SET status=$1, tracking_number=$2, tracking_url=$3, last_error=$4, updated_at=NOW() WHERE order_id=$5", status, tracking_number, tracking_url, error, order_id)
        return
    async with get_connection() as db:
        await db.execute("UPDATE orders SET status=?, tracking_number=?, tracking_url=?, last_error=?, updated_at=? WHERE order_id=?", (status, tracking_number, tracking_url, error, utc_now(), order_id))
        await db.commit()




async def fetch_order(order_id: str) -> dict[str, Any] | None:
    """Fetch a single order by id (best-effort, backend-neutral)."""
    if not order_id:
        return None
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM orders WHERE order_id = $1", order_id)
        return dict(row) if row else None
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
        row = await cur.fetchone(); await cur.close()
        return dict(row) if row else None

async def list_orders(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Return orders with bounded pagination."""
    safe_limit, safe_offset = min(max(limit, 1), 500), max(offset, 0)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM orders ORDER BY created_at DESC, order_id ASC LIMIT $1 OFFSET $2", safe_limit, safe_offset)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM orders ORDER BY created_at DESC, order_id ASC LIMIT ? OFFSET ?", (safe_limit, safe_offset))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def count_dashboard_metrics() -> dict[str, int]:
    """Return compact dashboard counters."""
    query = """SELECT
        (SELECT COUNT(*) FROM products) AS product_count,
        (SELECT COUNT(*) FROM products WHERE status='active') AS active_product_count,
        (SELECT COUNT(*) FROM orders WHERE status IN ('received', 'reserved', 'dispatching')) AS pending_order_count,
        (SELECT COUNT(*) FROM system_logs WHERE level='ERROR' AND created_at >= %s) AS error_count"""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query % "NOW() - INTERVAL '24 hours'")
        return {key: int(value) for key, value in dict(row).items()} if row else {}
    async with get_connection() as db:
        cursor = await db.execute(query % "datetime('now', '-24 hours')")
        row = await cursor.fetchone()
        await cursor.close()
        return {key: int(row[key]) for key in row.keys()} if row else {}


async def start_import_run(source: str, *, mode: str = "", feed_url: str = "") -> int:
    """Create an import-run record and return its id."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            return int(await conn.fetchval("INSERT INTO import_runs(source, mode, feed_url) VALUES ($1,$2,$3) RETURNING id", source, mode or "", feed_url or ""))
    async with get_connection() as db:
        cursor = await db.execute("INSERT INTO import_runs(source, mode, feed_url, started_at) VALUES (?, ?, ?, ?)", (source, mode or "", feed_url or "", utc_now()))
        await db.commit()
        run_id = int(cursor.lastrowid or 0)
        await cursor.close()
        return run_id


async def finish_import_run(run_id: int, *, imported: int = 0, updated: int = 0, errors: int = 0, error_message: str = "", skipped: int = 0, created_count: int | None = None, updated_count: int | None = None, variants_linked: int = 0, status: str | None = None, summary: dict[str, Any] | None = None, protocol: dict[str, Any] | None = None) -> None:
    """Complete an import-run record."""
    run_status = status or ("success" if errors == 0 else "failed")
    cc = created_count if created_count is not None else max(imported - updated, 0)
    uc = updated_count if updated_count is not None else updated
    sj = json.dumps(summary or {}, ensure_ascii=False, default=str)
    pj = json.dumps(protocol or {}, ensure_ascii=False, default=str)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE import_runs SET finished_at=NOW(), imported=$1, updated=$2, errors=$3, skipped=$4, created_count=$5, updated_count=$6, variants_linked=$7, status=$8, error_message=$9, summary_json=$10::jsonb, protocol_json=$11::jsonb WHERE id=$12",
                imported, updated, errors, skipped, cc, uc, variants_linked, run_status, (error_message or "")[:4000], sj, pj, run_id,
            )
        return
    async with get_connection() as db:
        await db.execute(
            "UPDATE import_runs SET finished_at=?, imported=?, updated=?, errors=?, skipped=?, created_count=?, updated_count=?, variants_linked=?, status=?, error_message=?, summary_json=?, protocol_json=? WHERE id=?",
            (utc_now(), imported, updated, errors, skipped, cc, uc, variants_linked, run_status, (error_message or "")[:4000], sj, pj, run_id),
        )
        await db.commit()


async def recent_import_runs(limit: int = 10) -> list[dict[str, Any]]:
    """Return recent import-run records."""
    safe_limit = min(max(limit, 1), 50)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM import_runs ORDER BY id DESC LIMIT $1", safe_limit)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM import_runs ORDER BY id DESC LIMIT ?", (safe_limit,))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


# ════════════════════════════════════════════════════════════════════════════
# Google Merchant Center - Account Management
# ════════════════════════════════════════════════════════════════════════════

async def google_upsert_account(*, account_id: str, merchant_id: str, account_name: str,
                                 google_cloud_project_id: str, oauth_token_encrypted: str,
                                 oauth_refresh_token_encrypted: str | None = None,
                                 service_account_encrypted: str | None = None,
                                 is_primary: bool = False) -> None:
    """Upsert a Google Merchant Center account."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO google_accounts(id, merchant_id, account_name, google_cloud_project_id,
                   oauth_token_encrypted, oauth_refresh_token_encrypted, service_account_encrypted,
                   is_primary, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW())
                   ON CONFLICT(id) DO UPDATE SET
                     merchant_id=EXCLUDED.merchant_id, account_name=EXCLUDED.account_name,
                     google_cloud_project_id=EXCLUDED.google_cloud_project_id,
                     oauth_token_encrypted=EXCLUDED.oauth_token_encrypted,
                     oauth_refresh_token_encrypted=EXCLUDED.oauth_refresh_token_encrypted,
                     service_account_encrypted=EXCLUDED.service_account_encrypted,
                     is_primary=EXCLUDED.is_primary, updated_at=NOW()""",
                account_id, merchant_id, account_name, google_cloud_project_id,
                oauth_token_encrypted, oauth_refresh_token_encrypted, service_account_encrypted, is_primary,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO google_accounts(id, merchant_id, account_name, google_cloud_project_id,
               oauth_token_encrypted, oauth_refresh_token_encrypted, service_account_encrypted,
               is_active, is_primary, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,1,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 merchant_id=excluded.merchant_id, account_name=excluded.account_name,
                 google_cloud_project_id=excluded.google_cloud_project_id,
                 oauth_token_encrypted=excluded.oauth_token_encrypted,
                 oauth_refresh_token_encrypted=excluded.oauth_refresh_token_encrypted,
                 service_account_encrypted=excluded.service_account_encrypted,
                 is_primary=excluded.is_primary, updated_at=excluded.updated_at""",
            (account_id, merchant_id, account_name, google_cloud_project_id,
             oauth_token_encrypted, oauth_refresh_token_encrypted, service_account_encrypted,
             is_primary, utc_now(), utc_now()),
        )
        await db.commit()


async def google_list_accounts() -> list[dict[str, Any]]:
    """List all Google Merchant Center accounts."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM google_accounts ORDER BY account_name")
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM google_accounts ORDER BY account_name")
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def google_get_account(account_id: str) -> dict[str, Any] | None:
    """Get a single Google account by ID."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM google_accounts WHERE id = $1", account_id)
        return dict(row) if row else None
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM google_accounts WHERE id = ?", (account_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None


# ════════════════════════════════════════════════════════════════════════════
# Google Merchant Center - Data Sources
# ════════════════════════════════════════════════════════════════════════════

async def google_upsert_data_source(*, ds_id: str, account_id: str, data_source_id: str,
                                     display_name: str, primary_country: str = "DE",
                                     primary_language: str = "de", primary_currency: str = "EUR") -> None:
    """Upsert a Google data source."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO google_data_sources(id, account_id, data_source_id, display_name,
                   primary_country, primary_language, primary_currency, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,NOW())
                   ON CONFLICT(id) DO UPDATE SET
                     display_name=EXCLUDED.display_name, primary_country=EXCLUDED.primary_country,
                     primary_language=EXCLUDED.primary_language, primary_currency=EXCLUDED.primary_currency,
                     updated_at=NOW()""",
                ds_id, account_id, data_source_id, display_name, primary_country,
                primary_language, primary_currency,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO google_data_sources(id, account_id, data_source_id, display_name,
               source_type, status, primary_country, primary_language, primary_currency,
               feed_label, created_at, updated_at)
               VALUES (?,?,?,?,'products','active',?,?,?, '', ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 display_name=excluded.display_name, primary_country=excluded.primary_country,
                 primary_language=excluded.primary_language, primary_currency=excluded.primary_currency,
                 updated_at=excluded.updated_at""",
            (ds_id, account_id, data_source_id, display_name, primary_country,
             primary_language, primary_currency, utc_now(), utc_now()),
        )
        await db.commit()


async def google_list_data_sources(account_id: str | None = None) -> list[dict[str, Any]]:
    """List data sources, optionally filtered by account."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if account_id:
                rows = await conn.fetch("SELECT * FROM google_data_sources WHERE account_id = $1 ORDER BY display_name", account_id)
            else:
                rows = await conn.fetch("SELECT * FROM google_data_sources ORDER BY display_name")
        return [dict(row) for row in rows]
    async with get_connection() as db:
        if account_id:
            cursor = await db.execute("SELECT * FROM google_data_sources WHERE account_id = ? ORDER BY display_name", (account_id,))
        else:
            cursor = await db.execute("SELECT * FROM google_data_sources ORDER BY display_name")
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


# ════════════════════════════════════════════════════════════════════════════
# Google Merchant Center - Product Exports
# ════════════════════════════════════════════════════════════════════════════

async def google_upsert_export(*, export_id: str, account_id: str, product_sku: str,
                                offer_id: str, status: str = "pending",
                                payload_json: str = "{}", response_json: str = "{}",
                                error_message: str = "", max_retries: int = 3) -> None:
    """Upsert a Google product export record."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO google_product_exports(id, account_id, product_sku, offer_id,
                   status, payload_json, response_json, error_message, max_retries, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9,NOW())
                   ON CONFLICT(product_sku, offer_id) DO UPDATE SET
                     status=EXCLUDED.status, payload_json=EXCLUDED.payload_json,
                     response_json=EXCLUDED.response_json, error_message=EXCLUDED.error_message,
                     max_retries=EXCLUDED.max_retries, updated_at=NOW()""",
                export_id, account_id, product_sku, offer_id, status,
                payload_json, response_json, error_message, max_retries,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO google_product_exports(id, account_id, product_sku, google_product_id,
               offer_id, status, payload_json, response_json, error_message,
               max_retries, created_at, updated_at)
               VALUES (?,?,?,'',?,?,?,?,?,?,?,?)
               ON CONFLICT(product_sku, offer_id) DO UPDATE SET
                 status=excluded.status, payload_json=excluded.payload_json,
                 response_json=excluded.response_json, error_message=excluded.error_message,
                 max_retries=excluded.max_retries, updated_at=excluded.updated_at""",
            (export_id, account_id, product_sku, offer_id, status, payload_json,
             response_json, error_message, max_retries, utc_now(), utc_now()),
        )
        await db.commit()


async def google_list_exports(account_id: str | None = None, status: str | None = None,
                                limit: int = 100) -> list[dict[str, Any]]:
    """List Google product exports with optional filtering."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            query = "SELECT * FROM google_product_exports WHERE 1=1"
            params: list[Any] = []
            if account_id:
                query += f" AND account_id = ${len(params) + 1}"
                params.append(account_id)
            if status:
                query += f" AND status = ${len(params) + 1}"
                params.append(status)
            query += f" ORDER BY created_at DESC LIMIT ${len(params) + 1}"
            params.append(limit)
            rows = await conn.fetch(query, *params)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        query = "SELECT * FROM google_product_exports WHERE 1=1"
        params: list[Any] = []
        if account_id:
            query += " AND account_id = ?"
            params.append(account_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = await db.execute(query, tuple(params))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


# ════════════════════════════════════════════════════════════════════════════
# Google Merchant Center - Sync Jobs
# ════════════════════════════════════════════════════════════════════════════

async def google_upsert_sync_job(*, job_id: str, account_id: str, job_type: str,
                                  total_products: int = 0) -> None:
    """Upsert a Google sync job record."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO google_sync_jobs(id, account_id, job_type, total_products, started_at)
                   VALUES ($1,$2,$3,$4,NOW())
                   ON CONFLICT(id) DO UPDATE SET
                     total_products=EXCLUDED.total_products, updated_at=NOW()""",
                job_id, account_id, job_type, total_products,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO google_sync_jobs(id, account_id, job_type, status, total_products,
               processed_products, failed_products, started_at, created_at)
               VALUES (?,?,'pending',?,0,0,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 total_products=excluded.total_products, updated_at=excluded.updated_at""",
            (job_id, account_id, job_type, total_products, utc_now(), utc_now()),
        )
        await db.commit()


async def google_list_sync_jobs(account_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """List Google sync jobs."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if account_id:
                rows = await conn.fetch("SELECT * FROM google_sync_jobs WHERE account_id = $1 ORDER BY created_at DESC LIMIT $2", account_id, limit)
            else:
                rows = await conn.fetch("SELECT * FROM google_sync_jobs ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        if account_id:
            cursor = await db.execute("SELECT * FROM google_sync_jobs WHERE account_id = ? ORDER BY created_at DESC LIMIT ?", (account_id, limit))
        else:
            cursor = await db.execute("SELECT * FROM google_sync_jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


# ════════════════════════════════════════════════════════════════════════════
# OmniRoute - Provider Management
# ════════════════════════════════════════════════════════════════════════════

async def ai_upsert_provider(*, provider_id: str, name: str, provider_type: str,
                              api_endpoint: str, api_key_encrypted: str,
                              auth_type: str = "bearer", priority: int = 100,
                              rate_limit_per_minute: int = 60, timeout_seconds: int = 30,
                              max_retries: int = 3, retry_delay_seconds: int = 1,
                              circuit_breaker_threshold: int = 5,
                              circuit_breaker_timeout_seconds: int = 300,
                              capabilities_json: str = "[]") -> None:
    """Upsert an AI provider."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ai_providers(id, name, provider_type, api_endpoint,
                   api_key_encrypted, auth_type, priority, rate_limit_per_minute,
                   timeout_seconds, max_retries, retry_delay_seconds,
                   circuit_breaker_threshold, circuit_breaker_timeout_seconds,
                   capabilities_json, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,NOW())
                   ON CONFLICT(id) DO UPDATE SET
                     name=EXCLUDED.name, provider_type=EXCLUDED.provider_type,
                     api_endpoint=EXCLUDED.api_endpoint, api_key_encrypted=EXCLUDED.api_key_encrypted,
                     auth_type=EXCLUDED.auth_type, priority=EXCLUDED.priority,
                     rate_limit_per_minute=EXCLUDED.rate_limit_per_minute,
                     timeout_seconds=EXCLUDED.timeout_seconds, max_retries=EXCLUDED.max_retries,
                     retry_delay_seconds=EXCLUDED.retry_delay_seconds,
                     circuit_breaker_threshold=EXCLUDED.circuit_breaker_threshold,
                     circuit_breaker_timeout_seconds=EXCLUDED.circuit_breaker_timeout_seconds,
                     capabilities_json=EXCLUDED.capabilities_json, updated_at=NOW()""",
                provider_id, name, provider_type, api_endpoint, api_key_encrypted,
                auth_type, priority, rate_limit_per_minute, timeout_seconds, max_retries,
                retry_delay_seconds, circuit_breaker_threshold, circuit_breaker_timeout_seconds,
                capabilities_json,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO ai_providers(id, name, provider_type, api_endpoint,
               api_key_encrypted, auth_type, is_active, priority, rate_limit_per_minute,
               timeout_seconds, max_retries, retry_delay_seconds,
               circuit_breaker_threshold, circuit_breaker_timeout_seconds,
               capabilities_json, health_status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,'unknown',?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, provider_type=excluded.provider_type,
                 api_endpoint=excluded.api_endpoint, api_key_encrypted=excluded.api_key_encrypted,
                 auth_type=excluded.auth_type, priority=excluded.priority,
                 rate_limit_per_minute=excluded.rate_limit_per_minute,
                 timeout_seconds=excluded.timeout_seconds, max_retries=excluded.max_retries,
                 retry_delay_seconds=excluded.retry_delay_seconds,
                 circuit_breaker_threshold=excluded.circuit_breaker_threshold,
                 circuit_breaker_timeout_seconds=excluded.circuit_breaker_timeout_seconds,
                 capabilities_json=excluded.capabilities_json, updated_at=excluded.updated_at""",
            (provider_id, name, provider_type, api_endpoint, api_key_encrypted, auth_type,
             priority, rate_limit_per_minute, timeout_seconds, max_retries,
             retry_delay_seconds, circuit_breaker_threshold, circuit_breaker_timeout_seconds,
             capabilities_json, utc_now(), utc_now()),
        )
        await db.commit()


async def ai_list_providers() -> list[dict[str, Any]]:
    """List all AI providers."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM ai_providers ORDER BY priority, name")
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM ai_providers ORDER BY priority, name")
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


# ════════════════════════════════════════════════════════════════════════════
# OmniRoute - Model Management
# ════════════════════════════════════════════════════════════════════════════

async def ai_upsert_model(*, model_id: str, provider_id: str, model_id_str: str,
                           model_name: str, model_category: str = "general",
                           context_window: int = 4096,
                           input_price_per_1k: float = 0.0,
                           output_price_per_1k: float = 0.0,
                           image_price_per_image: float = 0.0,
                           rate_limit_per_minute: int = 60,
                           capabilities_json: str = "[]",
                           is_active: bool = True) -> None:
    """Upsert an AI model."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ai_models(id, provider_id, model_id, model_name,
                   model_category, context_window, input_price_per_1k,
                   output_price_per_1k, image_price_per_image, rate_limit_per_minute,
                   capabilities_json, is_active, updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12,NOW())
                   ON CONFLICT(provider_id, model_id) DO UPDATE SET
                     model_name=EXCLUDED.model_name, model_category=EXCLUDED.model_category,
                     context_window=EXCLUDED.context_window,
                     input_price_per_1k=EXCLUDED.input_price_per_1k,
                     output_price_per_1k=EXCLUDED.output_price_per_1k,
                     image_price_per_image=EXCLUDED.image_price_per_image,
                     rate_limit_per_minute=EXCLUDED.rate_limit_per_minute,
                     capabilities_json=EXCLUDED.capabilities_json,
                     is_active=EXCLUDED.is_active, updated_at=NOW()""",
                model_id, provider_id, model_id_str, model_name, model_category,
                context_window, input_price_per_1k, output_price_per_1k,
                image_price_per_image, rate_limit_per_minute, capabilities_json,
                is_active,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO ai_models(id, provider_id, model_id, model_name,
               model_category, context_window, input_price_per_1k,
               output_price_per_1k, image_price_per_image, rate_limit_per_minute,
               capabilities_json, is_active, is_discoverable, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1,1,?,?)
               ON CONFLICT(provider_id, model_id) DO UPDATE SET
                 model_name=excluded.model_name, model_category=excluded.model_category,
                 context_window=excluded.context_window,
                 input_price_per_1k=excluded.input_price_per_1k,
                 output_price_per_1k=excluded.output_price_per_1k,
                 image_price_per_image=excluded.image_price_per_image,
                 rate_limit_per_minute=excluded.rate_limit_per_minute,
                 capabilities_json=excluded.capabilities_json,
                 is_active=excluded.is_active, updated_at=excluded.updated_at""",
            (model_id, provider_id, model_id_str, model_name, model_category,
             context_window, input_price_per_1k, output_price_per_1k,
             image_price_per_image, rate_limit_per_minute, capabilities_json,
             is_active, utc_now(), utc_now()),
        )
        await db.commit()


async def ai_list_models(provider_id: str | None = None) -> list[dict[str, Any]]:
    """List AI models, optionally filtered by provider."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if provider_id:
                rows = await conn.fetch("SELECT * FROM ai_models WHERE provider_id = $1 ORDER BY model_name", provider_id)
            else:
                rows = await conn.fetch("SELECT * FROM ai_models ORDER BY model_name")
        return [dict(row) for row in rows]
    async with get_connection() as db:
        if provider_id:
            cursor = await db.execute("SELECT * FROM ai_models WHERE provider_id = ? ORDER BY model_name", (provider_id,))
        else:
            cursor = await db.execute("SELECT * FROM ai_models ORDER BY model_name")
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


# ════════════════════════════════════════════════════════════════════════════
# OmniRoute - Usage Records
# ════════════════════════════════════════════════════════════════════════════

async def ai_insert_usage_record(*, record_id: str, provider_id: str,
                                  model_id: str | None, workflow_id: str | None,
                                  request_type: str, tokens_input: int = 0,
                                  tokens_output: int = 0, images_generated: int = 0,
                                  videos_generated: int = 0, cost_usd: float = 0.0,
                                  latency_ms: int = 0, status: str = "success",
                                  error_message: str = "") -> None:
    """Insert an AI usage record."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ai_usage_records(id, provider_id, model_id, workflow_id,
                   request_type, tokens_input, tokens_output, images_generated,
                   videos_generated, cost_usd, latency_ms, status, error_message)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
                record_id, provider_id, model_id, workflow_id, request_type,
                tokens_input, tokens_output, images_generated, videos_generated,
                cost_usd, latency_ms, status, error_message,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO ai_usage_records(id, provider_id, model_id, workflow_id,
               request_type, tokens_input, tokens_output, images_generated,
               videos_generated, cost_usd, latency_ms, status, error_message, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record_id, provider_id, model_id, workflow_id, request_type,
             tokens_input, tokens_output, images_generated, videos_generated,
             cost_usd, latency_ms, status, error_message, utc_now()),
        )
        await db.commit()


async def ai_list_usage_records(limit: int = 100) -> list[dict[str, Any]]:
    """List recent AI usage records."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM ai_usage_records ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM ai_usage_records ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


# ════════════════════════════════════════════════════════════════════════════
# GTIN / Product Intelligence helpers
# ════════════════════════════════════════════════════════════════════════════

async def gtin_upsert_provider(provider: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid
    pid = provider.get("id") or str(_uuid.uuid4())
    name = str(provider.get("name") or "").strip().lower()
    endpoint = str(provider.get("endpoint") or "")
    auth_ref = str(provider.get("auth_ref") or "")
    priority = int(provider.get("priority") or 100)
    rate = int(provider.get("rate_limit_per_min") or 60)
    license_info = str(provider.get("license_info") or "")
    cats = provider.get("supported_categories_json") or provider.get("supported_categories") or []
    cfg = provider.get("config_json") or provider.get("config") or {}
    enabled = bool(provider.get("enabled", False))
    cats_s = json.dumps(cats, ensure_ascii=False) if not isinstance(cats, str) else cats
    cfg_s = json.dumps(cfg, ensure_ascii=False) if not isinstance(cfg, str) else cfg
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO gtin_providers(id,name,endpoint,auth_ref,priority,rate_limit_per_min,license_info,supported_categories_json,config_json,enabled)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10)
                   ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,endpoint=EXCLUDED.endpoint,auth_ref=EXCLUDED.auth_ref,priority=EXCLUDED.priority,
                   rate_limit_per_min=EXCLUDED.rate_limit_per_min,license_info=EXCLUDED.license_info,supported_categories_json=EXCLUDED.supported_categories_json,
                   config_json=EXCLUDED.config_json,enabled=EXCLUDED.enabled,updated_at=NOW()""",
                pid, name, endpoint, auth_ref, priority, rate, license_info, cats_s, cfg_s, enabled,
            )
            row = await conn.fetchrow("SELECT * FROM gtin_providers WHERE id=$1", pid)
        out = dict(row) if row else {}
    else:
        async with get_connection() as db:
            cur = await db.execute("SELECT id FROM gtin_providers WHERE id=?", (pid,))
            ex = await cur.fetchone(); await cur.close()
            if ex:
                await db.execute("UPDATE gtin_providers SET name=?,endpoint=?,auth_ref=?,priority=?,rate_limit_per_min=?,license_info=?,supported_categories_json=?,config_json=?,enabled=?,updated_at=? WHERE id=?",
                    (name,endpoint,auth_ref,priority,rate,license_info,cats_s,cfg_s,1 if enabled else 0,utc_now(),pid))
            else:
                await db.execute("INSERT INTO gtin_providers(id,name,endpoint,auth_ref,priority,rate_limit_per_min,license_info,supported_categories_json,config_json,enabled) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (pid,name,endpoint,auth_ref,priority,rate,license_info,cats_s,cfg_s,1 if enabled else 0))
            await db.commit()
            cur = await db.execute("SELECT * FROM gtin_providers WHERE id=?", (pid,))
            row = await cur.fetchone(); await cur.close(); out = dict(row) if row else {}
    if "enabled" in out: out["enabled"]=bool(out["enabled"])
    for k in ("supported_categories_json","config_json"):
        if isinstance(out.get(k), str):
            try: out[k]=json.loads(out[k])
            except Exception: pass
    return out


async def gtin_list_providers(enabled_only: bool = False) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM gtin_providers " + ("WHERE enabled=TRUE " if enabled_only else "") + "ORDER BY priority ASC, name ASC")
        out=[dict(r) for r in rows]
    else:
        async with get_connection() as db:
            q = "SELECT * FROM gtin_providers " + ("WHERE enabled=1 " if enabled_only else "") + "ORDER BY priority ASC, name ASC"
            cur = await db.execute(q); rows=await cur.fetchall(); await cur.close(); out=[dict(r) for r in rows]
    for r in out:
        if "enabled" in r: r["enabled"]=bool(r["enabled"])
        for k in ("supported_categories_json","config_json"):
            if isinstance(r.get(k), str):
                try: r[k]=json.loads(r[k])
                except Exception: pass
    return out


async def gtin_save_lookup(gtin: str, result: dict[str, Any]) -> str:
    import uuid as _uuid
    lid = str(_uuid.uuid4())
    payload = json.dumps(result, ensure_ascii=False, default=str)
    status = str(((result.get("reconciliation") or {}).get("status")) or result.get("status") or "done")
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO gtin_lookups(id,gtin,result_json,status) VALUES ($1,$2,$3::jsonb,$4)", lid, gtin, payload, status)
    else:
        async with get_connection() as db:
            await db.execute("INSERT INTO gtin_lookups(id,gtin,result_json,status) VALUES (?,?,?,?)", (lid, gtin, payload, status))
            await db.commit()
    return lid


async def gtin_list_lookups(limit: int = 100) -> list[dict[str, Any]]:
    limit = min(max(limit,1),500)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM gtin_lookups ORDER BY created_at DESC LIMIT $1", limit)
        out=[dict(r) for r in rows]
    else:
        async with get_connection() as db:
            cur = await db.execute("SELECT * FROM gtin_lookups ORDER BY created_at DESC LIMIT ?", (limit,))
            rows=await cur.fetchall(); await cur.close(); out=[dict(r) for r in rows]
    for r in out:
        if isinstance(r.get("result_json"), str):
            try: r["result_json"]=json.loads(r["result_json"])
            except Exception: pass
    return out


async def gtin_save_enrichment(**kw: Any) -> str:
    import uuid as _uuid
    eid = kw.get("id") or str(_uuid.uuid4())
    meta = kw.get("metadata") or kw.get("metadata_json") or {}
    meta_s = json.dumps(meta, ensure_ascii=False) if not isinstance(meta, str) else meta
    vals = (eid, kw.get("entity_type","product"), kw.get("entity_id",""), kw.get("field_name",""), str(kw.get("original_value","")),
            str(kw.get("normalized_value","")), str(kw.get("source_provider","")), str(kw.get("source_record_id","")),
            float(kw.get("confidence",0.0) or 0.0), str(kw.get("trust_level","C")), str(kw.get("verification_state","adopted")),
            str(kw.get("license_info","")), meta_s)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO gtin_enrichments(id,entity_type,entity_id,field_name,original_value,normalized_value,source_provider,source_record_id,confidence,trust_level,verification_state,license_info,metadata_json) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb)", *vals)
    else:
        async with get_connection() as db:
            await db.execute("INSERT INTO gtin_enrichments(id,entity_type,entity_id,field_name,original_value,normalized_value,source_provider,source_record_id,confidence,trust_level,verification_state,license_info,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", vals)
            await db.commit()
    return eid


async def gtin_list_enrichments(entity_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    limit=min(max(limit,1),500)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if entity_id: rows = await conn.fetch("SELECT * FROM gtin_enrichments WHERE entity_id=$1 ORDER BY created_at DESC LIMIT $2", entity_id, limit)
            else: rows = await conn.fetch("SELECT * FROM gtin_enrichments ORDER BY created_at DESC LIMIT $1", limit)
        out=[dict(r) for r in rows]
    else:
        async with get_connection() as db:
            if entity_id:
                cur=await db.execute("SELECT * FROM gtin_enrichments WHERE entity_id=? ORDER BY created_at DESC LIMIT ?", (entity_id,limit))
            else:
                cur=await db.execute("SELECT * FROM gtin_enrichments ORDER BY created_at DESC LIMIT ?", (limit,))
            rows=await cur.fetchall(); await cur.close(); out=[dict(r) for r in rows]
    for r in out:
        if isinstance(r.get("metadata_json"), str):
            try: r["metadata_json"]=json.loads(r["metadata_json"])
            except Exception: pass
    return out


async def gtin_save_conflict(**kw: Any) -> str:
    import uuid as _uuid
    cid = kw.get("id") or str(_uuid.uuid4())
    cand = kw.get("candidates") or kw.get("candidates_json") or []
    reso = kw.get("resolution") or kw.get("resolution_json") or {}
    cand_s=json.dumps(cand,ensure_ascii=False) if not isinstance(cand,str) else cand
    res_s=json.dumps(reso,ensure_ascii=False) if not isinstance(reso,str) else reso
    vals=(cid, kw.get("entity_type","product"), kw.get("entity_id",""), kw.get("field_name",""), cand_s, str(kw.get("status","open")), res_s)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO gtin_conflicts(id,entity_type,entity_id,field_name,candidates_json,status,resolution_json) VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7::jsonb)", *vals)
    else:
        async with get_connection() as db:
            await db.execute("INSERT INTO gtin_conflicts(id,entity_type,entity_id,field_name,candidates_json,status,resolution_json) VALUES (?,?,?,?,?,?,?)", vals)
            await db.commit()
    return cid


async def gtin_list_conflicts(status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    limit=min(max(limit,1),500)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if status: rows=await conn.fetch("SELECT * FROM gtin_conflicts WHERE status=$1 ORDER BY created_at DESC LIMIT $2", status, limit)
            else: rows=await conn.fetch("SELECT * FROM gtin_conflicts ORDER BY created_at DESC LIMIT $1", limit)
        out=[dict(r) for r in rows]
    else:
        async with get_connection() as db:
            if status: cur=await db.execute("SELECT * FROM gtin_conflicts WHERE status=? ORDER BY created_at DESC LIMIT ?", (status,limit))
            else: cur=await db.execute("SELECT * FROM gtin_conflicts ORDER BY created_at DESC LIMIT ?", (limit,))
            rows=await cur.fetchall(); await cur.close(); out=[dict(r) for r in rows]
    for r in out:
        for k in ("candidates_json","resolution_json"):
            if isinstance(r.get(k), str):
                try: r[k]=json.loads(r[k])
                except Exception: pass
    return out


# ════════════════════════════════════════════════════════════════════════════
# Multi-channel Content helpers
# ════════════════════════════════════════════════════════════════════════════

async def content_version_create(product_sku: str, channel: str, content: dict[str, Any], status: str = "draft", model: str = "", prompt_version: str = "") -> str:
    import uuid as _uuid
    vid = str(_uuid.uuid4())
    cjson = json.dumps(content, ensure_ascii=False, default=str)
    vjson = json.dumps(content.get("validation") or {}, ensure_ascii=False, default=str)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO content_versions(id,product_sku,channel,status,model,prompt_version,content_json,validation_json) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb)", vid, product_sku, channel, status, model, prompt_version, cjson, vjson)
    else:
        async with get_connection() as db:
            await db.execute("INSERT INTO content_versions(id,product_sku,channel,status,model,prompt_version,content_json,validation_json) VALUES (?,?,?,?,?,?,?,?)", (vid, product_sku, channel, status, model, prompt_version, cjson, vjson))
            await db.commit()
    return vid


async def content_version_list(product_sku: str | None = None, channel: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    limit=min(max(limit,1),500)
    if uses_postgres():
        pool = await get_pg_pool(); q="SELECT * FROM content_versions WHERE 1=1"; params=[]
        if product_sku: q+=f" AND product_sku=${len(params)+1}"; params.append(product_sku)
        if channel: q+=f" AND channel=${len(params)+1}"; params.append(channel)
        q+=f" ORDER BY created_at DESC LIMIT ${len(params)+1}"; params.append(limit)
        async with pool.acquire() as conn: rows=await conn.fetch(q,*params)
        out=[dict(r) for r in rows]
    else:
        async with get_connection() as db:
            q="SELECT * FROM content_versions WHERE 1=1"; params=[]
            if product_sku: q+=" AND product_sku=?"; params.append(product_sku)
            if channel: q+=" AND channel=?"; params.append(channel)
            q+=" ORDER BY created_at DESC LIMIT ?"; params.append(limit)
            cur=await db.execute(q,tuple(params)); rows=await cur.fetchall(); await cur.close(); out=[dict(r) for r in rows]
    for r in out:
        for k in ("content_json","validation_json"):
            if isinstance(r.get(k), str):
                try: r[k]=json.loads(r[k])
                except Exception: pass
    return out


# ════════════════════════════════════════════════════════════════════════════
# IAM helpers (incremental)
# ════════════════════════════════════════════════════════════════════════════

async def iam_create_user(*, email: str, password_hash: str = "", display_name: str = "", username: str = "", is_superadmin: bool = False, is_active: bool = True) -> dict[str, Any]:
    import uuid as _uuid
    uid=str(_uuid.uuid4())
    if uses_postgres():
        pool=await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO iam_users(id,email,username,password_hash,display_name,is_active,is_superadmin) VALUES ($1,$2,$3,$4,$5,$6,$7)", uid,email,username,password_hash,display_name,is_active,is_superadmin)
            row=await conn.fetchrow("SELECT id,email,username,display_name,is_active,is_superadmin,created_at FROM iam_users WHERE id=$1", uid)
        return dict(row) if row else {"id":uid}
    async with get_connection() as db:
        await db.execute("INSERT INTO iam_users(id,email,username,password_hash,display_name,is_active,is_superadmin) VALUES (?,?,?,?,?,?,?)", (uid,email,username,password_hash,display_name,1 if is_active else 0,1 if is_superadmin else 0))
        await db.commit()
        cur=await db.execute("SELECT id,email,username,display_name,is_active,is_superadmin,created_at FROM iam_users WHERE id=?", (uid,))
        row=await cur.fetchone(); await cur.close(); return dict(row) if row else {"id":uid}


async def iam_get_user_by_email(email: str) -> dict[str, Any] | None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM iam_users WHERE email=$1", email)
        return dict(row) if row else None
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM iam_users WHERE email=?", (email,))
        row = await cur.fetchone(); await cur.close()
        return dict(row) if row else None


async def iam_get_user(user_id: str) -> dict[str, Any] | None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM iam_users WHERE id=$1", user_id)
        return dict(row) if row else None
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM iam_users WHERE id=?", (user_id,))
        row = await cur.fetchone(); await cur.close()
        return dict(row) if row else None


async def iam_list_users(limit: int = 200) -> list[dict[str, Any]]:
    limit = min(max(limit, 1), 500)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id,email,username,display_name,is_active,is_superadmin,last_login_at,created_at FROM iam_users ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]
    async with get_connection() as db:
        cur = await db.execute("SELECT id,email,username,display_name,is_active,is_superadmin,last_login_at,created_at FROM iam_users ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = await cur.fetchall(); await cur.close()
        return [dict(r) for r in rows]


async def iam_update_user(user_id: str, **fields: Any) -> bool:
    allowed = {"display_name", "username", "is_active", "is_superadmin", "mfa_required", "password_hash", "last_login_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    if uses_postgres():
        pool = await get_pg_pool(); sets=[]; params=[]
        for i,(k,v) in enumerate(updates.items(),1): sets.append(f"{k}=${i}"); params.append(v)
        params.append(user_id)
        async with pool.acquire() as conn:
            await conn.execute(f"UPDATE iam_users SET {', '.join(sets)}, updated_at=NOW() WHERE id=${len(params)}", *params)
        return True
    async with get_connection() as db:
        sets=[]; params=[]
        for k,v in updates.items():
            sets.append(f"{k}=?"); params.append((1 if v else 0) if isinstance(v,bool) else v)
        params.extend([utc_now(), user_id])
        await db.execute(f"UPDATE iam_users SET {', '.join(sets)}, updated_at=? WHERE id=?", tuple(params))
        await db.commit(); return True


async def iam_ensure_role(name: str, description: str = "") -> dict[str, Any]:
    import uuid as _uuid
    rid = str(_uuid.uuid4())
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO iam_roles(id,name,description) VALUES ($1,$2,$3) ON CONFLICT(name) DO UPDATE SET description=EXCLUDED.description", rid, name, description)
            row = await conn.fetchrow("SELECT * FROM iam_roles WHERE name=$1", name)
        return dict(row) if row else {"name": name}
    async with get_connection() as db:
        cur = await db.execute("SELECT id FROM iam_roles WHERE name=?", (name,)); ex = await cur.fetchone(); await cur.close()
        if not ex: await db.execute("INSERT INTO iam_roles(id,name,description) VALUES (?,?,?)", (rid,name,description))
        else: await db.execute("UPDATE iam_roles SET description=? WHERE name=?", (description,name))
        await db.commit()
        cur = await db.execute("SELECT * FROM iam_roles WHERE name=?", (name,)); row = await cur.fetchone(); await cur.close()
        return dict(row) if row else {"name": name}


async def iam_assign_role(user_id: str, role_name: str) -> None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO iam_user_roles(user_id,role_name) VALUES ($1,$2) ON CONFLICT DO NOTHING", user_id, role_name)
        return
    async with get_connection() as db:
        await db.execute("INSERT OR IGNORE INTO iam_user_roles(user_id,role_name) VALUES (?,?)", (user_id,role_name)); await db.commit()


async def iam_list_roles() -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn: rows = await conn.fetch("SELECT * FROM iam_roles ORDER BY name")
        return [dict(r) for r in rows]
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM iam_roles ORDER BY name"); rows = await cur.fetchall(); await cur.close(); return [dict(r) for r in rows]


async def iam_list_permissions(role_name: str | None = None) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if role_name: rows = await conn.fetch("SELECT * FROM iam_permissions WHERE role_name=$1 ORDER BY permission", role_name)
            else: rows = await conn.fetch("SELECT * FROM iam_permissions ORDER BY role_name,permission")
        return [dict(r) for r in rows]
    async with get_connection() as db:
        if role_name: cur = await db.execute("SELECT * FROM iam_permissions WHERE role_name=? ORDER BY permission", (role_name,))
        else: cur = await db.execute("SELECT * FROM iam_permissions ORDER BY role_name,permission")
        rows = await cur.fetchall(); await cur.close(); return [dict(r) for r in rows]


async def iam_grant_permission(role_name: str, permission: str) -> None:
    import uuid as _uuid
    pid = str(_uuid.uuid4())
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO iam_permissions(id,role_name,permission) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING", pid, role_name, permission)
        return
    async with get_connection() as db:
        await db.execute("INSERT OR IGNORE INTO iam_permissions(id,role_name,permission) VALUES (?,?,?)", (pid,role_name,permission)); await db.commit()



async def iam_create_session(*, session_id: str, user_id: str, session_token_hash: str, ip_address: str = "", user_agent: str = "", expires_at: str | None = None) -> dict[str, Any]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO iam_sessions(id,user_id,session_token_hash,ip_address,user_agent,expires_at,revoked,created_at,last_seen_at) VALUES ($1,$2,$3,$4,$5,$6,FALSE,NOW(),NOW())",
                session_id, user_id, session_token_hash, ip_address, user_agent, _pg_ts(expires_at),
            )
            row = await conn.fetchrow("SELECT * FROM iam_sessions WHERE id=$1", session_id)
        return dict(row) if row else {"id": session_id}
    async with get_connection() as db:
        now = utc_now()
        await db.execute(
            "INSERT INTO iam_sessions(id,user_id,session_token_hash,ip_address,user_agent,expires_at,revoked,created_at,last_seen_at) VALUES (?,?,?,?,?,?,0,?,?)",
            (session_id, user_id, session_token_hash, ip_address, user_agent, expires_at, now, now),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM iam_sessions WHERE id=?", (session_id,))
        row = await cur.fetchone(); await cur.close()
        return dict(row) if row else {"id": session_id}


async def iam_get_session(session_id: str) -> dict[str, Any] | None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM iam_sessions WHERE id=$1", session_id)
        return dict(row) if row else None
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM iam_sessions WHERE id=?", (session_id,))
        row = await cur.fetchone(); await cur.close()
        return dict(row) if row else None


async def iam_revoke_session(session_id: str) -> None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE iam_sessions SET revoked=TRUE WHERE id=$1", session_id)
        return
    async with get_connection() as db:
        await db.execute("UPDATE iam_sessions SET revoked=1 WHERE id=?", (session_id,))
        await db.commit()


async def iam_touch_session(session_id: str) -> None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE iam_sessions SET last_seen_at=NOW() WHERE id=$1", session_id)
        return
    async with get_connection() as db:
        await db.execute("UPDATE iam_sessions SET last_seen_at=? WHERE id=?", (utc_now(), session_id))
        await db.commit()


async def iam_get_user_roles(user_id: str) -> list[str]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT role_name FROM iam_user_roles WHERE user_id=$1", user_id)
        return [str(r["role_name"]) for r in rows]
    async with get_connection() as db:
        cur = await db.execute("SELECT role_name FROM iam_user_roles WHERE user_id=?", (user_id,))
        rows = await cur.fetchall(); await cur.close()
        return [str(r["role_name"]) for r in rows]

# ── Persistent Job System helpers ─────────────────────────────────────────

async def job_create(*, job_type: str, payload: dict[str, Any] | None = None, run_at: str | None = None, max_attempts: int = 3, created_by: str = "", correlation_id: str = "") -> dict[str, Any]:
    import uuid as _uuid
    jid = str(_uuid.uuid4())
    now = utc_now()
    payload_json = json.dumps(payload or {}, ensure_ascii=False, default=str)
    run_at_val = run_at or now
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO jobs(id,type,status,payload_json,result_json,error_message,attempts,max_attempts,run_at,created_by,correlation_id,created_at,updated_at) VALUES ($1,$2,'pending',$3::jsonb,'{}'::jsonb,'',0,$4,$5,$6,$7,NOW(),NOW()) RETURNING *",
                jid, job_type, payload_json, max_attempts, _pg_ts(run_at_val), created_by, correlation_id,
            )
        return dict(row) if row else {"id": jid}
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO jobs(id,type,status,payload_json,result_json,error_message,attempts,max_attempts,run_at,created_by,correlation_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, job_type, "pending", payload_json, "{}", "", 0, max_attempts, run_at_val, created_by, correlation_id, now, now),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM jobs WHERE id=?", (jid,))
        row = await cur.fetchone(); await cur.close()
        return dict(row) if row else {"id": jid}


async def job_list(*, limit: int = 100, status: str | None = None, job_type: str | None = None) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        q = "SELECT * FROM jobs WHERE 1=1"; args=[]; i=1
        if status: q += f" AND status=${i}"; args.append(status); i+=1
        if job_type: q += f" AND type=${i}"; args.append(job_type); i+=1
        q += f" ORDER BY created_at DESC LIMIT ${i}"; args.append(limit)
        async with pool.acquire() as conn:
            rows = await conn.fetch(q, *args)
        return [dict(r) for r in rows]
    async with get_connection() as db:
        q = "SELECT * FROM jobs WHERE 1=1"; args=[]
        if status: q += " AND status=?"; args.append(status)
        if job_type: q += " AND type=?"; args.append(job_type)
        q += " ORDER BY created_at DESC LIMIT ?"; args.append(limit)
        cur = await db.execute(q, tuple(args))
        rows = await cur.fetchall(); await cur.close()
        return [dict(r) for r in rows]


async def job_get(job_id: str) -> dict[str, Any] | None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM jobs WHERE id=$1", job_id)
        return dict(row) if row else None
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        row = await cur.fetchone(); await cur.close()
        return dict(row) if row else None


async def job_update(job_id: str, **fields: Any) -> bool:
    if not fields: return False
    if uses_postgres():
        sets=[]; vals=[]; i=1
        for k,v in fields.items():
            if k in {"payload_json","result_json"} and not isinstance(v,str): v=json.dumps(v,ensure_ascii=False,default=str)
            if k in {"run_at","locked_at","started_at","finished_at","created_at","updated_at"}: v=_pg_ts(v)
            sets.append(f"{k}=${i}"); vals.append(v); i+=1
        sets.append("updated_at=NOW()")
        vals.append(job_id)
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=${i}", *vals)
        return res.endswith('1')
    async with get_connection() as db:
        sets=[]; vals=[]
        for k,v in fields.items():
            if k in {"payload_json","result_json"} and not isinstance(v,str): v=json.dumps(v,ensure_ascii=False,default=str)
            sets.append(f"{k}=?"); vals.append(v)
        sets.append("updated_at=?"); vals.append(utc_now()); vals.append(job_id)
        cur = await db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", tuple(vals))
        await db.commit(); ok = cur.rowcount>0; await cur.close(); return ok


async def job_claim_next(worker_id: str = "worker-1") -> dict[str, Any] | None:
    now = utc_now()
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE jobs SET status='running', locked_by=$1, locked_at=NOW(), started_at=COALESCE(started_at,NOW()), attempts=attempts+1, updated_at=NOW()
                   WHERE id=(SELECT id FROM jobs WHERE status IN ('pending','retry') AND run_at <= NOW() ORDER BY run_at ASC, created_at ASC FOR UPDATE SKIP LOCKED LIMIT 1)
                   RETURNING *""", worker_id)
        return dict(row) if row else None
    async with get_connection() as db:
        cur = await db.execute("SELECT id FROM jobs WHERE status IN ('pending','retry') AND run_at <= ? ORDER BY run_at ASC, created_at ASC LIMIT 1", (now,))
        row = await cur.fetchone(); await cur.close()
        if not row: return None
        jid = row['id'] if isinstance(row, dict) or hasattr(row,'keys') else row[0]
        await db.execute("UPDATE jobs SET status='running', locked_by=?, locked_at=?, started_at=COALESCE(started_at,?), attempts=attempts+1, updated_at=? WHERE id=? AND status IN ('pending','retry')", (worker_id, now, now, now, jid))
        await db.commit()
        return await job_get(str(jid))


async def job_complete(job_id: str, result: dict[str, Any] | None = None) -> None:
    await job_update(job_id, status='succeeded', result_json=result or {}, finished_at=utc_now(), error_message='')


async def job_fail(job_id: str, error: str, *, retry: bool = False, delay_seconds: int = 60) -> None:
    j = await job_get(job_id) or {}
    attempts = int(j.get('attempts') or 0)
    max_attempts = int(j.get('max_attempts') or 3)
    if retry and attempts < max_attempts:
        from datetime import datetime, timedelta, timezone
        run_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
        await job_update(job_id, status='retry', error_message=str(error)[:2000], run_at=run_at)
    else:
        await job_update(job_id, status='failed', error_message=str(error)[:2000], finished_at=utc_now())


async def job_cancel(job_id: str) -> bool:
    j = await job_get(job_id)
    if not j or j.get('status') in ('succeeded','failed','cancelled'): return False
    return await job_update(job_id, status='cancelled', finished_at=utc_now())


async def job_retry(job_id: str) -> bool:
    j = await job_get(job_id)
    if not j: return False
    return await job_update(job_id, status='retry', run_at=utc_now(), error_message='')


# ── Scheduler config helpers ──────────────────────────────────────────────

async def scheduler_list() -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM scheduler_config ORDER BY job_type ASC")
        return [dict(r) for r in rows]
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM scheduler_config ORDER BY job_type ASC")
        rows = await cur.fetchall(); await cur.close()
        return [dict(r) for r in rows]


async def scheduler_upsert(*, job_type: str, enabled: bool = True, interval_seconds: int = 3600, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    import uuid as _uuid
    sid = f"sched_{job_type}"
    now = utc_now()
    payload_json = json.dumps(payload or {}, ensure_ascii=False, default=str)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO scheduler_config(id,job_type,enabled,interval_seconds,payload_json,created_at,updated_at)
                   VALUES ($1,$2,$3,$4,$5::jsonb,NOW(),NOW())
                   ON CONFLICT(job_type) DO UPDATE SET enabled=EXCLUDED.enabled, interval_seconds=EXCLUDED.interval_seconds, payload_json=EXCLUDED.payload_json, updated_at=NOW()""",
                sid, job_type, enabled, interval_seconds, payload_json)
            row = await conn.fetchrow("SELECT * FROM scheduler_config WHERE job_type=$1", job_type)
        return dict(row) if row else {"job_type": job_type}
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO scheduler_config(id,job_type,enabled,interval_seconds,payload_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(job_type) DO UPDATE SET enabled=excluded.enabled, interval_seconds=excluded.interval_seconds, payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
            (sid, job_type, 1 if enabled else 0, interval_seconds, payload_json, now, now))
        await db.commit()
        cur = await db.execute("SELECT * FROM scheduler_config WHERE job_type=?", (job_type,))
        row = await cur.fetchone(); await cur.close()
        return dict(row) if row else {"job_type": job_type}


async def scheduler_mark_run(job_type: str, *, status: str = "", error: str = "") -> None:
    now = utc_now()
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE scheduler_config SET last_run_at=NOW(), last_status=$1, last_error=$2, updated_at=NOW() WHERE job_type=$3", status, error[:1000], job_type)
        return
    async with get_connection() as db:
        await db.execute("UPDATE scheduler_config SET last_run_at=?, last_status=?, last_error=?, updated_at=? WHERE job_type=?", (now, status, error[:1000], now, job_type))
        await db.commit()


# ── Matterhorn config/feeds/mappings/previews helpers ─────────────────────

async def matterhorn_get_config() -> dict[str, Any]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            cols = {r['column_name'] for r in await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name='matterhorn_config'")}
            if 'value_json' in cols:
                rows = await conn.fetch("SELECT key, value_json FROM matterhorn_config")
                out = {}
                for r in rows:
                    v = r['value_json']
                    if isinstance(v, str):
                        try: v = json.loads(v)
                        except Exception: v = {}
                    out[r['key']] = v or {}
                return out
            else:
                rows = await conn.fetch("SELECT key, value FROM matterhorn_config")
                return {r['key']: {"value": r['value']} for r in rows}
    async with get_connection() as db:
        try:
            cur = await db.execute("SELECT key, value_json FROM matterhorn_config")
            rows = await cur.fetchall(); await cur.close()
            out={}
            for r in rows:
                d=dict(r); v=d.get('value_json')
                if isinstance(v,str):
                    try: v=json.loads(v)
                    except Exception: v={}
                out[d['key']]=v or {}
            return out
        except Exception:
            cur = await db.execute("SELECT key, value FROM matterhorn_config")
            rows = await cur.fetchall(); await cur.close()
            return {dict(r)['key']: {"value": dict(r).get('value')} for r in rows}


async def matterhorn_set_config(key: str, value: dict[str, Any]) -> None:
    now = utc_now(); vj=json.dumps(value, ensure_ascii=False, default=str)
    plain = str(value.get('value','') if isinstance(value, dict) else value)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            cols = {r['column_name'] for r in await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name='matterhorn_config'")}
            if 'value_json' in cols and 'value' in cols:
                await conn.execute("INSERT INTO matterhorn_config(key,value,value_json,updated_at) VALUES ($1,$2,$3::jsonb,NOW()) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,value_json=EXCLUDED.value_json,updated_at=NOW()", key, plain, vj)
            elif 'value_json' in cols:
                await conn.execute("INSERT INTO matterhorn_config(key,value_json,updated_at) VALUES ($1,$2::jsonb,NOW()) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json,updated_at=NOW()", key, vj)
            else:
                await conn.execute("INSERT INTO matterhorn_config(key,value,updated_at) VALUES ($1,$2,NOW()) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=NOW()", key, plain)
        return
    async with get_connection() as db:
        try:
            await db.execute("INSERT INTO matterhorn_config(key,value_json,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at", (key, vj, now))
        except Exception:
            await db.execute("INSERT INTO matterhorn_config(key,value,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (key, plain, now))
        await db.commit()


async def matterhorn_list_feeds() -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM matterhorn_feeds ORDER BY updated_at DESC")
        out = [dict(r) for r in rows]
        for f in out:
            if not f.get('feed_url') and f.get('url'): f['feed_url'] = f.get('url')
            if 'url' not in f and f.get('feed_url'): f['url'] = f.get('feed_url')
        return out
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM matterhorn_feeds ORDER BY updated_at DESC")
        rows = await cur.fetchall(); await cur.close()
        out = [dict(r) for r in rows]
        for f in out:
            if not f.get('feed_url') and f.get('url'): f['feed_url'] = f.get('url')
        return out


async def matterhorn_upsert_feed(feed: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid
    fid = str(feed.get('id') or _uuid.uuid4())
    now = utc_now(); cfg=json.dumps(feed.get('config_json') or feed.get('config') or {}, ensure_ascii=False, default=str)
    enabled = bool(feed.get('enabled', True))
    name = feed.get('name') or 'Feed'
    feed_url = feed.get('feed_url') or feed.get('url') or ''
    feed_type = feed.get('feed_type') or 'xml'
    auth_header = feed.get('auth_header') or ''
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            # detect schema columns
            cols = {r['column_name'] for r in await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name='matterhorn_feeds'")}
            has_url = 'url' in cols
            has_feed_url = 'feed_url' in cols
            has_auth = 'auth_header' in cols
            if has_url and has_feed_url:
                await conn.execute("""INSERT INTO matterhorn_feeds(id,name,url,feed_url,feed_type,enabled,auth_header,config_json,created_at,updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,NOW(),NOW())
                    ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,url=EXCLUDED.url,feed_url=EXCLUDED.feed_url,feed_type=EXCLUDED.feed_type,enabled=EXCLUDED.enabled,auth_header=EXCLUDED.auth_header,config_json=EXCLUDED.config_json,updated_at=NOW()""",
                    fid, name, feed_url, feed_url, feed_type, enabled, auth_header, cfg)
            elif has_url:
                await conn.execute("""INSERT INTO matterhorn_feeds(id,name,url,feed_type,enabled,auth_header,config_json,created_at,updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,NOW(),NOW())
                    ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,url=EXCLUDED.url,feed_type=EXCLUDED.feed_type,enabled=EXCLUDED.enabled,auth_header=EXCLUDED.auth_header,config_json=EXCLUDED.config_json,updated_at=NOW()""",
                    fid, name, feed_url, feed_type, enabled, auth_header, cfg)
            else:
                await conn.execute("""INSERT INTO matterhorn_feeds(id,name,feed_url,feed_type,enabled,config_json,created_at,updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6::jsonb,NOW(),NOW())
                    ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,feed_url=EXCLUDED.feed_url,feed_type=EXCLUDED.feed_type,enabled=EXCLUDED.enabled,config_json=EXCLUDED.config_json,updated_at=NOW()""",
                    fid, name, feed_url, feed_type, enabled, cfg)
            row = await conn.fetchrow("SELECT * FROM matterhorn_feeds WHERE id=$1", fid)
        d = dict(row) if row else {"id": fid}
        if not d.get('feed_url') and d.get('url'): d['feed_url']=d.get('url')
        return d
    async with get_connection() as db:
        # try new schema, fallback old
        try:
            await db.execute("""INSERT INTO matterhorn_feeds(id,name,feed_url,feed_type,enabled,config_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, feed_url=excluded.feed_url, feed_type=excluded.feed_type, enabled=excluded.enabled, config_json=excluded.config_json, updated_at=excluded.updated_at""",
                (fid, name, feed_url, feed_type, 1 if enabled else 0, cfg, now, now))
        except Exception:
            await db.execute("""INSERT INTO matterhorn_feeds(id,name,url,feed_type,enabled,config_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, url=excluded.url, feed_type=excluded.feed_type, enabled=excluded.enabled, config_json=excluded.config_json, updated_at=excluded.updated_at""",
                (fid, name, feed_url, feed_type, 1 if enabled else 0, cfg, now, now))
        await db.commit()
        cur = await db.execute("SELECT * FROM matterhorn_feeds WHERE id=?", (fid,))
        row = await cur.fetchone(); await cur.close()
        d = dict(row) if row else {"id": fid}
        if not d.get('feed_url') and d.get('url'): d['feed_url']=d.get('url')
        return d


async def matterhorn_delete_feed(feed_id: str) -> bool:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            res = await conn.execute("DELETE FROM matterhorn_feeds WHERE id=$1", feed_id)
        return str(res).endswith('1')
    async with get_connection() as db:
        cur = await db.execute("DELETE FROM matterhorn_feeds WHERE id=?", (feed_id,))
        await db.commit(); ok=cur.rowcount>0; await cur.close(); return ok


async def matterhorn_list_mappings() -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM matterhorn_mappings ORDER BY updated_at DESC")
        out=[dict(r) for r in rows]
        for m in out:
            if 'enabled' not in m and 'is_active' in m: m['enabled']=m.get('is_active')
        return out
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM matterhorn_mappings ORDER BY updated_at DESC")
        rows = await cur.fetchall(); await cur.close()
        return [dict(r) for r in rows]


async def matterhorn_upsert_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid
    mid = str(mapping.get('id') or _uuid.uuid4())
    now = utc_now(); cfg=json.dumps(mapping.get('config_json') or mapping.get('config') or {}, ensure_ascii=False, default=str)
    enabled = bool(mapping.get('enabled', mapping.get('is_active', True)))
    feed_id = mapping.get('feed_id') or ''
    source_field = mapping.get('source_field') or ''
    target_field = mapping.get('target_field') or ''
    transform = mapping.get('transform') or 'direct'
    priority = int(mapping.get('priority') or 0)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            cols = {r['column_name'] for r in await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name='matterhorn_mappings'")}
            if 'feed_id' in cols:
                await conn.execute("""INSERT INTO matterhorn_mappings(id,feed_id,source_field,target_field,transform,enabled,config_json,priority,is_active,created_at,updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,NOW(),NOW())
                    ON CONFLICT(id) DO UPDATE SET feed_id=EXCLUDED.feed_id,source_field=EXCLUDED.source_field,target_field=EXCLUDED.target_field,transform=EXCLUDED.transform,enabled=EXCLUDED.enabled,config_json=EXCLUDED.config_json,priority=EXCLUDED.priority,is_active=EXCLUDED.is_active,updated_at=NOW()""",
                    mid, feed_id, source_field, target_field, transform, enabled, cfg, priority, enabled)
            else:
                await conn.execute("""INSERT INTO matterhorn_mappings(id,source_field,target_field,transform,enabled,config_json,created_at,updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6::jsonb,NOW(),NOW())
                    ON CONFLICT(id) DO UPDATE SET source_field=EXCLUDED.source_field,target_field=EXCLUDED.target_field,transform=EXCLUDED.transform,enabled=EXCLUDED.enabled,config_json=EXCLUDED.config_json,updated_at=NOW()""",
                    mid, source_field, target_field, transform, enabled, cfg)
            row = await conn.fetchrow("SELECT * FROM matterhorn_mappings WHERE id=$1", mid)
        d=dict(row) if row else {"id": mid}
        if 'enabled' not in d and 'is_active' in d: d['enabled']=d.get('is_active')
        return d
    async with get_connection() as db:
        await db.execute("""INSERT INTO matterhorn_mappings(id,source_field,target_field,transform,enabled,config_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET source_field=excluded.source_field,target_field=excluded.target_field,transform=excluded.transform,enabled=excluded.enabled,config_json=excluded.config_json,updated_at=excluded.updated_at""",
            (mid, source_field, target_field, transform, 1 if enabled else 0, cfg, now, now))
        await db.commit()
        cur = await db.execute("SELECT * FROM matterhorn_mappings WHERE id=?", (mid,))
        row = await cur.fetchone(); await cur.close()
        return dict(row) if row else {"id": mid}


async def matterhorn_delete_mapping(mapping_id: str) -> bool:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            res = await conn.execute("DELETE FROM matterhorn_mappings WHERE id=$1", mapping_id)
        return str(res).endswith('1')
    async with get_connection() as db:
        cur = await db.execute("DELETE FROM matterhorn_mappings WHERE id=?", (mapping_id,))
        await db.commit(); ok=cur.rowcount>0; await cur.close(); return ok


async def matterhorn_save_preview(*, preview_id: str, feed_url: str, status: str, summary: dict[str, Any], items: list[dict[str, Any]], validation: dict[str, Any], source_hash: str = "") -> dict[str, Any]:
    now = utc_now()
    sj=json.dumps(summary,ensure_ascii=False,default=str); ij=json.dumps(items,ensure_ascii=False,default=str); vj=json.dumps(validation,ensure_ascii=False,default=str)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO matterhorn_import_previews(id,feed_url,status,summary_json,items_json,validation_json,source_hash,created_at) VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6::jsonb,$7,NOW())", preview_id, feed_url, status, sj, ij, vj, source_hash)
            row = await conn.fetchrow("SELECT * FROM matterhorn_import_previews WHERE id=$1", preview_id)
        return dict(row) if row else {"id": preview_id}
    async with get_connection() as db:
        await db.execute("INSERT INTO matterhorn_import_previews(id,feed_url,status,summary_json,items_json,validation_json,source_hash,created_at) VALUES (?,?,?,?,?,?,?,?)", (preview_id, feed_url, status, sj, ij, vj, source_hash, now))
        await db.commit()
        cur = await db.execute("SELECT * FROM matterhorn_import_previews WHERE id=?", (preview_id,))
        row = await cur.fetchone(); await cur.close()
        return dict(row) if row else {"id": preview_id}


async def matterhorn_list_previews(limit: int = 50) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, feed_url, status, summary_json, validation_json, source_hash, created_at FROM matterhorn_import_previews ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]
    async with get_connection() as db:
        cur = await db.execute("SELECT id, feed_url, status, summary_json, validation_json, source_hash, created_at FROM matterhorn_import_previews ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = await cur.fetchall(); await cur.close()
        out=[]
        for r in rows:
            d=dict(r)
            for k in ("summary_json","validation_json"):
                if isinstance(d.get(k), str):
                    try: d[k]=json.loads(d[k])
                    except Exception: pass
            out.append(d)
        return out


async def matterhorn_get_preview(preview_id: str) -> dict[str, Any] | None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM matterhorn_import_previews WHERE id=$1", preview_id)
        return dict(row) if row else None
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM matterhorn_import_previews WHERE id=?", (preview_id,))
        row = await cur.fetchone(); await cur.close()
        if not row: return None
        d=dict(row)
        for k in ("summary_json","items_json","validation_json"):
            if isinstance(d.get(k), str):
                try: d[k]=json.loads(d[k])
                except Exception: pass
        return d


async def list_import_runs(limit: int = 10) -> list[dict[str, Any]]:
    return await recent_import_runs(limit)

# ── News & Knowledge helpers ──────────────────────────────────────────────

async def news_list_sources() -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM news_sources ORDER BY updated_at DESC")
        return [dict(r) for r in rows]
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM news_sources ORDER BY updated_at DESC")
        rows = await cur.fetchall(); await cur.close()
        return [dict(r) for r in rows]


async def news_upsert_source(src: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid
    sid = str(src.get('id') or _uuid.uuid4()); now = utc_now()
    cfg = json.dumps(src.get('config_json') or src.get('config') or {}, ensure_ascii=False, default=str)
    enabled = src.get('enabled', True)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""INSERT INTO news_sources(id,name,url,category,enabled,config_json,created_at,updated_at) VALUES ($1,$2,$3,$4,$5,$6::jsonb,NOW(),NOW())
                ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,url=EXCLUDED.url,category=EXCLUDED.category,enabled=EXCLUDED.enabled,config_json=EXCLUDED.config_json,updated_at=NOW()""",
                sid, src.get('name') or 'Source', src.get('url') or '', src.get('category') or 'general', bool(enabled), cfg)
            row = await conn.fetchrow("SELECT * FROM news_sources WHERE id=$1", sid)
        return dict(row) if row else {"id": sid}
    async with get_connection() as db:
        await db.execute("""INSERT INTO news_sources(id,name,url,category,enabled,config_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url,category=excluded.category,enabled=excluded.enabled,config_json=excluded.config_json,updated_at=excluded.updated_at""",
            (sid, src.get('name') or 'Source', src.get('url') or '', src.get('category') or 'general', 1 if enabled else 0, cfg, now, now))
        await db.commit(); cur = await db.execute("SELECT * FROM news_sources WHERE id=?", (sid,)); row=await cur.fetchone(); await cur.close()
        return dict(row) if row else {"id": sid}


async def news_list_items(limit: int = 100, category: str | None = None) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        q = "SELECT * FROM news_items"; args=[]; i=1
        if category: q += f" WHERE category=${i}"; args.append(category); i+=1
        q += f" ORDER BY created_at DESC LIMIT ${i}"; args.append(limit)
        async with pool.acquire() as conn: rows = await conn.fetch(q, *args)
        return [dict(r) for r in rows]
    async with get_connection() as db:
        q = "SELECT * FROM news_items"; args=[]
        if category: q += " WHERE category=?"; args.append(category)
        q += " ORDER BY created_at DESC LIMIT ?"; args.append(limit)
        cur = await db.execute(q, tuple(args)); rows=await cur.fetchall(); await cur.close()
        return [dict(r) for r in rows]


async def news_add_item(item: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid
    iid = str(item.get('id') or _uuid.uuid4()); now = utc_now()
    meta = json.dumps(item.get('metadata_json') or item.get('metadata') or {}, ensure_ascii=False, default=str)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO news_items(id,source_id,title,url,summary,category,published_at,metadata_json,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,NOW())",
                iid, str(item.get('source_id') or ''), str(item.get('title') or ''), str(item.get('url') or ''), str(item.get('summary') or ''), str(item.get('category') or 'general'), item.get('published_at'), meta)
            row = await conn.fetchrow("SELECT * FROM news_items WHERE id=$1", iid)
        return dict(row) if row else {"id": iid}
    async with get_connection() as db:
        await db.execute("INSERT INTO news_items(id,source_id,title,url,summary,category,published_at,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (iid, str(item.get('source_id') or ''), str(item.get('title') or ''), str(item.get('url') or ''), str(item.get('summary') or ''), str(item.get('category') or 'general'), item.get('published_at'), meta, now))
        await db.commit(); cur=await db.execute("SELECT * FROM news_items WHERE id=?", (iid,)); row=await cur.fetchone(); await cur.close()
        return dict(row) if row else {"id": iid}


async def knowledge_list(limit: int = 100) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM knowledge_entries ORDER BY updated_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM knowledge_entries ORDER BY updated_at DESC LIMIT ?", (limit,))
        rows = await cur.fetchall(); await cur.close()
        return [dict(r) for r in rows]


async def knowledge_upsert(entry: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid
    eid = str(entry.get('id') or _uuid.uuid4()); now = utc_now()
    tags = json.dumps(entry.get('tags_json') or entry.get('tags') or [], ensure_ascii=False, default=str)
    meta = json.dumps(entry.get('metadata_json') or entry.get('metadata') or {}, ensure_ascii=False, default=str)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""INSERT INTO knowledge_entries(id,title,content,category,tags_json,source,metadata_json,created_at,updated_at) VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7::jsonb,NOW(),NOW())
                ON CONFLICT(id) DO UPDATE SET title=EXCLUDED.title,content=EXCLUDED.content,category=EXCLUDED.category,tags_json=EXCLUDED.tags_json,source=EXCLUDED.source,metadata_json=EXCLUDED.metadata_json,updated_at=NOW()""",
                eid, entry.get('title') or '', entry.get('content') or '', entry.get('category') or 'general', tags, entry.get('source') or '', meta)
            row = await conn.fetchrow("SELECT * FROM knowledge_entries WHERE id=$1", eid)
        return dict(row) if row else {"id": eid}
    async with get_connection() as db:
        await db.execute("""INSERT INTO knowledge_entries(id,title,content,category,tags_json,source,metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title,content=excluded.content,category=excluded.category,tags_json=excluded.tags_json,source=excluded.source,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
            (eid, entry.get('title') or '', entry.get('content') or '', entry.get('category') or 'general', tags, entry.get('source') or '', meta, now, now))
        await db.commit(); cur=await db.execute("SELECT * FROM knowledge_entries WHERE id=?", (eid,)); row=await cur.fetchone(); await cur.close()
        return dict(row) if row else {"id": eid}


async def audit_list(limit: int = 200) -> list[dict[str, Any]]:
    """Unified audit view over structured system logs (best-effort)."""
    logs = await recent_logs(limit=min(max(limit,1),500))
    out=[]
    for l in logs:
        d=dict(l)
        ctx = d.get('context_json')
        if isinstance(ctx, str):
            try: d['context_json']=json.loads(ctx)
            except Exception: pass
        out.append(d)
    return out


async def analytics_overview() -> dict[str, Any]:
    """Compute lightweight analytics/KPIs from available real data."""
    metrics = await count_dashboard_metrics()
    runs = await recent_import_runs(20)
    jobs = await job_list(limit=50)
    ok_runs = sum(1 for r in runs if str(r.get('status'))=='success')
    failed_jobs = sum(1 for j in jobs if str(j.get('status'))=='failed')
    return {
        "metrics": metrics,
        "imports": {"recent": len(runs), "successful_recent": ok_runs},
        "jobs": {"recent": len(jobs), "failed_recent": failed_jobs},
        "generated_at": utc_now(),
    }

# ── Multi-Channel Connector Management Tables & Persistence Helpers ───────

async def connector_account_list(connector_id: str | None = None) -> list[dict[str, Any]]:
    """List configured seller accounts for channels."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if connector_id:
                rows = await conn.fetch("SELECT * FROM connector_accounts WHERE connector_id = $1 ORDER BY updated_at DESC", connector_id)
            else:
                rows = await conn.fetch("SELECT * FROM connector_accounts ORDER BY updated_at DESC")
            return [dict(r) for r in rows]
    async with get_connection() as db:
        if connector_id:
            cur = await db.execute("SELECT * FROM connector_accounts WHERE connector_id = ? ORDER BY updated_at DESC", (connector_id,))
        else:
            cur = await db.execute("SELECT * FROM connector_accounts ORDER BY updated_at DESC")
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]


async def connector_account_upsert(account: dict[str, Any]) -> dict[str, Any]:
    """Upsert a multi-channel seller account."""
    import uuid as _uuid
    aid = str(account.get("id") or _uuid.uuid4())
    cid = str(account.get("connector_id") or "")
    name = str(account.get("account_name") or "Default Account")
    marketplace = str(account.get("marketplace") or "DE")
    cfg = json.dumps(account.get("credentials_json") or account.get("config") or {}, ensure_ascii=False, default=str)
    is_active = bool(account.get("is_active", True))
    is_sandbox = bool(account.get("is_sandbox", False))
    now = utc_now()
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO connector_accounts (id, connector_id, account_name, marketplace, is_active, is_sandbox, credentials_json, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, NOW(), NOW())
                ON CONFLICT(id) DO UPDATE SET
                    account_name = EXCLUDED.account_name,
                    marketplace = EXCLUDED.marketplace,
                    is_active = EXCLUDED.is_active,
                    is_sandbox = EXCLUDED.is_sandbox,
                    credentials_json = EXCLUDED.credentials_json,
                    updated_at = NOW()
            """, aid, cid, name, marketplace, is_active, is_sandbox, cfg)
            row = await conn.fetchrow("SELECT * FROM connector_accounts WHERE id = $1", aid)
            return dict(row) if row else {"id": aid}
    async with get_connection() as db:
        await db.execute("""
            INSERT INTO connector_accounts (id, connector_id, account_name, marketplace, is_active, is_sandbox, credentials_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                account_name = excluded.account_name,
                marketplace = excluded.marketplace,
                is_active = excluded.is_active,
                is_sandbox = excluded.is_sandbox,
                credentials_json = excluded.credentials_json,
                updated_at = excluded.updated_at
        """, (aid, cid, name, marketplace, 1 if is_active else 0, 1 if is_sandbox else 0, cfg, now, now))
        await db.commit()
        cur = await db.execute("SELECT * FROM connector_accounts WHERE id = ?", (aid,))
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else {"id": aid}


async def connector_account_delete(account_id: str) -> bool:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            res = await conn.execute("DELETE FROM connector_accounts WHERE id = $1", account_id)
            return res.endswith("1")
    async with get_connection() as db:
        cur = await db.execute("DELETE FROM connector_accounts WHERE id = ?", (account_id,))
        await db.commit()
        ok = cur.rowcount > 0
        await cur.close()
        return ok


async def connector_mapping_list(connector_id: str | None = None) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if connector_id:
                rows = await conn.fetch("SELECT * FROM connector_mappings WHERE connector_id = $1 ORDER BY updated_at DESC", connector_id)
            else:
                rows = await conn.fetch("SELECT * FROM connector_mappings ORDER BY updated_at DESC")
            return [dict(r) for r in rows]
    async with get_connection() as db:
        if connector_id:
            cur = await db.execute("SELECT * FROM connector_mappings WHERE connector_id = ? ORDER BY updated_at DESC", (connector_id,))
        else:
            cur = await db.execute("SELECT * FROM connector_mappings ORDER BY updated_at DESC")
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]


async def connector_mapping_upsert(mapping: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid
    mid = str(mapping.get("id") or _uuid.uuid4())
    cid = str(mapping.get("connector_id") or "")
    src = str(mapping.get("canonical_field") or mapping.get("source_field") or "")
    tgt = str(mapping.get("channel_field") or mapping.get("target_field") or "")
    m_type = str(mapping.get("mapping_type") or "direct")
    cfg = json.dumps(mapping.get("config_json") or {}, ensure_ascii=False, default=str)
    now = utc_now()
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO connector_mappings (id, connector_id, canonical_field, channel_field, mapping_type, config_json, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, NOW(), NOW())
                ON CONFLICT(id) DO UPDATE SET
                    canonical_field = EXCLUDED.canonical_field,
                    channel_field = EXCLUDED.channel_field,
                    mapping_type = EXCLUDED.mapping_type,
                    config_json = EXCLUDED.config_json,
                    updated_at = NOW()
            """, mid, cid, src, tgt, m_type, cfg)
            row = await conn.fetchrow("SELECT * FROM connector_mappings WHERE id = $1", mid)
            return dict(row) if row else {"id": mid}
    async with get_connection() as db:
        await db.execute("""
            INSERT INTO connector_mappings (id, connector_id, canonical_field, channel_field, mapping_type, config_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                canonical_field = excluded.canonical_field,
                channel_field = excluded.channel_field,
                mapping_type = excluded.mapping_type,
                config_json = excluded.config_json,
                updated_at = excluded.updated_at
        """, (mid, cid, src, tgt, m_type, cfg, now, now))
        await db.commit()
        cur = await db.execute("SELECT * FROM connector_mappings WHERE id = ?", (mid,))
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else {"id": mid}


async def connector_export_enqueue(sku: str, connector_id: str, account_id: str = "") -> dict[str, Any]:
    import uuid as _uuid
    eid = str(_uuid.uuid4())
    now = utc_now()
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO connector_exports (id, connector_id, account_id, sku, status, payload_json, created_at, updated_at)
                VALUES ($1, $2, $3, $4, 'pending', '{}'::jsonb, NOW(), NOW())
            """, eid, connector_id, account_id, sku)
            row = await conn.fetchrow("SELECT * FROM connector_exports WHERE id = $1", eid)
            return dict(row) if row else {"id": eid}
    async with get_connection() as db:
        await db.execute("""
            INSERT INTO connector_exports (id, connector_id, account_id, sku, status, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', '{}', ?, ?)
        """, (eid, connector_id, account_id, sku, now, now))
        await db.commit()
        cur = await db.execute("SELECT * FROM connector_exports WHERE id = ?", (eid,))
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else {"id": eid}


async def connector_export_list(connector_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if connector_id:
                rows = await conn.fetch("SELECT * FROM connector_exports WHERE connector_id = $1 ORDER BY created_at DESC LIMIT $2", connector_id, limit)
            else:
                rows = await conn.fetch("SELECT * FROM connector_exports ORDER BY created_at DESC LIMIT $1", limit)
            return [dict(r) for r in rows]
    async with get_connection() as db:
        if connector_id:
            cur = await db.execute("SELECT * FROM connector_exports WHERE connector_id = ? ORDER BY created_at DESC LIMIT ?", (connector_id, limit))
        else:
            cur = await db.execute("SELECT * FROM connector_exports ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]


async def delete_product(sku: str) -> bool:
    """Delete a product by SKU. Returns True if deleted."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            res = await conn.execute("DELETE FROM products WHERE sku = $1", sku)
        return res.endswith("1")
    async with get_connection() as db:
        cur = await db.execute("DELETE FROM products WHERE sku = ?", (sku,))
        await db.commit()
        ok = cur.rowcount > 0
        await cur.close()
        return ok


async def delete_bundle(bundle_sku: str) -> bool:
    """Delete a bundle definition (product row remains managed separately if needed)."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            res = await conn.execute("DELETE FROM bundles WHERE bundle_sku = $1", bundle_sku)
        return res.endswith("1")
    async with get_connection() as db:
        cur = await db.execute("DELETE FROM bundles WHERE bundle_sku = ?", (bundle_sku,))
        await db.commit()
        ok = cur.rowcount > 0
        await cur.close()
        return ok


# ── CRM / Omnichannel Communication helpers ──────────────────────────────

async def crm_upsert_customer(data: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid
    cid = str(data.get('id') or _uuid.uuid4())
    now = utc_now()
    email = str(data.get('email') or '').strip().lower()
    name = str(data.get('display_name') or data.get('name') or email or 'Kunde')
    meta = json.dumps(data.get('metadata_json') or data.get('metadata') or {}, ensure_ascii=False, default=str)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO crm_customers(id,email,display_name,shopware_id,amazon_id,ebay_id,phone,metadata_json,created_at,updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,NOW(),NOW())
                   ON CONFLICT(id) DO UPDATE SET email=EXCLUDED.email,display_name=EXCLUDED.display_name,shopware_id=EXCLUDED.shopware_id,amazon_id=EXCLUDED.amazon_id,ebay_id=EXCLUDED.ebay_id,phone=EXCLUDED.phone,metadata_json=EXCLUDED.metadata_json,updated_at=NOW()""",
                cid, email, name, str(data.get('shopware_id') or ''), str(data.get('amazon_id') or ''), str(data.get('ebay_id') or ''), str(data.get('phone') or ''), meta)
            row = await conn.fetchrow("SELECT * FROM crm_customers WHERE id=$1", cid)
        return dict(row) if row else {"id": cid}
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO crm_customers(id,email,display_name,shopware_id,amazon_id,ebay_id,phone,metadata_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET email=excluded.email,display_name=excluded.display_name,shopware_id=excluded.shopware_id,amazon_id=excluded.amazon_id,ebay_id=excluded.ebay_id,phone=excluded.phone,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
            (cid,email,name,str(data.get('shopware_id') or ''),str(data.get('amazon_id') or ''),str(data.get('ebay_id') or ''),str(data.get('phone') or ''),meta,now,now))
        await db.commit(); cur=await db.execute("SELECT * FROM crm_customers WHERE id=?", (cid,)); row=await cur.fetchone(); await cur.close()
        return dict(row) if row else {"id": cid}


async def crm_get_customer(*, customer_id: str | None = None, email: str | None = None, channel: str | None = None, external_id: str | None = None) -> dict[str, Any] | None:
    if customer_id:
        if uses_postgres():
            pool = await get_pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM crm_customers WHERE id=$1", customer_id)
            return dict(row) if row else None
        async with get_connection() as db:
            cur=await db.execute("SELECT * FROM crm_customers WHERE id=?", (customer_id,)); row=await cur.fetchone(); await cur.close(); return dict(row) if row else None
    if email:
        em = email.strip().lower()
        if uses_postgres():
            pool = await get_pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM crm_customers WHERE lower(email)=$1 LIMIT 1", em)
            return dict(row) if row else None
        async with get_connection() as db:
            cur=await db.execute("SELECT * FROM crm_customers WHERE lower(email)=? LIMIT 1", (em,)); row=await cur.fetchone(); await cur.close(); return dict(row) if row else None
    if channel and external_id:
        col = {"shopware":"shopware_id","amazon":"amazon_id","ebay":"ebay_id"}.get(channel)
        if col:
            if uses_postgres():
                pool = await get_pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(f"SELECT * FROM crm_customers WHERE {col}=$1 LIMIT 1", external_id)
                return dict(row) if row else None
            async with get_connection() as db:
                cur=await db.execute(f"SELECT * FROM crm_customers WHERE {col}=? LIMIT 1", (external_id,)); row=await cur.fetchone(); await cur.close(); return dict(row) if row else None
    return None


async def crm_list_customers(limit: int = 100, query: str | None = None) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if query:
                q=f"%{query.lower()}%"; rows = await conn.fetch("SELECT * FROM crm_customers WHERE lower(email) LIKE $1 OR lower(display_name) LIKE $1 ORDER BY updated_at DESC LIMIT $2", q, limit)
            else:
                rows = await conn.fetch("SELECT * FROM crm_customers ORDER BY updated_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]
    async with get_connection() as db:
        if query:
            q=f"%{query.lower()}%"; cur=await db.execute("SELECT * FROM crm_customers WHERE lower(email) LIKE ? OR lower(display_name) LIKE ? ORDER BY updated_at DESC LIMIT ?", (q,q,limit))
        else:
            cur=await db.execute("SELECT * FROM crm_customers ORDER BY updated_at DESC LIMIT ?", (limit,))
        rows=await cur.fetchall(); await cur.close(); return [dict(r) for r in rows]


async def crm_upsert_thread(data: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid
    tid = str(data.get('id') or _uuid.uuid4())
    now = utc_now(); meta = json.dumps(data.get('metadata_json') or data.get('metadata') or {}, ensure_ascii=False, default=str)
    channel = str(data.get('channel') or 'email'); ext = str(data.get('external_thread_id') or tid)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT id FROM crm_threads WHERE channel=$1 AND external_thread_id=$2", channel, ext)
            if existing: tid = str(existing['id'])
            await conn.execute(
                """INSERT INTO crm_threads(id,customer_id,channel,external_thread_id,subject,status,last_message_at,order_id,metadata_json,created_at,updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,NOW(),NOW())
                   ON CONFLICT(id) DO UPDATE SET customer_id=EXCLUDED.customer_id,subject=EXCLUDED.subject,status=EXCLUDED.status,last_message_at=EXCLUDED.last_message_at,order_id=EXCLUDED.order_id,metadata_json=EXCLUDED.metadata_json,updated_at=NOW()""",
                tid, str(data.get('customer_id') or ''), channel, ext, str(data.get('subject') or ''), str(data.get('status') or 'open'), _pg_ts(data.get('last_message_at')) if data.get('last_message_at') else None, str(data.get('order_id') or ''), meta)
            row = await conn.fetchrow("SELECT * FROM crm_threads WHERE id=$1", tid)
        return dict(row) if row else {"id": tid}
    async with get_connection() as db:
        cur=await db.execute("SELECT id FROM crm_threads WHERE channel=? AND external_thread_id=?", (channel,ext)); ex=await cur.fetchone(); await cur.close()
        if ex: tid = ex['id'] if isinstance(ex,dict) or hasattr(ex,'keys') else ex[0]
        await db.execute(
            """INSERT INTO crm_threads(id,customer_id,channel,external_thread_id,subject,status,last_message_at,order_id,metadata_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET customer_id=excluded.customer_id,subject=excluded.subject,status=excluded.status,last_message_at=excluded.last_message_at,order_id=excluded.order_id,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
            (tid,str(data.get('customer_id') or ''),channel,ext,str(data.get('subject') or ''),str(data.get('status') or 'open'),data.get('last_message_at'),str(data.get('order_id') or ''),meta,now,now))
        await db.commit(); cur=await db.execute("SELECT * FROM crm_threads WHERE id=?", (tid,)); row=await cur.fetchone(); await cur.close(); return dict(row) if row else {"id": tid}


async def crm_get_thread(*, thread_id: str | None = None, channel: str | None = None, external_thread_id: str | None = None) -> dict[str, Any] | None:
    if thread_id:
        if uses_postgres():
            pool = await get_pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM crm_threads WHERE id=$1", thread_id)
            return dict(row) if row else None
        async with get_connection() as db:
            cur=await db.execute("SELECT * FROM crm_threads WHERE id=?", (thread_id,)); row=await cur.fetchone(); await cur.close(); return dict(row) if row else None
    if channel and external_thread_id:
        if uses_postgres():
            pool = await get_pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM crm_threads WHERE channel=$1 AND external_thread_id=$2", channel, external_thread_id)
            return dict(row) if row else None
        async with get_connection() as db:
            cur=await db.execute("SELECT * FROM crm_threads WHERE channel=? AND external_thread_id=?", (channel, external_thread_id)); row=await cur.fetchone(); await cur.close(); return dict(row) if row else None
    return None


async def crm_list_threads(limit: int = 100, channel: str | None = None, status: str | None = None, customer_id: str | None = None) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool(); clauses=[]; args=[]; i=1
        if channel: clauses.append(f"channel=${i}"); args.append(channel); i+=1
        if status: clauses.append(f"status=${i}"); args.append(status); i+=1
        if customer_id: clauses.append(f"customer_id=${i}"); args.append(customer_id); i+=1
        where = (" WHERE "+" AND ".join(clauses)) if clauses else ""
        q = f"SELECT * FROM crm_threads{where} ORDER BY COALESCE(last_message_at, updated_at) DESC LIMIT ${i}"; args.append(limit)
        async with pool.acquire() as conn: rows = await conn.fetch(q, *args)
        return [dict(r) for r in rows]
    async with get_connection() as db:
        clauses=[]; args=[]
        if channel: clauses.append("channel=?"); args.append(channel)
        if status: clauses.append("status=?"); args.append(status)
        if customer_id: clauses.append("customer_id=?"); args.append(customer_id)
        where = (" WHERE "+" AND ".join(clauses)) if clauses else ""
        q = f"SELECT * FROM crm_threads{where} ORDER BY COALESCE(last_message_at, updated_at) DESC LIMIT ?"; args.append(limit)
        cur=await db.execute(q, tuple(args)); rows=await cur.fetchall(); await cur.close(); return [dict(r) for r in rows]


async def crm_add_message(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Insert message idempotently. Returns (message, created)."""
    import uuid as _uuid
    mid = str(data.get('id') or _uuid.uuid4()); now = utc_now()
    att = data.get('attachments_json'); meta = data.get('metadata_json')
    if isinstance(att,(list,dict)): att=json.dumps(att,ensure_ascii=False,default=str)
    if isinstance(meta,(list,dict)): meta=json.dumps(meta,ensure_ascii=False,default=str)
    idem = str(data.get('idempotency_key') or _uuid.uuid4()); channel=str(data.get('channel') or 'email')
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM crm_messages WHERE channel=$1 AND idempotency_key=$2", channel, idem)
            if row: return dict(row), False
            await conn.execute(
                """INSERT INTO crm_messages(id,thread_id,customer_id,channel,direction,external_message_id,idempotency_key,subject,body,sender,recipient,attachments_json,sent_at,status,metadata_json,created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,$14,$15::jsonb,NOW())""",
                mid, str(data.get('thread_id') or ''), str(data.get('customer_id') or ''), channel, str(data.get('direction') or 'inbound'), str(data.get('external_message_id') or ''), idem, str(data.get('subject') or ''), str(data.get('body') or ''), str(data.get('sender') or ''), str(data.get('recipient') or ''), att or '[]', _pg_ts(data.get('sent_at') or now), str(data.get('status') or 'received'), meta or '{}')
            await conn.execute("UPDATE crm_threads SET last_message_at=$1, updated_at=NOW() WHERE id=$2", _pg_ts(data.get('sent_at') or now), str(data.get('thread_id') or ''))
            row = await conn.fetchrow("SELECT * FROM crm_messages WHERE id=$1", mid)
        return (dict(row) if row else {"id": mid}), True
    async with get_connection() as db:
        cur=await db.execute("SELECT * FROM crm_messages WHERE channel=? AND idempotency_key=?", (channel,idem)); row=await cur.fetchone(); await cur.close()
        if row: return dict(row), False
        await db.execute(
            """INSERT INTO crm_messages(id,thread_id,customer_id,channel,direction,external_message_id,idempotency_key,subject,body,sender,recipient,attachments_json,sent_at,status,metadata_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid,str(data.get('thread_id') or ''),str(data.get('customer_id') or ''),channel,str(data.get('direction') or 'inbound'),str(data.get('external_message_id') or ''),idem,str(data.get('subject') or ''),str(data.get('body') or ''),str(data.get('sender') or ''),str(data.get('recipient') or ''),att or '[]',data.get('sent_at') or now,str(data.get('status') or 'received'),meta or '{}',now))
        await db.execute("UPDATE crm_threads SET last_message_at=?, updated_at=? WHERE id=?", (data.get('sent_at') or now, now, str(data.get('thread_id') or '')))
        await db.commit(); cur=await db.execute("SELECT * FROM crm_messages WHERE id=?", (mid,)); row=await cur.fetchone(); await cur.close()
        return (dict(row) if row else {"id": mid}), True


async def crm_list_messages(*, thread_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if thread_id: rows = await conn.fetch("SELECT * FROM crm_messages WHERE thread_id=$1 ORDER BY sent_at ASC LIMIT $2", thread_id, limit)
            else: rows = await conn.fetch("SELECT * FROM crm_messages ORDER BY sent_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]
    async with get_connection() as db:
        if thread_id: cur=await db.execute("SELECT * FROM crm_messages WHERE thread_id=? ORDER BY sent_at ASC LIMIT ?", (thread_id,limit))
        else: cur=await db.execute("SELECT * FROM crm_messages ORDER BY sent_at DESC LIMIT ?", (limit,))
        rows=await cur.fetchall(); await cur.close(); return [dict(r) for r in rows]


async def crm_create_appointment(data: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid
    aid = str(data.get('id') or _uuid.uuid4()); now = utc_now(); meta=json.dumps(data.get('metadata_json') or data.get('metadata') or {}, ensure_ascii=False, default=str)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO crm_appointments(id,customer_id,thread_id,order_id,title,description,starts_at,ends_at,status,channel,location,metadata_json,created_by,created_at,updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,NOW(),NOW())""",
                aid, data.get('customer_id'), data.get('thread_id'), str(data.get('order_id') or ''), str(data.get('title') or 'Termin'), str(data.get('description') or ''), _pg_ts(data.get('starts_at') or now), _pg_ts(data.get('ends_at')) if data.get('ends_at') else None, str(data.get('status') or 'scheduled'), str(data.get('channel') or 'email'), str(data.get('location') or ''), meta, str(data.get('created_by') or ''))
            row = await conn.fetchrow("SELECT * FROM crm_appointments WHERE id=$1", aid)
        return dict(row) if row else {"id": aid}
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO crm_appointments(id,customer_id,thread_id,order_id,title,description,starts_at,ends_at,status,channel,location,metadata_json,created_by,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid,data.get('customer_id'),data.get('thread_id'),str(data.get('order_id') or ''),str(data.get('title') or 'Termin'),str(data.get('description') or ''),data.get('starts_at') or now,data.get('ends_at'),str(data.get('status') or 'scheduled'),str(data.get('channel') or 'email'),str(data.get('location') or ''),meta,str(data.get('created_by') or ''),now,now))
        await db.commit(); cur=await db.execute("SELECT * FROM crm_appointments WHERE id=?", (aid,)); row=await cur.fetchone(); await cur.close(); return dict(row) if row else {"id": aid}


async def crm_list_appointments(limit: int = 100, customer_id: str | None = None) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if customer_id: rows = await conn.fetch("SELECT * FROM crm_appointments WHERE customer_id=$1 ORDER BY starts_at ASC LIMIT $2", customer_id, limit)
            else: rows = await conn.fetch("SELECT * FROM crm_appointments ORDER BY starts_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]
    async with get_connection() as db:
        if customer_id: cur=await db.execute("SELECT * FROM crm_appointments WHERE customer_id=? ORDER BY starts_at ASC LIMIT ?", (customer_id,limit))
        else: cur=await db.execute("SELECT * FROM crm_appointments ORDER BY starts_at DESC LIMIT ?", (limit,))
        rows=await cur.fetchall(); await cur.close(); return [dict(r) for r in rows]


# ── AI agent action / audit helpers ──────────────────────────────────────

async def agent_action_create(data: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid
    aid = str(data.get('id') or _uuid.uuid4()); now = utc_now()
    args = json.dumps(data.get('args_json') or data.get('args') or {}, ensure_ascii=False, default=str)
    prev = json.dumps(data.get('preview_json') or data.get('preview') or {}, ensure_ascii=False, default=str)
    resj = json.dumps(data.get('result_json') or data.get('result') or {}, ensure_ascii=False, default=str)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO ai_agent_actions(id,actor,prompt,tool_name,args_json,preview_json,status,requires_confirmation,result_json,error_message,created_at) VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8,$9::jsonb,$10,NOW())", aid, str(data.get('actor') or ''), str(data.get('prompt') or ''), str(data.get('tool_name') or ''), args, prev, str(data.get('status') or 'pending'), bool(data.get('requires_confirmation', True)), resj, str(data.get('error_message') or ''))
            row = await conn.fetchrow("SELECT * FROM ai_agent_actions WHERE id=$1", aid)
        return dict(row) if row else {"id": aid}
    async with get_connection() as db:
        await db.execute("INSERT INTO ai_agent_actions(id,actor,prompt,tool_name,args_json,preview_json,status,requires_confirmation,result_json,error_message,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (aid,str(data.get('actor') or ''),str(data.get('prompt') or ''),str(data.get('tool_name') or ''),args,prev,str(data.get('status') or 'pending'),1 if data.get('requires_confirmation',True) else 0,resj,str(data.get('error_message') or ''),now))
        await db.commit(); cur=await db.execute("SELECT * FROM ai_agent_actions WHERE id=?", (aid,)); row=await cur.fetchone(); await cur.close(); return dict(row) if row else {"id": aid}


async def agent_action_get(action_id: str) -> dict[str, Any] | None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn: row = await conn.fetchrow("SELECT * FROM ai_agent_actions WHERE id=$1", action_id)
        return dict(row) if row else None
    async with get_connection() as db:
        cur=await db.execute("SELECT * FROM ai_agent_actions WHERE id=?", (action_id,)); row=await cur.fetchone(); await cur.close(); return dict(row) if row else None


async def agent_action_update(action_id: str, **fields: Any) -> dict[str, Any] | None:
    if not fields: return await agent_action_get(action_id)
    now = utc_now()
    if uses_postgres():
        sets=[]; vals=[]; i=1
        for k,v in fields.items():
            if k in {'args_json','preview_json','result_json'} and not isinstance(v,str): v=json.dumps(v,ensure_ascii=False,default=str)
            if k=='executed_at': v=_pg_ts(v)
            sets.append(f"{k}=${i}"); vals.append(v); i+=1
        vals.append(action_id)
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(f"UPDATE ai_agent_actions SET {', '.join(sets)} WHERE id=${i}", *vals)
            row = await conn.fetchrow("SELECT * FROM ai_agent_actions WHERE id=$1", action_id)
        return dict(row) if row else None
    async with get_connection() as db:
        sets=[]; vals=[]
        for k,v in fields.items():
            if k in {'args_json','preview_json','result_json'} and not isinstance(v,str): v=json.dumps(v,ensure_ascii=False,default=str)
            if k=='requires_confirmation': v=1 if v else 0
            sets.append(f"{k}=?"); vals.append(v)
        vals.append(action_id)
        await db.execute(f"UPDATE ai_agent_actions SET {', '.join(sets)} WHERE id=?", tuple(vals)); await db.commit()
        cur=await db.execute("SELECT * FROM ai_agent_actions WHERE id=?", (action_id,)); row=await cur.fetchone(); await cur.close(); return dict(row) if row else None


# ── Multimodal Creative Engine helpers ───────────────────────────────────

async def creative_project_upsert(data: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid
    pid = str(data.get("id") or _uuid.uuid4())
    now = utc_now()
    ctx = json.dumps(data.get("context_json") or data.get("context") or {}, ensure_ascii=False, default=str)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO creative_projects(id,name,project_type,status,brief,context_json,created_by,created_at,updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,NOW(),NOW())
                   ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,project_type=EXCLUDED.project_type,status=EXCLUDED.status,
                   brief=EXCLUDED.brief,context_json=EXCLUDED.context_json,updated_at=NOW()""",
                pid, str(data.get("name") or "Projekt"), str(data.get("project_type") or "general"), str(data.get("status") or "draft"),
                str(data.get("brief") or ""), ctx, str(data.get("created_by") or ""))
            row = await conn.fetchrow("SELECT * FROM creative_projects WHERE id=$1", pid)
        return dict(row) if row else {"id": pid}
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO creative_projects(id,name,project_type,status,brief,context_json,created_by,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,project_type=excluded.project_type,status=excluded.status,
               brief=excluded.brief,context_json=excluded.context_json,updated_at=excluded.updated_at""",
            (pid, str(data.get("name") or "Projekt"), str(data.get("project_type") or "general"), str(data.get("status") or "draft"),
             str(data.get("brief") or ""), ctx, str(data.get("created_by") or ""), now, now))
        await db.commit(); cur = await db.execute("SELECT * FROM creative_projects WHERE id=?", (pid,)); row = await cur.fetchone(); await cur.close()
        return dict(row) if row else {"id": pid}


async def creative_project_list(limit: int = 100) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM creative_projects ORDER BY updated_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM creative_projects ORDER BY updated_at DESC LIMIT ?", (limit,))
        rows = await cur.fetchall(); await cur.close(); return [dict(r) for r in rows]


async def creative_job_create(data: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid
    jid = str(data.get("id") or _uuid.uuid4()); now = utc_now()
    inp = json.dumps(data.get("input_json") or data.get("input") or {}, ensure_ascii=False, default=str)
    out = json.dumps(data.get("output_json") or data.get("output") or {}, ensure_ascii=False, default=str)
    cost = json.dumps(data.get("cost_json") or data.get("cost") or {}, ensure_ascii=False, default=str)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO creative_jobs(id,project_id,job_type,capability,status,provider_id,model_id,prompt,negative_prompt,input_json,output_json,cost_json,error_message,requires_review,created_by,created_at,updated_at,started_at,completed_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb,$12::jsonb,$13,$14,$15,NOW(),NOW(),$16,$17)""",
                jid, data.get("project_id"), str(data.get("job_type") or "image_generation"), str(data.get("capability") or "image_generation"),
                str(data.get("status") or "queued"), str(data.get("provider_id") or ""), str(data.get("model_id") or ""),
                str(data.get("prompt") or ""), str(data.get("negative_prompt") or ""), inp, out, cost,
                str(data.get("error_message") or ""), bool(data.get("requires_review", False)), str(data.get("created_by") or ""),
                _pg_ts(data.get("started_at")), _pg_ts(data.get("completed_at")))
            row = await conn.fetchrow("SELECT * FROM creative_jobs WHERE id=$1", jid)
        return dict(row) if row else {"id": jid}
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO creative_jobs(id,project_id,job_type,capability,status,provider_id,model_id,prompt,negative_prompt,input_json,output_json,cost_json,error_message,requires_review,created_by,created_at,updated_at,started_at,completed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (jid, data.get("project_id"), str(data.get("job_type") or "image_generation"), str(data.get("capability") or "image_generation"),
             str(data.get("status") or "queued"), str(data.get("provider_id") or ""), str(data.get("model_id") or ""),
             str(data.get("prompt") or ""), str(data.get("negative_prompt") or ""), inp, out, cost,
             str(data.get("error_message") or ""), 1 if data.get("requires_review") else 0, str(data.get("created_by") or ""),
             now, now, data.get("started_at"), data.get("completed_at")))
        await db.commit(); cur = await db.execute("SELECT * FROM creative_jobs WHERE id=?", (jid,)); row = await cur.fetchone(); await cur.close()
        return dict(row) if row else {"id": jid}


async def creative_job_update(job_id: str, **fields: Any) -> dict[str, Any] | None:
    if not fields:
        return await creative_job_get(job_id)
    now = utc_now()
    if uses_postgres():
        sets=[]; vals=[]; i=1
        for k,v in fields.items():
            if k in {"input_json","output_json","cost_json"} and not isinstance(v,str): v=json.dumps(v,ensure_ascii=False,default=str)
            if k in {"started_at","completed_at"}: v=_pg_ts(v)
            sets.append(f"{k}=${i}"); vals.append(v); i+=1
        sets.append(f"updated_at=${i}"); vals.append(now); i+=1
        vals.append(job_id)
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(f"UPDATE creative_jobs SET {', '.join(sets)} WHERE id=${i}", *vals)
            row = await conn.fetchrow("SELECT * FROM creative_jobs WHERE id=$1", job_id)
        return dict(row) if row else None
    async with get_connection() as db:
        sets=[]; vals=[]
        for k,v in fields.items():
            if k in {"input_json","output_json","cost_json"} and not isinstance(v,str): v=json.dumps(v,ensure_ascii=False,default=str)
            if k=="requires_review": v=1 if v else 0
            sets.append(f"{k}=?"); vals.append(v)
        sets.append("updated_at=?"); vals.append(now); vals.append(job_id)
        await db.execute(f"UPDATE creative_jobs SET {', '.join(sets)} WHERE id=?", tuple(vals))
        await db.commit(); cur = await db.execute("SELECT * FROM creative_jobs WHERE id=?", (job_id,)); row = await cur.fetchone(); await cur.close()
        return dict(row) if row else None


async def creative_job_get(job_id: str) -> dict[str, Any] | None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM creative_jobs WHERE id=$1", job_id)
        return dict(row) if row else None
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM creative_jobs WHERE id=?", (job_id,)); row = await cur.fetchone(); await cur.close(); return dict(row) if row else None


async def creative_job_list(limit: int = 100, project_id: str | None = None) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if project_id:
                rows = await conn.fetch("SELECT * FROM creative_jobs WHERE project_id=$1 ORDER BY created_at DESC LIMIT $2", project_id, limit)
            else:
                rows = await conn.fetch("SELECT * FROM creative_jobs ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]
    async with get_connection() as db:
        if project_id:
            cur = await db.execute("SELECT * FROM creative_jobs WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (project_id, limit))
        else:
            cur = await db.execute("SELECT * FROM creative_jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = await cur.fetchall(); await cur.close(); return [dict(r) for r in rows]


async def media_asset_create(data: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid
    aid = str(data.get("id") or _uuid.uuid4()); now = utc_now()
    meta = json.dumps(data.get("metadata_json") or data.get("metadata") or {}, ensure_ascii=False, default=str)
    immutable = bool(data.get("immutable_original", False))
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO media_assets(id,asset_type,source_kind,immutable_original,storage_path,url,sha256,mime_type,width,height,duration_seconds,size_bytes,product_sku,model_profile_id,job_id,parent_asset_id,metadata_json,created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17::jsonb,NOW())""",
                aid, str(data.get("asset_type") or "image"), str(data.get("source_kind") or "generated"), immutable,
                str(data.get("storage_path") or ""), str(data.get("url") or ""), str(data.get("sha256") or ""),
                str(data.get("mime_type") or ""), data.get("width"), data.get("height"), data.get("duration_seconds"), data.get("size_bytes"),
                str(data.get("product_sku") or ""), str(data.get("model_profile_id") or ""), data.get("job_id"),
                str(data.get("parent_asset_id") or ""), meta)
            row = await conn.fetchrow("SELECT * FROM media_assets WHERE id=$1", aid)
        return dict(row) if row else {"id": aid}
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO media_assets(id,asset_type,source_kind,immutable_original,storage_path,url,sha256,mime_type,width,height,duration_seconds,size_bytes,product_sku,model_profile_id,job_id,parent_asset_id,metadata_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, str(data.get("asset_type") or "image"), str(data.get("source_kind") or "generated"), 1 if immutable else 0,
             str(data.get("storage_path") or ""), str(data.get("url") or ""), str(data.get("sha256") or ""),
             str(data.get("mime_type") or ""), data.get("width"), data.get("height"), data.get("duration_seconds"), data.get("size_bytes"),
             str(data.get("product_sku") or ""), str(data.get("model_profile_id") or ""), data.get("job_id"),
             str(data.get("parent_asset_id") or ""), meta, now))
        await db.commit(); cur = await db.execute("SELECT * FROM media_assets WHERE id=?", (aid,)); row = await cur.fetchone(); await cur.close()
        return dict(row) if row else {"id": aid}


async def media_asset_list(limit: int = 200, product_sku: str | None = None, job_id: str | None = None) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if product_sku:
                rows = await conn.fetch("SELECT * FROM media_assets WHERE product_sku=$1 ORDER BY created_at DESC LIMIT $2", product_sku, limit)
            elif job_id:
                rows = await conn.fetch("SELECT * FROM media_assets WHERE job_id=$1 ORDER BY created_at DESC LIMIT $2", job_id, limit)
            else:
                rows = await conn.fetch("SELECT * FROM media_assets ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]
    async with get_connection() as db:
        if product_sku:
            cur = await db.execute("SELECT * FROM media_assets WHERE product_sku=? ORDER BY created_at DESC LIMIT ?", (product_sku, limit))
        elif job_id:
            cur = await db.execute("SELECT * FROM media_assets WHERE job_id=? ORDER BY created_at DESC LIMIT ?", (job_id, limit))
        else:
            cur = await db.execute("SELECT * FROM media_assets ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = await cur.fetchall(); await cur.close(); return [dict(r) for r in rows]


async def model_profile_upsert(data: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid
    pid = str(data.get("id") or _uuid.uuid4()); now = utc_now()
    slug = str(data.get("slug") or "").strip().lower() or pid
    def jd(v, d):
        return json.dumps(v if v is not None else d, ensure_ascii=False, default=str)
    fields = (
        pid, slug, str(data.get("display_name") or slug.title()), str(data.get("description") or ""),
        jd(data.get("reference_images_json") or data.get("reference_images"), []),
        jd(data.get("identity_profile_json") or data.get("identity_profile"), {}),
        jd(data.get("body_profile_json") or data.get("body_profile"), {}),
        jd(data.get("style_profile_json") or data.get("style_profile"), {}),
        jd(data.get("preferred_locations_json") or data.get("preferred_locations"), []),
        jd(data.get("preferred_perspectives_json") or data.get("preferred_perspectives"), []),
        jd(data.get("posing_profiles_json") or data.get("posing_profiles"), []),
        jd(data.get("prompt_templates_json") or data.get("prompt_templates"), []),
        jd(data.get("rights_consent_json") or data.get("rights_consent"), {}),
        bool(data.get("usage_allowed", True)), bool(data.get("active", True)),
        jd(data.get("history_json") or data.get("history"), []),
    )
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO model_profiles(id,slug,display_name,description,reference_images_json,identity_profile_json,body_profile_json,style_profile_json,preferred_locations_json,preferred_perspectives_json,posing_profiles_json,prompt_templates_json,rights_consent_json,usage_allowed,active,history_json,created_at,updated_at)
                   VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb,$8::jsonb,$9::jsonb,$10::jsonb,$11::jsonb,$12::jsonb,$13::jsonb,$14,$15,$16::jsonb,NOW(),NOW())
                   ON CONFLICT(slug) DO UPDATE SET display_name=EXCLUDED.display_name,description=EXCLUDED.description,reference_images_json=EXCLUDED.reference_images_json,identity_profile_json=EXCLUDED.identity_profile_json,body_profile_json=EXCLUDED.body_profile_json,style_profile_json=EXCLUDED.style_profile_json,preferred_locations_json=EXCLUDED.preferred_locations_json,preferred_perspectives_json=EXCLUDED.preferred_perspectives_json,posing_profiles_json=EXCLUDED.posing_profiles_json,prompt_templates_json=EXCLUDED.prompt_templates_json,rights_consent_json=EXCLUDED.rights_consent_json,usage_allowed=EXCLUDED.usage_allowed,active=EXCLUDED.active,history_json=EXCLUDED.history_json,updated_at=NOW()""",
                *fields)
            row = await conn.fetchrow("SELECT * FROM model_profiles WHERE slug=$1", slug)
        return dict(row) if row else {"id": pid, "slug": slug}
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO model_profiles(id,slug,display_name,description,reference_images_json,identity_profile_json,body_profile_json,style_profile_json,preferred_locations_json,preferred_perspectives_json,posing_profiles_json,prompt_templates_json,rights_consent_json,usage_allowed,active,history_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(slug) DO UPDATE SET display_name=excluded.display_name,description=excluded.description,reference_images_json=excluded.reference_images_json,identity_profile_json=excluded.identity_profile_json,body_profile_json=excluded.body_profile_json,style_profile_json=excluded.style_profile_json,preferred_locations_json=excluded.preferred_locations_json,preferred_perspectives_json=excluded.preferred_perspectives_json,posing_profiles_json=excluded.posing_profiles_json,prompt_templates_json=excluded.prompt_templates_json,rights_consent_json=excluded.rights_consent_json,usage_allowed=excluded.usage_allowed,active=excluded.active,history_json=excluded.history_json,updated_at=excluded.updated_at""",
            (pid, slug, fields[2], fields[3], fields[4], fields[5], fields[6], fields[7], fields[8], fields[9], fields[10], fields[11], fields[12], 1 if fields[13] else 0, 1 if fields[14] else 0, fields[15], now, now))
        await db.commit(); cur = await db.execute("SELECT * FROM model_profiles WHERE slug=?", (slug,)); row = await cur.fetchone(); await cur.close()
        return dict(row) if row else {"id": pid, "slug": slug}


async def model_profile_list(limit: int = 200) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM model_profiles ORDER BY display_name ASC LIMIT $1", limit)
        return [dict(r) for r in rows]
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM model_profiles ORDER BY display_name ASC LIMIT ?", (limit,))
        rows = await cur.fetchall(); await cur.close(); return [dict(r) for r in rows]


async def model_profile_get(slug: str) -> dict[str, Any] | None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM model_profiles WHERE slug=$1", slug)
        return dict(row) if row else None
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM model_profiles WHERE slug=?", (slug,)); row = await cur.fetchone(); await cur.close(); return dict(row) if row else None


async def fidelity_check_create(data: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid
    fid = str(data.get("id") or _uuid.uuid4()); now = utc_now()
    def jd(v,d): return json.dumps(v if v is not None else d, ensure_ascii=False, default=str)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO product_fidelity_checks(id,job_id,output_asset_id,product_sku,original_asset_ids_json,scores_json,overall_score,verdict,critical_issues_json,details_json,created_at)
                   VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8,$9::jsonb,$10::jsonb,NOW())""",
                fid, data.get("job_id"), str(data.get("output_asset_id") or ""), str(data.get("product_sku") or ""),
                jd(data.get("original_asset_ids_json") or data.get("original_asset_ids"), []),
                jd(data.get("scores_json") or data.get("scores"), {}), float(data.get("overall_score") or 0),
                str(data.get("verdict") or "pending"),
                jd(data.get("critical_issues_json") or data.get("critical_issues"), []),
                jd(data.get("details_json") or data.get("details"), {}))
            row = await conn.fetchrow("SELECT * FROM product_fidelity_checks WHERE id=$1", fid)
        return dict(row) if row else {"id": fid}
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO product_fidelity_checks(id,job_id,output_asset_id,product_sku,original_asset_ids_json,scores_json,overall_score,verdict,critical_issues_json,details_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (fid, data.get("job_id"), str(data.get("output_asset_id") or ""), str(data.get("product_sku") or ""),
             jd(data.get("original_asset_ids_json") or data.get("original_asset_ids"), []),
             jd(data.get("scores_json") or data.get("scores"), {}), float(data.get("overall_score") or 0),
             str(data.get("verdict") or "pending"),
             jd(data.get("critical_issues_json") or data.get("critical_issues"), []),
             jd(data.get("details_json") or data.get("details"), {}), now))
        await db.commit(); cur = await db.execute("SELECT * FROM product_fidelity_checks WHERE id=?", (fid,)); row = await cur.fetchone(); await cur.close()
        return dict(row) if row else {"id": fid}


async def fidelity_check_list(limit: int = 200, job_id: str | None = None) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if job_id: rows = await conn.fetch("SELECT * FROM product_fidelity_checks WHERE job_id=$1 ORDER BY created_at DESC LIMIT $2", job_id, limit)
            else: rows = await conn.fetch("SELECT * FROM product_fidelity_checks ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]
    async with get_connection() as db:
        if job_id: cur = await db.execute("SELECT * FROM product_fidelity_checks WHERE job_id=? ORDER BY created_at DESC LIMIT ?", (job_id, limit))
        else: cur = await db.execute("SELECT * FROM product_fidelity_checks ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = await cur.fetchall(); await cur.close(); return [dict(r) for r in rows]


_FINANCE_RECORD_FIELDS: dict[str, tuple[str, ...]] = {
    "finance_documents": (
        "document_type", "direction", "order_reference", "currency",
        "net_amount", "tax_amount", "gross_amount", "status", "issued_at", "due_at",
    ),
    "finance_transactions": (
        "transaction_type", "order_reference", "settlement_reference",
        "currency", "amount", "occurred_at",
    ),
    "finance_settlements": (
        "currency", "gross_amount", "fees_amount", "net_amount", "status", "settled_at",
    ),
}


def _finance_amount(value: Any, *, field: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a decimal amount.") from exc
    if not amount.is_finite():
        raise ValueError(f"{field} must be finite.")
    normalized = amount.normalize()
    _, digits, exponent = normalized.as_tuple()
    scale = max(-exponent, 0)
    integer_digits = max(len(digits) + exponent, 0)
    if scale > 6 or integer_digits > 14:
        raise ValueError(f"{field} exceeds the supported precision (14 integer and 6 decimal places).")
    return amount


def _finance_amount_sqlite(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def _decode_finance_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for field in ("payload_json", "details_json"):
        if isinstance(result.get(field), str):
            try:
                result[field] = json.loads(result[field])
            except json.JSONDecodeError:
                logger.exception("Invalid JSON stored for finance record %s", result.get("id"))
                raise DatabaseError("Stored finance payload is not valid JSON.")
    return result


async def _finance_record_upsert(table: str, data: Mapping[str, Any]) -> dict[str, Any]:
    if table not in _FINANCE_RECORD_FIELDS:
        raise ValueError("Unsupported finance record type.")
    import uuid as _uuid

    record_id = str(data.get("id") or _uuid.uuid4())
    provider = str(data.get("provider") or "").strip()
    external_id = str(data.get("external_id") or "").strip()
    if not provider or not external_id:
        raise ValueError("Finance provider and external ID are required.")

    fields = _FINANCE_RECORD_FIELDS[table]
    now = utc_now()
    payload_json = json.dumps(data.get("payload_json") or {}, ensure_ascii=False, default=str)
    values: list[Any] = [record_id, provider, external_id]
    for field in fields:
        value = data.get(field)
        if field in {"net_amount", "tax_amount", "gross_amount", "amount", "fees_amount"}:
            amount = _finance_amount(value, field=field)
            value = amount if uses_postgres() else _finance_amount_sqlite(amount)
        if field in {"order_reference", "settlement_reference"}:
            value = str(value or "")
        elif field == "status":
            value = str(value or ("received" if table == "finance_settlements" else "issued"))
        if field in {"issued_at", "due_at", "occurred_at", "settled_at"}:
            value = _pg_ts(value) if value is not None else None
            if value is not None and not uses_postgres() and hasattr(value, "isoformat"):
                value = value.isoformat()
        if not uses_postgres() and isinstance(value, Decimal):
            value = str(value)
        values.append(value)
    created_at = _pg_ts(now) if uses_postgres() else now
    updated_at = created_at
    values.extend((payload_json, created_at, updated_at))
    columns = ("id", "provider", "external_id", *fields, "payload_json", "created_at", "updated_at")
    update_fields = (*fields, "payload_json", "updated_at")

    if uses_postgres():
        pool = await get_pg_pool()
        placeholders = [
            f"${index}::jsonb" if column == "payload_json" else f"${index}"
            for index, column in enumerate(columns, start=1)
        ]
        updates = ", ".join(f"{field}=EXCLUDED.{field}" for field in update_fields)
        query = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(placeholders)}) "
            f"ON CONFLICT(provider, external_id) DO UPDATE SET {updates} RETURNING *"
        )
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, *values)
        return _decode_finance_row(row) or {"id": record_id}

    assignments = ", ".join(f"{field}=excluded.{field}" for field in update_fields)
    query = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
        f"ON CONFLICT(provider, external_id) DO UPDATE SET {assignments}"
    )
    async with get_connection() as db:
        await db.execute(query, tuple(values))
        await db.commit()
        cursor = await db.execute(
            f"SELECT * FROM {table} WHERE provider=? AND external_id=?",
            (provider, external_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
    return _decode_finance_row(row) or {"id": record_id}


async def finance_document_upsert(data: Mapping[str, Any]) -> dict[str, Any]:
    """Create or update an imported invoice, credit note, or expense by provider ID."""
    return await _finance_record_upsert("finance_documents", data)


async def finance_transaction_upsert(data: Mapping[str, Any]) -> dict[str, Any]:
    """Create or update an imported payment or payment-provider transaction."""
    return await _finance_record_upsert("finance_transactions", data)


async def finance_settlement_upsert(data: Mapping[str, Any]) -> dict[str, Any]:
    """Create or update a marketplace/payment settlement by provider ID."""
    return await _finance_record_upsert("finance_settlements", data)


async def _finance_list(table: str, *, limit: int, offset: int = 0) -> list[dict[str, Any]]:
    if table not in {*_FINANCE_RECORD_FIELDS, "finance_reconciliations"}:
        raise ValueError("Unsupported finance record type.")
    safe_limit = min(max(limit, 1), 500)
    safe_offset = max(offset, 0)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {table} ORDER BY created_at DESC LIMIT $1 OFFSET $2",
                safe_limit,
                safe_offset,
            )
        return [_decode_finance_row(row) or {} for row in rows]
    async with get_connection() as db:
        cursor = await db.execute(
            f"SELECT * FROM {table} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (safe_limit, safe_offset),
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [_decode_finance_row(row) or {} for row in rows]


async def finance_document_list(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    return await _finance_list("finance_documents", limit=limit, offset=offset)


async def finance_transaction_list(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    return await _finance_list("finance_transactions", limit=limit, offset=offset)


async def finance_settlement_list(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    return await _finance_list("finance_settlements", limit=limit, offset=offset)


async def finance_reconciliation_list(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    return await _finance_list("finance_reconciliations", limit=limit, offset=offset)


async def finance_transaction_get(transaction_id: str) -> dict[str, Any] | None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM finance_transactions WHERE id=$1", transaction_id)
        return _decode_finance_row(row)
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM finance_transactions WHERE id=?", (transaction_id,))
        row = await cursor.fetchone()
        await cursor.close()
    return _decode_finance_row(row)


async def finance_document_candidates(
    *,
    currency: str,
    order_reference: str,
    gross_amount: Any,
) -> list[dict[str, Any]]:
    """Fetch exact/reference candidates for safe, bounded payment matching."""
    amount = _finance_amount(gross_amount, field="gross_amount")
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM finance_documents
                   WHERE direction='receivable' AND currency=$1
                     AND (($2 <> '' AND order_reference=$2) OR gross_amount=$3)
                   ORDER BY created_at DESC LIMIT 501""",
                currency, order_reference, amount,
            )
        return [_decode_finance_row(row) or {} for row in rows]
    async with get_connection() as db:
        cursor = await db.execute(
            """SELECT * FROM finance_documents
               WHERE direction='receivable' AND currency=?
                 AND ((? <> '' AND order_reference=?) OR gross_amount=?)
               ORDER BY created_at DESC LIMIT 501""",
            (currency, order_reference, order_reference, _finance_amount_sqlite(amount)),
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [_decode_finance_row(row) or {} for row in rows]


async def finance_reconciliation_create(data: Mapping[str, Any]) -> dict[str, Any]:
    import uuid as _uuid

    record_id = str(data.get("id") or _uuid.uuid4())
    details_json = json.dumps(data.get("details_json") or {}, ensure_ascii=False, default=str)
    values = (
        record_id,
        str(data["source_type"]),
        str(data["source_id"]),
        str(data.get("target_type") or ""),
        str(data.get("target_id") or ""),
        str(data["status"]),
        str(data["currency"]),
        _finance_amount(data.get("matched_amount", "0"), field="matched_amount"),
        details_json,
        utc_now(),
    )
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO finance_reconciliations
                   (id,source_type,source_id,target_type,target_id,status,currency,matched_amount,details_json,created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,NOW()) RETURNING *""",
                *values[:-1],
            )
        return _decode_finance_row(row) or {"id": record_id}
    values = (*values[:7], _finance_amount_sqlite(values[7]), *values[8:])
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO finance_reconciliations
               (id,source_type,source_id,target_type,target_id,status,currency,matched_amount,details_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        await db.commit()
        cursor = await db.execute("SELECT * FROM finance_reconciliations WHERE id=?", (record_id,))
        row = await cursor.fetchone()
        await cursor.close()
    result = dict(row) if row else {"id": record_id}
    if isinstance(result.get("details_json"), str):
        result["details_json"] = json.loads(result["details_json"])
    return result


async def finance_overview() -> dict[str, Any]:
    """Return source totals grouped by currency, never conflating unlike records."""
    if uses_postgres():
        queries = {
            "documents": "SELECT currency, COUNT(*) AS count, SUM(gross_amount) AS gross_amount FROM finance_documents GROUP BY currency ORDER BY currency",
            "transactions": "SELECT currency, transaction_type, COUNT(*) AS count, SUM(amount) AS amount FROM finance_transactions GROUP BY currency, transaction_type ORDER BY currency, transaction_type",
            "settlements": "SELECT currency, COUNT(*) AS count, SUM(gross_amount) AS gross_amount, SUM(fees_amount) AS fees_amount, SUM(net_amount) AS net_amount FROM finance_settlements GROUP BY currency ORDER BY currency",
            "reconciliations": "SELECT status, COUNT(*) AS count FROM finance_reconciliations GROUP BY status ORDER BY status",
        }
        results: dict[str, Any] = {}
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            for name, query in queries.items():
                rows = [dict(row) for row in await conn.fetch(query)]
                for row in rows:
                    for field in ("gross_amount", "fees_amount", "net_amount", "amount"):
                        if row.get(field) is not None:
                            row[field] = str(row[field])
                results[name] = rows
        return results

    documents: dict[str, dict[str, Any]] = {}
    transactions: dict[tuple[str, str], dict[str, Any]] = {}
    settlements: dict[str, dict[str, Any]] = {}
    reconciliations: dict[str, int] = {}
    async with get_connection() as db:
        cursor = await db.execute("SELECT currency, gross_amount FROM finance_documents")
        for currency, gross_amount in await cursor.fetchall():
            item = documents.setdefault(currency, {"currency": currency, "count": 0, "gross_amount": Decimal("0")})
            item["count"] += 1
            item["gross_amount"] += _finance_amount(gross_amount, field="gross_amount")
        await cursor.close()

        cursor = await db.execute("SELECT currency, transaction_type, amount FROM finance_transactions")
        for currency, transaction_type, amount in await cursor.fetchall():
            item = transactions.setdefault(
                (currency, transaction_type),
                {"currency": currency, "transaction_type": transaction_type, "count": 0, "amount": Decimal("0")},
            )
            item["count"] += 1
            item["amount"] += _finance_amount(amount, field="amount")
        await cursor.close()

        cursor = await db.execute("SELECT currency, gross_amount, fees_amount, net_amount FROM finance_settlements")
        for currency, gross_amount, fees_amount, net_amount in await cursor.fetchall():
            item = settlements.setdefault(
                currency,
                {
                    "currency": currency,
                    "count": 0,
                    "gross_amount": Decimal("0"),
                    "fees_amount": Decimal("0"),
                    "net_amount": Decimal("0"),
                },
            )
            item["count"] += 1
            item["gross_amount"] += _finance_amount(gross_amount, field="gross_amount")
            item["fees_amount"] += _finance_amount(fees_amount, field="fees_amount")
            item["net_amount"] += _finance_amount(net_amount, field="net_amount")
        await cursor.close()

        cursor = await db.execute("SELECT status, COUNT(*) FROM finance_reconciliations GROUP BY status")
        reconciliations = {status: count for status, count in await cursor.fetchall()}
        await cursor.close()

    def serialize_amounts(rows: dict[Any, dict[str, Any]], amount_fields: tuple[str, ...]) -> list[dict[str, Any]]:
        serialized = []
        for key in sorted(rows):
            row = rows[key]
            serialized.append({
                field: str(value) if field in amount_fields else value
                for field, value in row.items()
            })
        return serialized

    return {
        "documents": serialize_amounts(documents, ("gross_amount",)),
        "transactions": serialize_amounts(transactions, ("amount",)),
        "settlements": serialize_amounts(settlements, ("gross_amount", "fees_amount", "net_amount")),
        "reconciliations": [{"status": status, "count": count} for status, count in sorted(reconciliations.items())],
    }


# ════════════════════════════════════════════════════════════════════════════
# Catalog extensions: categories / variants / attributes / media / imports
# ════════════════════════════════════════════════════════════════════════════

def _slugify(value: str) -> str:
    import re as _re
    v = (value or "").strip().lower()
    v = _re.sub(r"[^a-z0-9]+", "-", v)
    return _re.sub(r"-+", "-", v).strip("-") or "category"


async def upsert_category(*, name: str, parent_id: str = "", external_matterhorn_id: str = "", path: str = "", description: str = "", source_system: str = "", marketplace_mappings: dict[str, Any] | None = None, category_id: str | None = None) -> dict[str, Any]:
    name = str(name or "").strip()
    if not name:
        raise ValueError("Category name required")
    parent_id = str(parent_id or "")
    source_system = str(source_system or "")
    slug = _slugify(path or name)
    cid = str(category_id or external_matterhorn_id or f"cat-{_slugify(source_system+'-'+parent_id+'-'+name)}")
    mm = _json_dump(marketplace_mappings or {}, {})
    now = utc_now()
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO categories(id,name,slug,parent_id,external_matterhorn_id,path,description,source_system,marketplace_mappings_json,created_at,updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,NOW(),NOW())
                   ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,slug=EXCLUDED.slug,parent_id=EXCLUDED.parent_id,external_matterhorn_id=EXCLUDED.external_matterhorn_id,path=EXCLUDED.path,description=EXCLUDED.description,source_system=EXCLUDED.source_system,marketplace_mappings_json=EXCLUDED.marketplace_mappings_json,updated_at=NOW()
                   RETURNING *""",
                cid, name, slug, parent_id, str(external_matterhorn_id or ""), str(path or name), str(description or ""), source_system, mm,
            )
            if row is None:
                # fallback by unique tuple if id differs
                row = await conn.fetchrow("SELECT * FROM categories WHERE name=$1 AND parent_id=$2 AND source_system=$3", name, parent_id, source_system)
        return _decode_json_fields(dict(row), ("marketplace_mappings_json",)) if row else {"id": cid, "name": name}
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO categories(id,name,slug,parent_id,external_matterhorn_id,path,description,source_system,marketplace_mappings_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,slug=excluded.slug,parent_id=excluded.parent_id,external_matterhorn_id=excluded.external_matterhorn_id,path=excluded.path,description=excluded.description,source_system=excluded.source_system,marketplace_mappings_json=excluded.marketplace_mappings_json,updated_at=excluded.updated_at""",
            (cid, name, slug, parent_id, str(external_matterhorn_id or ""), str(path or name), str(description or ""), source_system, mm, now, now),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM categories WHERE id=?", (cid,))
        row = await cur.fetchone(); await cur.close()
        if row is None:
            cur = await db.execute("SELECT * FROM categories WHERE name=? AND parent_id=? AND source_system=?", (name, parent_id, source_system))
            row = await cur.fetchone(); await cur.close()
        return _decode_json_fields(dict(row), ("marketplace_mappings_json",)) if row else {"id": cid, "name": name}


async def list_categories(limit: int = 500) -> list[dict[str, Any]]:
    limit = min(max(limit,1),2000)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM categories ORDER BY path ASC, name ASC LIMIT $1", limit)
        return [_decode_json_fields(dict(r), ("marketplace_mappings_json",)) for r in rows]
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM categories ORDER BY path ASC, name ASC LIMIT ?", (limit,))
        rows = await cur.fetchall(); await cur.close()
        return [_decode_json_fields(dict(r), ("marketplace_mappings_json",)) for r in rows]


async def set_product_categories(product_sku: str, category_ids: list[str], *, primary_id: str | None = None, marketplace_mappings: dict[str, Any] | None = None, replace: bool = True) -> None:
    now = utc_now(); mm = _json_dump(marketplace_mappings or {}, {})
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if replace:
                    await conn.execute("DELETE FROM product_categories WHERE product_sku=$1", product_sku)
                for cid in category_ids:
                    if not cid: continue
                    await conn.execute(
                        """INSERT INTO product_categories(product_sku,category_id,is_primary,marketplace_mappings_json,created_at)
                           VALUES ($1,$2,$3,$4::jsonb,NOW()) ON CONFLICT(product_sku,category_id) DO UPDATE SET is_primary=EXCLUDED.is_primary, marketplace_mappings_json=EXCLUDED.marketplace_mappings_json""",
                        product_sku, str(cid), str(cid)==str(primary_id or (category_ids[0] if category_ids else "")), mm,
                    )
        return
    async with get_connection() as db:
        if replace:
            await db.execute("DELETE FROM product_categories WHERE product_sku=?", (product_sku,))
        for cid in category_ids:
            if not cid: continue
            await db.execute(
                """INSERT INTO product_categories(product_sku,category_id,is_primary,marketplace_mappings_json,created_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(product_sku,category_id) DO UPDATE SET is_primary=excluded.is_primary, marketplace_mappings_json=excluded.marketplace_mappings_json""",
                (product_sku, str(cid), 1 if str(cid)==str(primary_id or (category_ids[0] if category_ids else "")) else 0, mm, now),
            )
        await db.commit()


async def replace_product_variants(parent_sku: str, variants: list[dict[str, Any]]) -> int:
    now = utc_now(); count=0
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM product_variants WHERE parent_sku=$1", parent_sku)
                for v in variants or []:
                    vs = str(v.get("sku") or v.get("variant_sku") or "").strip()
                    if not vs: continue
                    count += 1
                    await conn.execute(
                        """INSERT INTO product_variants(id,parent_sku,sku,external_matterhorn_id,title,ean,barcode,supplier_price,sale_price,stock_quantity,reserved_quantity,available_quantity,weight_net_kg,weight_gross_kg,length_cm,width_cm,height_cm,attributes_json,raw_source_data_json,status,source_hash,created_at,updated_at)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18::jsonb,$19::jsonb,$20,$21,NOW(),NOW())
                           ON CONFLICT(sku) DO UPDATE SET parent_sku=EXCLUDED.parent_sku,external_matterhorn_id=EXCLUDED.external_matterhorn_id,title=EXCLUDED.title,ean=EXCLUDED.ean,barcode=EXCLUDED.barcode,supplier_price=EXCLUDED.supplier_price,sale_price=EXCLUDED.sale_price,stock_quantity=EXCLUDED.stock_quantity,reserved_quantity=EXCLUDED.reserved_quantity,available_quantity=EXCLUDED.available_quantity,weight_net_kg=EXCLUDED.weight_net_kg,weight_gross_kg=EXCLUDED.weight_gross_kg,length_cm=EXCLUDED.length_cm,width_cm=EXCLUDED.width_cm,height_cm=EXCLUDED.height_cm,attributes_json=EXCLUDED.attributes_json,raw_source_data_json=EXCLUDED.raw_source_data_json,status=EXCLUDED.status,source_hash=EXCLUDED.source_hash,updated_at=NOW()""",
                        str(v.get("id") or f"var-{parent_sku}-{vs}"), parent_sku, vs, str(v.get("external_matterhorn_id") or v.get("external_id") or ""), str(v.get("title") or v.get("name") or ""),
                        str(v.get("ean") or ""), str(v.get("barcode") or v.get("ean") or ""), _coerce_num(v.get("supplier_price", v.get("purchase_price", 0))), _coerce_num(v.get("sale_price", v.get("price", 0))),
                        _coerce_int(v.get("stock_quantity", v.get("stock", 0))), _coerce_int(v.get("reserved_quantity", 0)), max(_coerce_int(v.get("stock_quantity", v.get("stock", 0))) - _coerce_int(v.get("reserved_quantity", 0)), 0),
                        _coerce_num(v.get("weight_net_kg", 0)), _coerce_num(v.get("weight_gross_kg", 0)), _coerce_num(v.get("length_cm", 0)), _coerce_num(v.get("width_cm", 0)), _coerce_num(v.get("height_cm", 0)),
                        _json_dump(v.get("attributes_json") or v.get("attributes") or {}, {}), _json_dump(v.get("raw_source_data_json") or v.get("raw_data") or {}, {}), str(v.get("status") or "active"), str(v.get("source_hash") or ""),
                    )
                await conn.execute("UPDATE products SET has_variants=$1 WHERE sku=$2", count>0, parent_sku)
        return count
    async with get_connection() as db:
        await db.execute("DELETE FROM product_variants WHERE parent_sku=?", (parent_sku,))
        for v in variants or []:
            vs = str(v.get("sku") or v.get("variant_sku") or "").strip()
            if not vs: continue
            count += 1
            sq = _coerce_int(v.get("stock_quantity", v.get("stock", 0))); rq = _coerce_int(v.get("reserved_quantity", 0))
            await db.execute(
                """INSERT INTO product_variants(id,parent_sku,sku,external_matterhorn_id,title,ean,barcode,supplier_price,sale_price,stock_quantity,reserved_quantity,available_quantity,weight_net_kg,weight_gross_kg,length_cm,width_cm,height_cm,attributes_json,raw_source_data_json,status,source_hash,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(sku) DO UPDATE SET parent_sku=excluded.parent_sku,external_matterhorn_id=excluded.external_matterhorn_id,title=excluded.title,ean=excluded.ean,barcode=excluded.barcode,supplier_price=excluded.supplier_price,sale_price=excluded.sale_price,stock_quantity=excluded.stock_quantity,reserved_quantity=excluded.reserved_quantity,available_quantity=excluded.available_quantity,weight_net_kg=excluded.weight_net_kg,weight_gross_kg=excluded.weight_gross_kg,length_cm=excluded.length_cm,width_cm=excluded.width_cm,height_cm=excluded.height_cm,attributes_json=excluded.attributes_json,raw_source_data_json=excluded.raw_source_data_json,status=excluded.status,source_hash=excluded.source_hash,updated_at=excluded.updated_at""",
                (str(v.get("id") or f"var-{parent_sku}-{vs}"), parent_sku, vs, str(v.get("external_matterhorn_id") or v.get("external_id") or ""), str(v.get("title") or v.get("name") or ""),
                 str(v.get("ean") or ""), str(v.get("barcode") or v.get("ean") or ""), _coerce_num(v.get("supplier_price", v.get("purchase_price", 0))), _coerce_num(v.get("sale_price", v.get("price", 0))),
                 sq, rq, max(sq-rq,0), _coerce_num(v.get("weight_net_kg", 0)), _coerce_num(v.get("weight_gross_kg", 0)), _coerce_num(v.get("length_cm", 0)), _coerce_num(v.get("width_cm", 0)), _coerce_num(v.get("height_cm", 0)),
                 _json_dump(v.get("attributes_json") or v.get("attributes") or {}, {}), _json_dump(v.get("raw_source_data_json") or v.get("raw_data") or {}, {}), str(v.get("status") or "active"), str(v.get("source_hash") or ""), now, now),
            )
        await db.execute("UPDATE products SET has_variants=? WHERE sku=?", (1 if count>0 else 0, parent_sku))
        await db.commit()
    return count


async def replace_product_attributes(product_sku: str, attributes: list[dict[str, Any]], *, variant_sku: str = "") -> int:
    now = utc_now(); count=0
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if variant_sku:
                    await conn.execute("DELETE FROM product_attributes WHERE product_sku=$1 AND variant_sku=$2", product_sku, variant_sku)
                else:
                    await conn.execute("DELETE FROM product_attributes WHERE product_sku=$1", product_sku)
                for a in attributes or []:
                    name = str(a.get("name") or a.get("key") or "").strip()
                    if not name: continue
                    value = str(a.get("value") if a.get("value") is not None else a.get("val") or "")
                    vs = str(a.get("variant_sku") or variant_sku or "")
                    count += 1
                    aid = str(a.get("id") or f"attr-{product_sku}-{vs}-{name}-{abs(hash((name,value,vs)))%10**8}")
                    await conn.execute(
                        """INSERT INTO product_attributes(id,product_sku,variant_sku,name,value,unit,is_filterable,is_variant,source_field,extra_json,created_at,updated_at)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,NOW(),NOW())
                           ON CONFLICT(product_sku,variant_sku,name,value) DO UPDATE SET unit=EXCLUDED.unit,is_filterable=EXCLUDED.is_filterable,is_variant=EXCLUDED.is_variant,source_field=EXCLUDED.source_field,extra_json=EXCLUDED.extra_json,updated_at=NOW()""",
                        aid, product_sku, vs, name, value, str(a.get("unit") or ""), bool(a.get("is_filterable", False)), bool(a.get("is_variant", False) or vs), str(a.get("source_field") or a.get("source") or ""), _json_dump(a.get("extra_json") or a.get("extra") or {}, {}),
                    )
        return count
    async with get_connection() as db:
        if variant_sku:
            await db.execute("DELETE FROM product_attributes WHERE product_sku=? AND variant_sku=?", (product_sku, variant_sku))
        else:
            await db.execute("DELETE FROM product_attributes WHERE product_sku=?", (product_sku,))
        for a in attributes or []:
            name = str(a.get("name") or a.get("key") or "").strip()
            if not name: continue
            value = str(a.get("value") if a.get("value") is not None else a.get("val") or "")
            vs = str(a.get("variant_sku") or variant_sku or "")
            count += 1
            aid = str(a.get("id") or f"attr-{product_sku}-{vs}-{name}-{abs(hash((name,value,vs)))%10**8}")
            await db.execute(
                """INSERT INTO product_attributes(id,product_sku,variant_sku,name,value,unit,is_filterable,is_variant,source_field,extra_json,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(product_sku,variant_sku,name,value) DO UPDATE SET unit=excluded.unit,is_filterable=excluded.is_filterable,is_variant=excluded.is_variant,source_field=excluded.source_field,extra_json=excluded.extra_json,updated_at=excluded.updated_at""",
                (aid, product_sku, vs, name, value, str(a.get("unit") or ""), 1 if a.get("is_filterable") else 0, 1 if (a.get("is_variant") or vs) else 0, str(a.get("source_field") or a.get("source") or ""), _json_dump(a.get("extra_json") or a.get("extra") or {}, {}), now, now),
            )
        await db.commit()
    return count


async def replace_product_media(product_sku: str, media: list[dict[str, Any]]) -> int:
    now = utc_now(); count=0
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM product_media WHERE product_sku=$1", product_sku)
                for i,m in enumerate(media or []):
                    url = str(m.get("url") or m.get("src") or "").strip()
                    if not url: continue
                    count += 1
                    mid = str(m.get("id") or f"media-{product_sku}-{i}")
                    await conn.execute(
                        """INSERT INTO product_media(id,product_sku,variant_sku,media_type,url,position,title,alt_text,metadata_json,created_at,updated_at)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,NOW(),NOW())""",
                        mid, product_sku, str(m.get("variant_sku") or ""), str(m.get("media_type") or m.get("type") or "image"), url, _coerce_int(m.get("position", i)), str(m.get("title") or ""), str(m.get("alt_text") or m.get("alt") or ""), _json_dump(m.get("metadata_json") or m.get("metadata") or {}, {}),
                    )
        return count
    async with get_connection() as db:
        await db.execute("DELETE FROM product_media WHERE product_sku=?", (product_sku,))
        for i,m in enumerate(media or []):
            url = str(m.get("url") or m.get("src") or "").strip()
            if not url: continue
            count += 1
            mid = str(m.get("id") or f"media-{product_sku}-{i}")
            await db.execute(
                """INSERT INTO product_media(id,product_sku,variant_sku,media_type,url,position,title,alt_text,metadata_json,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, product_sku, str(m.get("variant_sku") or ""), str(m.get("media_type") or m.get("type") or "image"), url, _coerce_int(m.get("position", i)), str(m.get("title") or ""), str(m.get("alt_text") or m.get("alt") or ""), _json_dump(m.get("metadata_json") or m.get("metadata") or {}, {}), now, now),
            )
        await db.commit()
    return count


async def upsert_product_aggregate(data: dict[str, Any], *, sync_rules: dict[str, Any] | None = None) -> dict[str, Any]:
    """Upsert product + related categories/variants/attributes/media in a best-effort transactional flow."""
    sku = str(data.get("sku") or "").strip()
    if not sku:
        raise ValueError("sku required")
    changed = await upsert_product(
        sku=sku,
        title=str(data.get("title") or data.get("name") or sku),
        category=str(data.get("category") or ""),
        supplier_price=_coerce_num(data.get("supplier_price", data.get("purchase_price", data.get("price", 0)))),
        stock=_coerce_int(data.get("stock", data.get("stock_quantity", 0))),
        source_hash=str(data.get("source_hash") or ""),
        raw_description=str(data.get("raw_description") or data.get("description") or ""),
        status=str(data.get("status") or "active"),
        sync_rules=sync_rules or data.get("sync_rules") or {},
        **{k:v for k,v in data.items() if k not in {"sku","title","category","supplier_price","stock","source_hash","raw_description","status"}},
    )
    # categories
    cat_names = data.get("categories") or data.get("category_names") or []
    cat_ids: list[str] = []
    primary_id = None
    if isinstance(cat_names, str):
        cat_names = [c.strip() for c in cat_names.replace(">", "/").split("/") if c.strip()] or ([cat_names] if cat_names.strip() else [])
    for idx,c in enumerate(cat_names or []):
        if isinstance(c, dict):
            cobj = await upsert_category(name=str(c.get("name") or c.get("title") or ""), parent_id=str(c.get("parent_id") or ""), external_matterhorn_id=str(c.get("external_matterhorn_id") or c.get("external_id") or ""), path=str(c.get("path") or c.get("name") or ""), source_system=str(c.get("source_system") or data.get("source_system") or ""), marketplace_mappings=c.get("marketplace_mappings") or c.get("marketplace_mappings_json") or {})
        else:
            cobj = await upsert_category(name=str(c), path=str(c), source_system=str(data.get("source_system") or ""))
        cid = str(cobj.get("id") or "")
        if cid:
            cat_ids.append(cid)
            if idx==0: primary_id = cid
    if not cat_ids and str(data.get("category") or "").strip():
        cobj = await upsert_category(name=str(data.get("category")), path=str(data.get("category")), source_system=str(data.get("source_system") or ""))
        if cobj.get("id"): cat_ids=[str(cobj["id"])]; primary_id=cat_ids[0]
    if cat_ids:
        await set_product_categories(sku, cat_ids, primary_id=primary_id, replace=True)

    variants = data.get("variants") or []
    vcount = await replace_product_variants(sku, variants) if (isinstance(variants, list)) else 0
    attributes = data.get("attributes") or data.get("dynamic_attributes") or []
    acount = await replace_product_attributes(sku, attributes) if isinstance(attributes, list) else 0
    media = data.get("media") or []
    if not media:
        media=[]
        if data.get("main_image_url"): media.append({"url": data.get("main_image_url"), "position": 0, "media_type":"image"})
        for i,u in enumerate((data.get("gallery_json") or data.get("images") or []) or [], start=1):
            if isinstance(u, dict): media.append(u)
            elif str(u).strip(): media.append({"url": str(u), "position": i, "media_type":"image"})
    mcount = await replace_product_media(sku, media) if isinstance(media, list) else 0
    return {"sku": sku, "changed": bool(changed), "categories_linked": len(cat_ids), "variants_linked": vcount, "attributes_saved": acount, "media_saved": mcount}



# --- Product Intelligence / Export Gate ---

def _pi_dump(val: Any, default: Any) -> str:
    if val is None:
        val = default
    if isinstance(val, str):
        return val
    try:
        return json.dumps(val, ensure_ascii=False)
    except Exception:
        return json.dumps(default, ensure_ascii=False)


async def save_product_analysis_run(run: dict) -> dict:
    run_id = str(run.get("run_id") or uuid.uuid4())
    summary = _pi_dump(run.get("summary_json") or run.get("summary") or {}, {})
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO product_analysis_runs (run_id, sku, channel, status, model_combo, policy_version, summary_json, started_at, finished_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::timestamptz,$9::timestamptz) "
                "ON CONFLICT (run_id) DO UPDATE SET status=EXCLUDED.status, summary_json=EXCLUDED.summary_json, finished_at=EXCLUDED.finished_at",
                run_id, str(run.get("sku") or ""), str(run.get("channel") or ""),
                str(run.get("status") or "completed"), str(run.get("model_combo") or ""), str(run.get("policy_version") or "v1"),
                summary, run.get("started_at"), run.get("finished_at"),
            )
    else:
        async with get_connection() as db:
            await db.execute(
                "INSERT OR REPLACE INTO product_analysis_runs (run_id,sku,channel,status,model_combo,policy_version,summary_json,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, str(run.get("sku") or ""), str(run.get("channel") or ""), str(run.get("status") or "completed"), str(run.get("model_combo") or ""), str(run.get("policy_version") or "v1"), summary, run.get("started_at"), run.get("finished_at")),
            )
            await db.commit()
    run["run_id"] = run_id
    return run


async def save_product_analysis_result(result: dict) -> dict:
    scorecard = result.get("scorecard_json") or result.get("scorecard") or {}
    total = result.get("total_score")
    if total is None and isinstance(scorecard, dict):
        total = scorecard.get("total") or scorecard.get("total_score") or 0
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO product_analysis_results (run_id, sku, channel, decision, total_score, confidence, scorecard_json, reasons_json, signals_json, modifications_json, expires_at, source) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9::jsonb,$10::jsonb,$11::timestamptz,$12) RETURNING *",
                str(result.get("run_id") or ""), str(result.get("sku") or ""), str(result.get("channel") or "shopware"),
                str(result.get("decision") or "review_required"), total, result.get("confidence") or 0,
                _pi_dump(scorecard, {}), _pi_dump(result.get("reasons") or [], []), _pi_dump(result.get("signals") or [], []),
                _pi_dump(result.get("modifications") or [], []), result.get("expires_at"), str(result.get("source") or "product_intelligence"),
            )
            return dict(row) if row else result
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO product_analysis_results (run_id,sku,channel,decision,total_score,confidence,scorecard_json,reasons_json,signals_json,modifications_json,expires_at,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(result.get("run_id") or ""), str(result.get("sku") or ""), str(result.get("channel") or "shopware"), str(result.get("decision") or "review_required"), total, result.get("confidence") or 0, _pi_dump(scorecard, {}), _pi_dump(result.get("reasons") or [], []), _pi_dump(result.get("signals") or [], []), _pi_dump(result.get("modifications") or [], []), result.get("expires_at"), str(result.get("source") or "product_intelligence")),
        )
        await db.commit()
    return result


async def save_product_modifications(run_id: str, sku: str, channel: str, modifications: list[dict]) -> int:
    count = 0
    for m in modifications or []:
        before = _pi_dump(m.get("before") if "before" in m else {}, {})
        after = _pi_dump(m.get("after") if "after" in m else m, {})
        if uses_postgres():
            pool = await get_pg_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO product_modifications (run_id, sku, channel, field, before_json, after_json, action, status, reason, actor) "
                    "VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8,$9,$10)",
                    str(run_id or ""), str(sku or ""), str(channel or ""), str(m.get("field") or ""),
                    before, after, str(m.get("action") or "suggest"), str(m.get("status") or "suggested"), str(m.get("reason") or ""), str(m.get("actor") or "system"),
                )
        else:
            async with get_connection() as db:
                await db.execute(
                    "INSERT INTO product_modifications (run_id,sku,channel,field,before_json,after_json,action,status,reason,actor) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (str(run_id or ""), str(sku or ""), str(channel or ""), str(m.get("field") or ""), before, after, str(m.get("action") or "suggest"), str(m.get("status") or "suggested"), str(m.get("reason") or ""), str(m.get("actor") or "system")),
                )
                await db.commit()
        count += 1
    return count


async def get_product_export_approval(sku: str, channel: str) -> Optional[dict]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM product_export_approvals WHERE sku=$1 AND channel=$2", sku, channel)
            return dict(row) if row else None
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM product_export_approvals WHERE sku=? AND channel=?", (sku, channel))
        row = await cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


async def upsert_product_export_approval(rec: dict) -> dict:
    sku = str(rec.get("sku") or "")
    channel = str(rec.get("channel") or "shopware")
    reasons = _pi_dump(rec.get("reasons_json") or rec.get("reasons") or [], [])
    mods = _pi_dump(rec.get("modifications_json") or rec.get("modifications") or {}, {})
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO product_export_approvals (sku, channel, decision, score, reasons_json, modifications_json, source, actor, run_id, expires_at, updated_at) "
                "VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8,$9,$10::timestamptz,NOW()) "
                "ON CONFLICT (sku, channel) DO UPDATE SET decision=EXCLUDED.decision, score=EXCLUDED.score, reasons_json=EXCLUDED.reasons_json, "
                "modifications_json=EXCLUDED.modifications_json, source=EXCLUDED.source, actor=EXCLUDED.actor, run_id=EXCLUDED.run_id, expires_at=EXCLUDED.expires_at, updated_at=NOW() "
                "RETURNING *",
                sku, channel, str(rec.get("decision") or "review_required"), rec.get("score"),
                reasons, mods, str(rec.get("source") or "manual"), str(rec.get("actor") or "system"), str(rec.get("run_id") or ""), rec.get("expires_at"),
            )
            return dict(row) if row else rec
    async with get_connection() as db:
        await db.execute(
            "INSERT OR REPLACE INTO product_export_approvals (sku,channel,decision,score,reasons_json,modifications_json,source,actor,run_id,expires_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (sku, channel, str(rec.get("decision") or "review_required"), rec.get("score"), reasons, mods, str(rec.get("source") or "manual"), str(rec.get("actor") or "system"), str(rec.get("run_id") or ""), rec.get("expires_at")),
        )
        await db.commit()
    return rec


async def list_product_export_approvals(channel: str | None = None, decision: str | None = None, limit: int = 200) -> list[dict]:
    if uses_postgres():
        pool = await get_pg_pool()
        where = []
        params = []
        if channel:
            params.append(channel); where.append(f"channel=${len(params)}")
        if decision:
            params.append(decision); where.append(f"decision=${len(params)}")
        params.append(limit)
        q = "SELECT * FROM product_export_approvals " + ("WHERE " + " AND ".join(where) if where else "") + f" ORDER BY updated_at DESC LIMIT ${len(params)}"
        async with pool.acquire() as conn:
            rows = await conn.fetch(q, *params)
            return [dict(r) for r in rows]
    where = []
    params = []
    q = "SELECT * FROM product_export_approvals"
    if channel:
        where.append("channel=?"); params.append(channel)
    if decision:
        where.append("decision=?"); params.append(decision)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY updated_at DESC LIMIT ?"; params.append(limit)
    async with get_connection() as db:
        cur = await db.execute(q, tuple(params))
        rows = await cur.fetchall(); cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]


async def add_product_market_signal(signal: dict) -> dict:
    payload = _pi_dump(signal.get("payload") or signal.get("payload_json") or {}, {})
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO product_market_signals (sku,category,source,metric,value,period,payload_json) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) RETURNING *",
                str(signal.get("sku") or ""), str(signal.get("category") or ""), str(signal.get("source") or ""), str(signal.get("metric") or ""), float(signal.get("value") or 0), str(signal.get("period") or ""), payload,
            )
            return dict(row) if row else signal
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO product_market_signals (sku,category,source,metric,value,period,payload_json) VALUES (?,?,?,?,?,?,?)",
            (str(signal.get("sku") or ""), str(signal.get("category") or ""), str(signal.get("source") or ""), str(signal.get("metric") or ""), float(signal.get("value") or 0), str(signal.get("period") or ""), payload),
        )
        await db.commit()
    return signal


async def list_product_market_signals(sku: str | None = None, limit: int = 100) -> list[dict]:
    if uses_postgres():
        pool = await get_pg_pool()
        if sku:
            q = "SELECT * FROM product_market_signals WHERE sku=$1 ORDER BY created_at DESC LIMIT $2"; params = (sku, limit)
        else:
            q = "SELECT * FROM product_market_signals ORDER BY created_at DESC LIMIT $1"; params = (limit,)
        async with pool.acquire() as conn:
            rows = await conn.fetch(q, *params)
            return [dict(r) for r in rows]
    async with get_connection() as db:
        if sku:
            cur = await db.execute("SELECT * FROM product_market_signals WHERE sku=? ORDER BY created_at DESC LIMIT ?", (sku, limit))
        else:
            cur = await db.execute("SELECT * FROM product_market_signals ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = await cur.fetchall(); cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]


async def list_product_analysis_results(sku: str | None = None, channel: str | None = None, limit: int = 100) -> list[dict]:
    if uses_postgres():
        pool = await get_pg_pool()
        where = []
        params = []
        if sku:
            params.append(sku); where.append(f"sku=${len(params)}")
        if channel:
            params.append(channel); where.append(f"channel=${len(params)}")
        params.append(limit)
        q = "SELECT * FROM product_analysis_results " + ("WHERE " + " AND ".join(where) if where else "") + f" ORDER BY created_at DESC LIMIT ${len(params)}"
        async with pool.acquire() as conn:
            rows = await conn.fetch(q, *params)
            return [dict(r) for r in rows]
    where = []
    params = []
    if sku:
        where.append("sku=?"); params.append(sku)
    if channel:
        where.append("channel=?"); params.append(channel)
    q = "SELECT * FROM product_analysis_results " + ("WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
    async with get_connection() as db:
        cur = await db.execute(q, tuple(params))
        rows = await cur.fetchall(); cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]


# --- Catalog Intelligence helpers (Slice 1, pi_* tables) ---
def _pi_json_dumps(v: Any) -> str:
    return json.dumps(v if v is not None else {}, ensure_ascii=False, default=str)

def _pi_json_loads(v: Any, default: Any) -> Any:
    if v is None or v == "": return default
    if isinstance(v, (dict, list)): return v
    try: return json.loads(v)
    except Exception: return default

async def pi_save_scoring_config(name: str, config: dict[str, Any], is_default: bool = False) -> dict[str, Any]:
    cid=str(uuid.uuid4()); now=utc_now(); cfg=_pi_json_dumps(config)
    if uses_postgres():
        pool=await get_pg_pool()
        async with pool.acquire() as conn:
            if is_default: await conn.execute("UPDATE pi_scoring_configs SET is_default=FALSE, updated_at=NOW()")
            await conn.execute("""INSERT INTO pi_scoring_configs(id,name,config_json,is_default,created_at,updated_at) VALUES($1,$2,$3::jsonb,$4,NOW(),NOW())""", cid,name,cfg,bool(is_default))
            row=await conn.fetchrow("SELECT * FROM pi_scoring_configs WHERE id=$1", cid)
        d=dict(row); d["config_json"]=_pi_json_loads(d.get("config_json"),{}); return d
    async with get_connection() as db:
        if is_default: await db.execute("UPDATE pi_scoring_configs SET is_default=0, updated_at=?", (now,))
        await db.execute("""INSERT INTO pi_scoring_configs(id,name,config_json,is_default,created_at,updated_at) VALUES(?,?,?,?,?,?)""", (cid,name,cfg,1 if is_default else 0,now,now))
        await db.commit(); cur=await db.execute("SELECT * FROM pi_scoring_configs WHERE id=?", (cid,)); row=await cur.fetchone(); await cur.close()
    d=dict(row) if row else {"id":cid}; d["config_json"]=_pi_json_loads(d.get("config_json"),{}); d["is_default"]=bool(d.get("is_default")); return d

async def pi_get_default_scoring_config() -> dict[str, Any] | None:
    if uses_postgres():
        pool=await get_pg_pool()
        async with pool.acquire() as conn:
            row=await conn.fetchrow("SELECT * FROM pi_scoring_configs WHERE is_default=TRUE ORDER BY updated_at DESC LIMIT 1")
        if not row: return None
        d=dict(row); d["config_json"]=_pi_json_loads(d.get("config_json"),{}); return d
    async with get_connection() as db:
        cur=await db.execute("SELECT * FROM pi_scoring_configs WHERE is_default=1 ORDER BY updated_at DESC LIMIT 1")
        row=await cur.fetchone(); await cur.close()
    if not row: return None
    d=dict(row); d["config_json"]=_pi_json_loads(d.get("config_json"),{}); d["is_default"]=bool(d.get("is_default")); return d

async def pi_save_product_score(run_id: str, score: dict[str, Any], config: dict[str, Any]) -> None:
    sid=str(uuid.uuid4()); now=utc_now()
    if uses_postgres():
        pool=await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""INSERT INTO pi_product_scores(id,run_id,sku,score,eligible,criteria_json,exclusions_json,reasons_json,config_json,created_at)
                VALUES($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8::jsonb,$9::jsonb,NOW())""", sid,run_id,str(score.get('sku','')),float(score.get('score',0)),bool(score.get('eligible')),_pi_json_dumps(score.get('criteria',{})),_pi_json_dumps(score.get('exclusions',[])),_pi_json_dumps(score.get('reasons',[])),_pi_json_dumps(config))
        return
    async with get_connection() as db:
        await db.execute("""INSERT INTO pi_product_scores(id,run_id,sku,score,eligible,criteria_json,exclusions_json,reasons_json,config_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (sid,run_id,str(score.get('sku','')),float(score.get('score',0)),1 if score.get('eligible') else 0,_pi_json_dumps(score.get('criteria',{})),_pi_json_dumps(score.get('exclusions',[])),_pi_json_dumps(score.get('reasons',[])),_pi_json_dumps(config),now))
        await db.commit()

async def pi_create_selection_run(mode: str, config: dict[str, Any], created_by: str = "") -> dict[str, Any]:
    rid=str(uuid.uuid4()); now=utc_now(); cfg=_pi_json_dumps(config)
    if uses_postgres():
        pool=await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""INSERT INTO pi_selection_runs(id,mode,status,target_count,min_count,max_count,config_json,created_by,created_at) VALUES($1,$2,'running',$3,$4,$5,$6::jsonb,$7,NOW())""",
                rid,mode,int(config.get('target_count',600)),int(config.get('min_count',500)),int(config.get('max_count',750)),cfg,created_by)
            row=await conn.fetchrow("SELECT * FROM pi_selection_runs WHERE id=$1", rid)
        d=dict(row); d["config_json"]=_pi_json_loads(d.get("config_json"),{}); d["report_json"]=_pi_json_loads(d.get("report_json"),{}); return d
    async with get_connection() as db:
        await db.execute("""INSERT INTO pi_selection_runs(id,mode,status,target_count,min_count,max_count,config_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (rid,mode,'running',int(config.get('target_count',600)),int(config.get('min_count',500)),int(config.get('max_count',750)),cfg,created_by,now))
        await db.commit(); cur=await db.execute("SELECT * FROM pi_selection_runs WHERE id=?", (rid,)); row=await cur.fetchone(); await cur.close()
    d=dict(row) if row else {"id":rid}; d["config_json"]=_pi_json_loads(d.get("config_json"),{}); d["report_json"]=_pi_json_loads(d.get("report_json"),{}); return d

async def pi_finish_selection_run(run_id: str, status: str, report: dict[str, Any], selected_count: int, eligible_count: int, total_count: int, error_message: str = "") -> None:
    now=utc_now(); rep=_pi_json_dumps(report)
    if uses_postgres():
        pool=await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""UPDATE pi_selection_runs SET status=$2,selected_count=$3,eligible_count=$4,total_count=$5,report_json=$6::jsonb,error_message=$7,completed_at=NOW() WHERE id=$1""",
                run_id,status,int(selected_count),int(eligible_count),int(total_count),rep,error_message)
        return
    async with get_connection() as db:
        await db.execute("""UPDATE pi_selection_runs SET status=?,selected_count=?,eligible_count=?,total_count=?,report_json=?,error_message=?,completed_at=? WHERE id=?""",
            (status,int(selected_count),int(eligible_count),int(total_count),rep,error_message,now,run_id))
        await db.commit()

async def pi_save_selection_item(run_id: str, rank: int, item: dict[str, Any], selected: bool, reason: str) -> None:
    iid=str(uuid.uuid4()); now=utc_now(); snap={k:item.get(k) for k in ["sku","title","category","status","score","criteria"]}
    if uses_postgres():
        pool=await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""INSERT INTO pi_selection_items(id,run_id,sku,rank,score,selected,decision_reason,exclusions_json,snapshot_json,created_at)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,NOW())""", iid,run_id,str(item.get('sku','')),int(rank),float(item.get('score',0)),bool(selected),reason,_pi_json_dumps(item.get('exclusions',[])),_pi_json_dumps(snap))
        return
    async with get_connection() as db:
        await db.execute("""INSERT INTO pi_selection_items(id,run_id,sku,rank,score,selected,decision_reason,exclusions_json,snapshot_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (iid,run_id,str(item.get('sku','')),int(rank),float(item.get('score',0)),1 if selected else 0,reason,_pi_json_dumps(item.get('exclusions',[])),_pi_json_dumps(snap),now))
        await db.commit()

async def pi_get_selection_run(run_id: str) -> dict[str, Any] | None:
    if uses_postgres():
        pool=await get_pg_pool()
        async with pool.acquire() as conn:
            row=await conn.fetchrow("SELECT * FROM pi_selection_runs WHERE id=$1", run_id)
            items=await conn.fetch("SELECT * FROM pi_selection_items WHERE run_id=$1 ORDER BY selected DESC, rank ASC LIMIT 1000", run_id)
        if not row: return None
        d=dict(row); d["config_json"]=_pi_json_loads(d.get("config_json"),{}); d["report_json"]=_pi_json_loads(d.get("report_json"),{})
        d["items"]=[{**dict(r),"exclusions_json":_pi_json_loads(r.get("exclusions_json"),[]),"snapshot_json":_pi_json_loads(r.get("snapshot_json"),{})} for r in items]; return d
    async with get_connection() as db:
        cur=await db.execute("SELECT * FROM pi_selection_runs WHERE id=?", (run_id,)); row=await cur.fetchone(); await cur.close()
        if not row: return None
        cur=await db.execute("SELECT * FROM pi_selection_items WHERE run_id=? ORDER BY selected DESC, rank ASC LIMIT 1000", (run_id,)); items=await cur.fetchall(); await cur.close()
    d=dict(row); d["config_json"]=_pi_json_loads(d.get("config_json"),{}); d["report_json"]=_pi_json_loads(d.get("report_json"),{})
    out=[]
    for r in items:
        it=dict(r); it["selected"]=bool(it.get("selected")); it["exclusions_json"]=_pi_json_loads(it.get("exclusions_json"),[]); it["snapshot_json"]=_pi_json_loads(it.get("snapshot_json"),{}); out.append(it)
    d["items"]=out; return d

async def pi_list_selection_runs(limit: int = 20) -> list[dict[str, Any]]:
    limit=min(max(int(limit or 20),1),100)
    if uses_postgres():
        pool=await get_pg_pool()
        async with pool.acquire() as conn:
            rows=await conn.fetch("SELECT id,mode,status,target_count,min_count,max_count,selected_count,eligible_count,total_count,error_message,created_by,created_at,completed_at FROM pi_selection_runs ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]
    async with get_connection() as db:
        cur=await db.execute("SELECT id,mode,status,target_count,min_count,max_count,selected_count,eligible_count,total_count,error_message,created_by,created_at,completed_at FROM pi_selection_runs ORDER BY created_at DESC LIMIT ?", (limit,))
        rows=await cur.fetchall(); await cur.close()
    return [dict(r) for r in rows]

async def pi_create_export_job(mode: str, target: str, selection_run_id: str, requested_count: int, payload: dict[str, Any], created_by: str = "", idempotency_key: str = "") -> dict[str, Any]:
    import hashlib
    eid=str(uuid.uuid4()); now=utc_now(); idem=idempotency_key or hashlib.sha256(f"{mode}:{target}:{selection_run_id}:{_pi_json_dumps(payload)}".encode()).hexdigest()
    if uses_postgres():
        pool=await get_pg_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT * FROM pi_export_jobs WHERE idempotency_key=$1 LIMIT 1", idem) if idem else None
            if existing: d=dict(existing); d["payload_json"]=_pi_json_loads(d.get("payload_json"),{}); d["result_json"]=_pi_json_loads(d.get("result_json"),{}); d["idempotent_reuse"]=True; return d
            await conn.execute("""INSERT INTO pi_export_jobs(id,idempotency_key,mode,target,status,selection_run_id,requested_count,payload_json,created_by,created_at,updated_at) VALUES($1,$2,$3,$4,'completed',$5,$6,$7::jsonb,$8,NOW(),NOW())""", eid,idem,mode,target,selection_run_id,int(requested_count),_pi_json_dumps(payload),created_by)
            row=await conn.fetchrow("SELECT * FROM pi_export_jobs WHERE id=$1", eid)
        d=dict(row); d["payload_json"]=_pi_json_loads(d.get("payload_json"),{}); d["result_json"]=_pi_json_loads(d.get("result_json"),{}); return d
    async with get_connection() as db:
        if idem:
            cur=await db.execute("SELECT * FROM pi_export_jobs WHERE idempotency_key=? LIMIT 1", (idem,)); existing=await cur.fetchone(); await cur.close()
            if existing: d=dict(existing); d["payload_json"]=_pi_json_loads(d.get("payload_json"),{}); d["result_json"]=_pi_json_loads(d.get("result_json"),{}); d["idempotent_reuse"]=True; return d
        await db.execute("""INSERT INTO pi_export_jobs(id,idempotency_key,mode,target,status,selection_run_id,requested_count,payload_json,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (eid,idem,mode,target,'completed',selection_run_id,int(requested_count),_pi_json_dumps(payload),created_by,now,now))
        await db.commit(); cur=await db.execute("SELECT * FROM pi_export_jobs WHERE id=?", (eid,)); row=await cur.fetchone(); await cur.close()
    d=dict(row) if row else {"id":eid}; d["payload_json"]=_pi_json_loads(d.get("payload_json"),{}); d["result_json"]=_pi_json_loads(d.get("result_json"),{}); return d

async def pi_update_export_job(job_id: str, **fields: Any) -> None:
    if not fields: return
    now=utc_now(); fields["updated_at"]=now
    if uses_postgres():
        sets=[]; vals=[]; i=1
        for k,v in fields.items():
            if k in {"payload_json","result_json"}: sets.append(f"{k}=${i}::jsonb"); vals.append(_pi_json_dumps(v))
            else: sets.append(f"{k}=${i}"); vals.append(v)
            i+=1
        vals.append(job_id)
        pool=await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(f"UPDATE pi_export_jobs SET {', '.join(sets)} WHERE id=${i}", *vals)
        return
    sets=[]; vals=[]
    for k,v in fields.items():
        if k in {"payload_json","result_json"}: v=_pi_json_dumps(v)
        sets.append(f"{k}=?"); vals.append(v)
    vals.append(job_id)
    async with get_connection() as db:
        await db.execute(f"UPDATE pi_export_jobs SET {', '.join(sets)} WHERE id=?", tuple(vals)); await db.commit()

# ── Marketplace Command Center helpers ───────────────────────────────────

def _mcc_decode(row: dict[str, Any], *, include_credentials: bool = False) -> dict[str, Any]:
    for k in ("config_json","credentials_json","reasons_json","blockers_json","recommendations_json","criteria_json","canonical_product_json","marketplace_content_json","marketplace_pricing_json","attribute_mapping_json","payload_json","result_json","request_json","response_json","raw_status_json","details_json"):
        if k in row and isinstance(row[k], str):
            try: row[k]=json.loads(row[k])
            except Exception: pass
    if "credentials_json" in row:
        creds = row.get("credentials_json") or {}
        row["credentials_configured"] = bool(creds)
        if not include_credentials:
            row["credentials_json"] = {"configured": bool(creds)}
    for b in ("eligible","require_available_stock","require_valid_images","require_valid_pricing","require_category_mapping"):
        if b in row and isinstance(row[b], int):
            row[b]=bool(row[b])
    return row


async def mcc_upsert_marketplace(data: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    mid = str(data.get("id") or uuid.uuid4())
    fields = {
        "id": mid,
        "name": str(data.get("name") or mid),
        "connector_type": str(data.get("connector_type") or data.get("type") or ""),
        "status": str(data.get("status") or "inactive"),
        "config_json": _json_dump(data.get("config_json") or data.get("config") or {}, {}),
        "credentials_json": _json_dump(data.get("credentials_json") or data.get("credentials") or {}, {}),
        "currency": str(data.get("currency") or "EUR"),
        "country": str(data.get("country") or "DE"),
        "language": str(data.get("language") or "de-DE"),
        "min_score": float(data.get("min_score") or 70),
        "target_products": int(data.get("target_products") or 0),
        "min_products": int(data.get("min_products") or 0),
        "max_products": int(data.get("max_products") or 0),
        "require_available_stock": bool(data.get("require_available_stock", True)),
        "require_valid_images": bool(data.get("require_valid_images", True)),
        "require_valid_pricing": bool(data.get("require_valid_pricing", True)),
        "require_category_mapping": bool(data.get("require_category_mapping", False)),
        "sync_interval_minutes": int(data.get("sync_interval_minutes") or 60),
        "updated_at": now,
    }
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO marketplaces (id,name,connector_type,status,config_json,credentials_json,currency,country,language,min_score,target_products,min_products,max_products,require_available_stock,require_valid_images,require_valid_pricing,require_category_mapping,sync_interval_minutes,created_at,updated_at)
                VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,NOW(),NOW())
                ON CONFLICT(id) DO UPDATE SET
                  name=EXCLUDED.name, connector_type=EXCLUDED.connector_type, status=EXCLUDED.status,
                  config_json=EXCLUDED.config_json, credentials_json=CASE WHEN EXCLUDED.credentials_json='{}'::jsonb THEN marketplaces.credentials_json ELSE EXCLUDED.credentials_json END,
                  currency=EXCLUDED.currency, country=EXCLUDED.country, language=EXCLUDED.language, min_score=EXCLUDED.min_score,
                  target_products=EXCLUDED.target_products, min_products=EXCLUDED.min_products, max_products=EXCLUDED.max_products,
                  require_available_stock=EXCLUDED.require_available_stock, require_valid_images=EXCLUDED.require_valid_images,
                  require_valid_pricing=EXCLUDED.require_valid_pricing, require_category_mapping=EXCLUDED.require_category_mapping,
                  sync_interval_minutes=EXCLUDED.sync_interval_minutes, updated_at=NOW()
            """, fields["id"],fields["name"],fields["connector_type"],fields["status"],fields["config_json"],fields["credentials_json"],fields["currency"],fields["country"],fields["language"],fields["min_score"],fields["target_products"],fields["min_products"],fields["max_products"],fields["require_available_stock"],fields["require_valid_images"],fields["require_valid_pricing"],fields["require_category_mapping"],fields["sync_interval_minutes"])
            row = await conn.fetchrow("SELECT * FROM marketplaces WHERE id=$1", mid)
        return _mcc_decode(dict(row))
    async with get_connection() as db:
        await db.execute("""
            INSERT INTO marketplaces (id,name,connector_type,status,config_json,credentials_json,currency,country,language,min_score,target_products,min_products,max_products,require_available_stock,require_valid_images,require_valid_pricing,require_category_mapping,sync_interval_minutes,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name, connector_type=excluded.connector_type, status=excluded.status,
              config_json=excluded.config_json,
              credentials_json=CASE WHEN excluded.credentials_json='{}' THEN marketplaces.credentials_json ELSE excluded.credentials_json END,
              currency=excluded.currency, country=excluded.country, language=excluded.language, min_score=excluded.min_score,
              target_products=excluded.target_products, min_products=excluded.min_products, max_products=excluded.max_products,
              require_available_stock=excluded.require_available_stock, require_valid_images=excluded.require_valid_images,
              require_valid_pricing=excluded.require_valid_pricing, require_category_mapping=excluded.require_category_mapping,
              sync_interval_minutes=excluded.sync_interval_minutes, updated_at=excluded.updated_at
        """, (mid,fields["name"],fields["connector_type"],fields["status"],fields["config_json"],fields["credentials_json"],fields["currency"],fields["country"],fields["language"],fields["min_score"],fields["target_products"],fields["min_products"],fields["max_products"],1 if fields["require_available_stock"] else 0,1 if fields["require_valid_images"] else 0,1 if fields["require_valid_pricing"] else 0,1 if fields["require_category_mapping"] else 0,fields["sync_interval_minutes"],now,now))
        await db.commit()
        cur = await db.execute("SELECT * FROM marketplaces WHERE id=?", (mid,))
        row = await cur.fetchone(); await cur.close()
        return _mcc_decode(dict(row)) if row else {"id": mid}


async def mcc_list_marketplaces(*, include_credentials: bool = False) -> list[dict[str, Any]]:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM marketplaces ORDER BY created_at DESC")
        return [_mcc_decode(dict(r), include_credentials=include_credentials) for r in rows]
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM marketplaces ORDER BY created_at DESC")
        rows = await cur.fetchall(); await cur.close()
        return [_mcc_decode(dict(r), include_credentials=include_credentials) for r in rows]


async def mcc_get_marketplace(marketplace_id: str, *, include_credentials: bool = False) -> dict[str, Any] | None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM marketplaces WHERE id=$1", marketplace_id)
        return _mcc_decode(dict(row), include_credentials=include_credentials) if row else None
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM marketplaces WHERE id=?", (marketplace_id,))
        row = await cur.fetchone(); await cur.close()
        return _mcc_decode(dict(row), include_credentials=include_credentials) if row else None


async def mcc_update_marketplace_health(marketplace_id: str, status: str, message: str) -> None:
    now = utc_now()
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE marketplaces SET last_health_at=NOW(), last_health_status=$1, last_health_message=$2, updated_at=NOW() WHERE id=$3", status, (message or '')[:1000], marketplace_id)
        return
    async with get_connection() as db:
        await db.execute("UPDATE marketplaces SET last_health_at=?, last_health_status=?, last_health_message=?, updated_at=? WHERE id=?", (now,status,(message or '')[:1000],now,marketplace_id))
        await db.commit()


async def mcc_upsert_selection(marketplace_id: str, product_sku: str, result: dict[str, Any], *, selected_by: str = "ai") -> dict[str, Any]:
    now = utc_now()
    sid = str(uuid.uuid4())
    sel_status = "AI_SELECTED" if result.get("eligible") else "NOT_SELECTED"
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO marketplace_selections (id,marketplace_id,product_sku,selection_status,approval_status,score,eligible,reasons_json,blockers_json,recommendations_json,criteria_json,selected_by,selected_at,created_at,updated_at)
                VALUES ($1,$2,$3,$4,'PENDING',$5,$6,$7::jsonb,$8::jsonb,$9::jsonb,$10::jsonb,$11,NOW(),NOW(),NOW())
                ON CONFLICT(marketplace_id,product_sku) DO UPDATE SET
                  selection_status=EXCLUDED.selection_status, score=EXCLUDED.score, eligible=EXCLUDED.eligible,
                  reasons_json=EXCLUDED.reasons_json, blockers_json=EXCLUDED.blockers_json, recommendations_json=EXCLUDED.recommendations_json,
                  criteria_json=EXCLUDED.criteria_json, selected_by=EXCLUDED.selected_by, selected_at=NOW(), updated_at=NOW()
            """, sid,marketplace_id,product_sku,sel_status,float(result.get("score") or 0),bool(result.get("eligible")), _json_dump(result.get("reasons") or [],[]),_json_dump(result.get("blockers") or [],[]),_json_dump(result.get("recommendations") or [],[]),_json_dump(result.get("criteria") or {},{}), selected_by)
            row = await conn.fetchrow("SELECT * FROM marketplace_selections WHERE marketplace_id=$1 AND product_sku=$2", marketplace_id, product_sku)
        return _mcc_decode(dict(row))
    async with get_connection() as db:
        await db.execute("""
            INSERT INTO marketplace_selections (id,marketplace_id,product_sku,selection_status,approval_status,score,eligible,reasons_json,blockers_json,recommendations_json,criteria_json,selected_by,selected_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(marketplace_id,product_sku) DO UPDATE SET
              selection_status=excluded.selection_status, score=excluded.score, eligible=excluded.eligible,
              reasons_json=excluded.reasons_json, blockers_json=excluded.blockers_json, recommendations_json=excluded.recommendations_json,
              criteria_json=excluded.criteria_json, selected_by=excluded.selected_by, selected_at=excluded.selected_at, updated_at=excluded.updated_at
        """, (sid,marketplace_id,product_sku,sel_status,'PENDING',float(result.get("score") or 0),1 if result.get("eligible") else 0,_json_dump(result.get("reasons") or [],[]),_json_dump(result.get("blockers") or [],[]),_json_dump(result.get("recommendations") or [],[]),_json_dump(result.get("criteria") or {},{}),selected_by,now,now,now))
        await db.commit()
        cur = await db.execute("SELECT * FROM marketplace_selections WHERE marketplace_id=? AND product_sku=?", (marketplace_id,product_sku))
        row = await cur.fetchone(); await cur.close()
        return _mcc_decode(dict(row)) if row else {"id": sid}


async def mcc_list_selections(marketplace_id: str, limit: int = 200) -> list[dict[str, Any]]:
    limit = min(max(limit,1),1000)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM marketplace_selections WHERE marketplace_id=$1 ORDER BY score DESC, updated_at DESC LIMIT $2", marketplace_id, limit)
        return [_mcc_decode(dict(r)) for r in rows]
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM marketplace_selections WHERE marketplace_id=? ORDER BY score DESC, updated_at DESC LIMIT ?", (marketplace_id, limit))
        rows = await cur.fetchall(); await cur.close()
        return [_mcc_decode(dict(r)) for r in rows]


async def mcc_upsert_marketplace_product(marketplace_id: str, product_sku: str, **fields: Any) -> dict[str, Any]:
    now = utc_now()
    pid = str(fields.pop("id", None) or uuid.uuid4())
    existing = await mcc_get_marketplace_product(marketplace_id, product_sku)
    base = existing or {}
    payload = {
        "id": base.get("id") or pid,
        "marketplace_id": marketplace_id,
        "product_sku": product_sku,
        "external_product_id": str(fields.get("external_product_id", base.get("external_product_id", ""))),
        "canonical_product_json": _json_dump(fields.get("canonical_product_json", base.get("canonical_product_json", {})), {}),
        "marketplace_content_json": _json_dump(fields.get("marketplace_content_json", base.get("marketplace_content_json", {})), {}),
        "marketplace_pricing_json": _json_dump(fields.get("marketplace_pricing_json", base.get("marketplace_pricing_json", {})), {}),
        "marketplace_category_id": str(fields.get("marketplace_category_id", base.get("marketplace_category_id", ""))),
        "attribute_mapping_json": _json_dump(fields.get("attribute_mapping_json", base.get("attribute_mapping_json", {})), {}),
        "listing_status": str(fields.get("listing_status", base.get("listing_status", "NOT_LISTED"))),
        "sync_status": str(fields.get("sync_status", base.get("sync_status", "never"))),
        "last_error": str(fields.get("last_error", base.get("last_error", "")))[:2000],
        "last_synced_at": fields.get("last_synced_at", base.get("last_synced_at")),
        "last_exported_at": fields.get("last_exported_at", base.get("last_exported_at")),
        "updated_at": now,
    }
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO marketplace_products (id,marketplace_id,product_sku,external_product_id,canonical_product_json,marketplace_content_json,marketplace_pricing_json,marketplace_category_id,attribute_mapping_json,listing_status,sync_status,last_error,last_synced_at,last_exported_at,created_at,updated_at)
                VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb,$8,$9::jsonb,$10,$11,$12,$13,$14,NOW(),NOW())
                ON CONFLICT(marketplace_id,product_sku) DO UPDATE SET
                  external_product_id=EXCLUDED.external_product_id, canonical_product_json=EXCLUDED.canonical_product_json, marketplace_content_json=EXCLUDED.marketplace_content_json,
                  marketplace_pricing_json=EXCLUDED.marketplace_pricing_json, marketplace_category_id=EXCLUDED.marketplace_category_id, attribute_mapping_json=EXCLUDED.attribute_mapping_json,
                  listing_status=EXCLUDED.listing_status, sync_status=EXCLUDED.sync_status, last_error=EXCLUDED.last_error,
                  last_synced_at=COALESCE(EXCLUDED.last_synced_at, marketplace_products.last_synced_at), last_exported_at=COALESCE(EXCLUDED.last_exported_at, marketplace_products.last_exported_at), updated_at=NOW()
            """, payload["id"],payload["marketplace_id"],payload["product_sku"],payload["external_product_id"],payload["canonical_product_json"],payload["marketplace_content_json"],payload["marketplace_pricing_json"],payload["marketplace_category_id"],payload["attribute_mapping_json"],payload["listing_status"],payload["sync_status"],payload["last_error"],payload["last_synced_at"],payload["last_exported_at"])
            row = await conn.fetchrow("SELECT * FROM marketplace_products WHERE marketplace_id=$1 AND product_sku=$2", marketplace_id, product_sku)
        return _mcc_decode(dict(row))
    async with get_connection() as db:
        await db.execute("""
            INSERT INTO marketplace_products (id,marketplace_id,product_sku,external_product_id,canonical_product_json,marketplace_content_json,marketplace_pricing_json,marketplace_category_id,attribute_mapping_json,listing_status,sync_status,last_error,last_synced_at,last_exported_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(marketplace_id,product_sku) DO UPDATE SET
              external_product_id=excluded.external_product_id, canonical_product_json=excluded.canonical_product_json, marketplace_content_json=excluded.marketplace_content_json,
              marketplace_pricing_json=excluded.marketplace_pricing_json, marketplace_category_id=excluded.marketplace_category_id, attribute_mapping_json=excluded.attribute_mapping_json,
              listing_status=excluded.listing_status, sync_status=excluded.sync_status, last_error=excluded.last_error,
              last_synced_at=COALESCE(excluded.last_synced_at, marketplace_products.last_synced_at), last_exported_at=COALESCE(excluded.last_exported_at, marketplace_products.last_exported_at), updated_at=excluded.updated_at
        """, (payload["id"],payload["marketplace_id"],payload["product_sku"],payload["external_product_id"],payload["canonical_product_json"],payload["marketplace_content_json"],payload["marketplace_pricing_json"],payload["marketplace_category_id"],payload["attribute_mapping_json"],payload["listing_status"],payload["sync_status"],payload["last_error"],payload["last_synced_at"],payload["last_exported_at"],now,now))
        await db.commit()
        cur = await db.execute("SELECT * FROM marketplace_products WHERE marketplace_id=? AND product_sku=?", (marketplace_id, product_sku))
        row = await cur.fetchone(); await cur.close()
        return _mcc_decode(dict(row)) if row else payload


async def mcc_get_marketplace_product(marketplace_id: str, product_sku: str) -> dict[str, Any] | None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM marketplace_products WHERE marketplace_id=$1 AND product_sku=$2", marketplace_id, product_sku)
        return _mcc_decode(dict(row)) if row else None
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM marketplace_products WHERE marketplace_id=? AND product_sku=?", (marketplace_id, product_sku))
        row = await cur.fetchone(); await cur.close()
        return _mcc_decode(dict(row)) if row else None


async def mcc_list_marketplace_products(marketplace_id: str, limit: int = 500) -> list[dict[str, Any]]:
    limit=min(max(limit,1),2000)
    if uses_postgres():
        pool=await get_pg_pool()
        async with pool.acquire() as conn:
            rows=await conn.fetch("SELECT * FROM marketplace_products WHERE marketplace_id=$1 ORDER BY updated_at DESC LIMIT $2", marketplace_id, limit)
        return [_mcc_decode(dict(r)) for r in rows]
    async with get_connection() as db:
        cur=await db.execute("SELECT * FROM marketplace_products WHERE marketplace_id=? ORDER BY updated_at DESC LIMIT ?", (marketplace_id, limit))
        rows=await cur.fetchall(); await cur.close()
        return [_mcc_decode(dict(r)) for r in rows]


async def mcc_create_export_job(marketplace_id: str, mode: str, requested_by: str, payload: dict[str, Any]) -> dict[str, Any]:
    now=utc_now(); jid=str(uuid.uuid4())
    if uses_postgres():
        pool=await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO marketplace_export_jobs (id,marketplace_id,mode,status,requested_by,payload_json,created_at,updated_at) VALUES ($1,$2,$3,'pending',$4,$5::jsonb,NOW(),NOW())", jid,marketplace_id,mode,requested_by,_json_dump(payload,{}))
            row=await conn.fetchrow("SELECT * FROM marketplace_export_jobs WHERE id=$1", jid)
        return _mcc_decode(dict(row))
    async with get_connection() as db:
        await db.execute("INSERT INTO marketplace_export_jobs (id,marketplace_id,mode,status,requested_by,payload_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (jid,marketplace_id,mode,'pending',requested_by,_json_dump(payload,{}),now,now))
        await db.commit(); cur=await db.execute("SELECT * FROM marketplace_export_jobs WHERE id=?", (jid,)); row=await cur.fetchone(); await cur.close()
        return _mcc_decode(dict(row)) if row else {"id":jid}


_MCC_EXPORT_JOB_ALLOWED_FIELDS = {
    "status","requested_by","approved_by","approved_at","total_items","success_items","failed_items",
    "skipped_items","payload_json","result_json","error_message","started_at","finished_at","updated_at",
}

async def mcc_update_export_job(job_id: str, **fields: Any) -> dict[str, Any] | None:
    if not fields:
        return await mcc_get_export_job(job_id)
    now = utc_now()
    fields["updated_at"] = now
    safe_fields = {k: v for k, v in fields.items() if k in _MCC_EXPORT_JOB_ALLOWED_FIELDS}
    if not safe_fields:
        return await mcc_get_export_job(job_id)
    if uses_postgres():
        sets=[]; vals=[]; i=1
        for k,v in safe_fields.items():
            if k in {"payload_json","result_json"}:
                sets.append(f"{k}=${i}::jsonb"); vals.append(_json_dump(v,{}))
            else:
                sets.append(f"{k}=${i}"); vals.append(v)
            i+=1
        vals.append(job_id)
        pool=await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(f"UPDATE marketplace_export_jobs SET {', '.join(sets)} WHERE id=${i}", *vals)
            row=await conn.fetchrow("SELECT * FROM marketplace_export_jobs WHERE id=$1", job_id)
        return _mcc_decode(dict(row)) if row else None
    sets=[]; vals=[]
    for k,v in safe_fields.items():
        if k in {"payload_json","result_json"}:
            v=_json_dump(v,{})
        sets.append(f"{k}=?"); vals.append(v)
    vals.append(job_id)
    async with get_connection() as db:
        await db.execute(f"UPDATE marketplace_export_jobs SET {', '.join(sets)} WHERE id=?", tuple(vals))
        await db.commit()
        cur=await db.execute("SELECT * FROM marketplace_export_jobs WHERE id=?", (job_id,))
        row=await cur.fetchone(); await cur.close()
        return _mcc_decode(dict(row)) if row else None


async def mcc_get_export_job(job_id: str) -> dict[str, Any] | None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM marketplace_export_jobs WHERE id=$1", job_id)
        return _mcc_decode(dict(row)) if row else None
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM marketplace_export_jobs WHERE id=?", (job_id,))
        row = await cur.fetchone()
        await cur.close()
        return _mcc_decode(dict(row)) if row else None


async def mcc_list_export_jobs(marketplace_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    limit=min(max(limit,1),500)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM marketplace_export_jobs WHERE ($1::text IS NULL OR marketplace_id=$1) ORDER BY created_at DESC LIMIT $2", marketplace_id, limit)
        return [_mcc_decode(dict(r)) for r in rows]
    async with get_connection() as db:
        if marketplace_id:
            cur=await db.execute("SELECT * FROM marketplace_export_jobs WHERE marketplace_id=? ORDER BY created_at DESC LIMIT ?", (marketplace_id,limit))
        else:
            cur=await db.execute("SELECT * FROM marketplace_export_jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        rows=await cur.fetchall(); await cur.close(); return [_mcc_decode(dict(r)) for r in rows]


async def mcc_add_export_item(job_id: str, marketplace_id: str, product_sku: str, **fields: Any) -> dict[str, Any]:
    now=utc_now(); iid=str(uuid.uuid4())
    payload={
        "id":iid,"job_id":job_id,"marketplace_id":marketplace_id,"product_sku":product_sku,
        "action":str(fields.get("action","export")),"status":str(fields.get("status","pending")),
        "external_id":str(fields.get("external_id","")),"request_json":_json_dump(fields.get("request_json",{}),{}),
        "response_json":_json_dump(fields.get("response_json",{}),{}),"error_message":str(fields.get("error_message",""))[:2000],
        "idempotency_key":str(fields.get("idempotency_key","")), "started_at":fields.get("started_at"), "finished_at":fields.get("finished_at"),
        "created_at":now,"updated_at":now,
    }
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""INSERT INTO marketplace_export_items (id,job_id,marketplace_id,product_sku,action,status,external_id,request_json,response_json,error_message,idempotency_key,started_at,finished_at,created_at,updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10,$11,$12,$13,NOW(),NOW())""",
                payload["id"],payload["job_id"],payload["marketplace_id"],payload["product_sku"],payload["action"],payload["status"],payload["external_id"],payload["request_json"],payload["response_json"],payload["error_message"],payload["idempotency_key"],payload["started_at"],payload["finished_at"])
            row=await conn.fetchrow("SELECT * FROM marketplace_export_items WHERE id=$1", iid)
        return _mcc_decode(dict(row))
    async with get_connection() as db:
        await db.execute("""INSERT INTO marketplace_export_items (id,job_id,marketplace_id,product_sku,action,status,external_id,request_json,response_json,error_message,idempotency_key,started_at,finished_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (payload["id"],payload["job_id"],payload["marketplace_id"],payload["product_sku"],payload["action"],payload["status"],payload["external_id"],payload["request_json"],payload["response_json"],payload["error_message"],payload["idempotency_key"],payload["started_at"],payload["finished_at"],now,now))
        await db.commit(); cur=await db.execute("SELECT * FROM marketplace_export_items WHERE id=?", (iid,)); row=await cur.fetchone(); await cur.close(); return _mcc_decode(dict(row)) if row else payload


async def mcc_list_export_items(job_id: str, limit: int = 500) -> list[dict[str, Any]]:
    limit=min(max(limit,1),2000)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows=await conn.fetch("SELECT * FROM marketplace_export_items WHERE job_id=$1 ORDER BY created_at ASC LIMIT $2", job_id, limit)
        return [_mcc_decode(dict(r)) for r in rows]
    async with get_connection() as db:
        cur=await db.execute("SELECT * FROM marketplace_export_items WHERE job_id=? ORDER BY created_at ASC LIMIT ?", (job_id,limit)); rows=await cur.fetchall(); await cur.close(); return [_mcc_decode(dict(r)) for r in rows]


async def mcc_audit(action: str, *, marketplace_id: str = "", product_sku: str = "", job_id: str = "", actor: str = "system", severity: str = "info", message: str = "", details: dict[str, Any] | None = None) -> None:
    now=utc_now(); aid=str(uuid.uuid4())
    details_json=_json_dump(details or {}, {})
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO marketplace_audit_events (id,marketplace_id,product_sku,job_id,actor,action,severity,message,details_json,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,NOW())", aid,marketplace_id,product_sku,job_id,actor,action,severity,message[:2000],details_json)
        return
    async with get_connection() as db:
        await db.execute("INSERT INTO marketplace_audit_events (id,marketplace_id,product_sku,job_id,actor,action,severity,message,details_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (aid,marketplace_id,product_sku,job_id,actor,action,severity,message[:2000],details_json,now))
        await db.commit()


async def mcc_list_audit(marketplace_id: str = "", product_sku: str = "", limit: int = 200) -> list[dict[str, Any]]:
    limit=min(max(limit,1),1000)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows=await conn.fetch("""SELECT * FROM marketplace_audit_events WHERE ($1='' OR marketplace_id=$1) AND ($2='' OR product_sku=$2) ORDER BY created_at DESC LIMIT $3""", marketplace_id, product_sku, limit)
        return [_mcc_decode(dict(r)) for r in rows]
    async with get_connection() as db:
        cur=await db.execute("""SELECT * FROM marketplace_audit_events WHERE (?='' OR marketplace_id=?) AND (?='' OR product_sku=?) ORDER BY created_at DESC LIMIT ?""", (marketplace_id,marketplace_id,product_sku,product_sku,limit))
        rows=await cur.fetchall(); await cur.close(); return [_mcc_decode(dict(r)) for r in rows]


async def mcc_overview() -> dict[str, Any]:
    mps = await mcc_list_marketplaces()
    jobs = await mcc_list_export_jobs(limit=200)
    # aggregate selection/listing counts
    sel_total=0; listed=0; errors=0; pending=0
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            r1=await conn.fetchrow("SELECT COUNT(*) c FROM marketplace_selections WHERE selection_status='AI_SELECTED'")
            r2=await conn.fetchrow("SELECT COUNT(*) c FROM marketplace_products WHERE listing_status='ONLINE'")
            r3=await conn.fetchrow("SELECT COUNT(*) c FROM marketplace_products WHERE listing_status='ERROR'")
            r4=await conn.fetchrow("SELECT COUNT(*) c FROM marketplace_export_jobs WHERE status IN ('pending','running')")
        sel_total=int((r1 or {}).get('c',0)); listed=int((r2 or {}).get('c',0)); errors=int((r3 or {}).get('c',0)); pending=int((r4 or {}).get('c',0))
    else:
        async with get_connection() as db:
            async def c(q,a=()):
                cur=await db.execute(q,a); row=await cur.fetchone(); await cur.close(); return int(list(row.values())[0]) if row else 0
            sel_total=await c("SELECT COUNT(*) c FROM marketplace_selections WHERE selection_status='AI_SELECTED'")
            listed=await c("SELECT COUNT(*) c FROM marketplace_products WHERE listing_status='ONLINE'")
            errors=await c("SELECT COUNT(*) c FROM marketplace_products WHERE listing_status='ERROR'")
            pending=await c("SELECT COUNT(*) c FROM marketplace_export_jobs WHERE status IN ('pending','running')")
    return {
        "marketplaces_total": len(mps),
        "marketplaces_active": sum(1 for m in mps if str(m.get("status")) in {"active","connected"}),
        "ai_selected_total": sel_total,
        "listed_online_total": listed,
        "export_errors_total": errors,
        "export_jobs_pending": pending,
        "recent_jobs": jobs[:10],
        "marketplaces": mps,
    }
