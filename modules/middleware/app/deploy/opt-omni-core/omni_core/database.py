"""SQLite persistence layer for Omni-Core Middleware.

The application deliberately uses SQLite in WAL mode and short-lived connections.
All multi-row inventory changes are performed with ``BEGIN IMMEDIATE`` so that an
order cannot oversell a product or bundle when requests arrive concurrently.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

import aiosqlite

DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "middleware.db"))
SQLITE_BUSY_TIMEOUT_MS = 5_000


class DatabaseError(RuntimeError):
    """Raised when a persistent state transition cannot be completed."""


class InsufficientStockError(DatabaseError):
    """Raised when an order cannot be reserved without overselling."""


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp suitable for SQLite TEXT columns."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _connect() -> aiosqlite.Connection:
    """Open and configure one SQLite connection."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(DATABASE_PATH, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = aiosqlite.Row
    await connection.execute("PRAGMA foreign_keys = ON")
    await connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return connection


@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """Yield a configured connection and always close it."""
    connection = await _connect()
    try:
        yield connection
    finally:
        await connection.close()


async def init_db() -> None:
    """Create the schema and activate SQLite WAL durability settings."""
    async with get_db() as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA synchronous = NORMAL")
        await db.execute("PRAGMA wal_autocheckpoint = 1000")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                is_secret INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0, 1)),
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS products (
                sku TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                raw_description TEXT NOT NULL DEFAULT '',
                ai_title TEXT NOT NULL DEFAULT '',
                ai_description_html TEXT NOT NULL DEFAULT '',
                ai_tags_json TEXT NOT NULL DEFAULT '[]',
                supplier_price_cents INTEGER NOT NULL CHECK (supplier_price_cents >= 0),
                stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
                is_bundle INTEGER NOT NULL DEFAULT 0 CHECK (is_bundle IN (0, 1)),
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('draft', 'active', 'paused', 'archived')),
                source_hash TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_products_status_stock
                ON products(status, stock, is_bundle);

            CREATE TABLE IF NOT EXISTS bundles (
                bundle_sku TEXT PRIMARY KEY,
                component_skus_json TEXT NOT NULL,
                title TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (bundle_sku) REFERENCES products(sku) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                sku TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                marketplace TEXT NOT NULL,
                customer_reference TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL
                    CHECK (status IN ('received', 'reserved', 'dispatching', 'dispatched', 'failed', 'cancelled')),
                tracking_number TEXT NOT NULL DEFAULT '',
                tracking_url TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (sku) REFERENCES products(sku)
            );
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, updated_at);

            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR')),
                event TEXT NOT NULL,
                message TEXT NOT NULL,
                context_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_logs_created_at ON logs(created_at DESC);
            """
        )
        await db.commit()


async def set_setting(key: str, value: str, *, is_secret: bool = False) -> None:
    """Atomically upsert a non-empty configuration setting."""
    if not key or not key.replace("_", "").isalnum():
        raise ValueError("Setting keys may only contain letters, digits, and underscores.")
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO settings(key, value, is_secret, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                is_secret = excluded.is_secret,
                updated_at = excluded.updated_at
            """,
            (key, value, int(is_secret), utc_now()),
        )
        await db.commit()


async def get_setting(key: str) -> str | None:
    """Get the raw configured value for trusted server-side service code."""
    async with get_db() as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
    return str(row["value"]) if row else None


async def list_settings(*, reveal_secrets: bool = False) -> dict[str, str]:
    """List settings; secrets are masked unless explicitly requested internally."""
    async with get_db() as db:
        cursor = await db.execute("SELECT key, value, is_secret FROM settings ORDER BY key")
        rows = await cursor.fetchall()
        await cursor.close()
    return {
        str(row["key"]): str(row["value"]) if reveal_secrets or not row["is_secret"] else ""
        for row in rows
    }


async def write_log(level: str, event: str, message: str, context: Mapping[str, Any] | None = None) -> None:
    """Store a bounded, structured operational log record without secrets."""
    payload = json.dumps(context or {}, ensure_ascii=False, separators=(",", ":"))[:4_096]
    async with get_db() as db:
        await db.execute(
            "INSERT INTO logs(level, event, message, context_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (level.upper(), event, message[:2_000], payload, utc_now()),
        )
        await db.commit()


async def recent_logs(limit: int = 50) -> list[dict[str, Any]]:
    """Return newest operational records for the authenticated dashboard."""
    safe_limit = min(max(limit, 1), 200)
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, level, event, message, context_json, created_at FROM logs ORDER BY id DESC LIMIT ?",
            (safe_limit,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [dict(row) for row in rows]


async def upsert_product(
    *,
    sku: str,
    title: str,
    category: str,
    supplier_price_cents: int,
    stock: int,
    source_hash: str,
    raw_description: str = "",
) -> bool:
    """Idempotently persist a supplier product. Return true if state changed."""
    if supplier_price_cents < 0 or stock < 0:
        raise ValueError("Price and stock must not be negative.")
    now = utc_now()
    async with get_db() as db:
        cursor = await db.execute("SELECT source_hash FROM products WHERE sku = ?", (sku,))
        current = await cursor.fetchone()
        await cursor.close()
        if current and current["source_hash"] == source_hash:
            return False
        await db.execute(
            """
            INSERT INTO products(
                sku, title, category, raw_description, supplier_price_cents, stock,
                is_bundle, status, source_hash, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 'active', ?, ?)
            ON CONFLICT(sku) DO UPDATE SET
                title = excluded.title,
                category = excluded.category,
                raw_description = excluded.raw_description,
                supplier_price_cents = excluded.supplier_price_cents,
                stock = excluded.stock,
                status = CASE WHEN products.is_bundle = 1 THEN products.status ELSE 'active' END,
                source_hash = excluded.source_hash,
                updated_at = excluded.updated_at
            """,
            (sku, title, category, raw_description, supplier_price_cents, stock, source_hash, now),
        )
        await db.commit()
    return True


async def refresh_bundle_stock(db: aiosqlite.Connection, bundle_sku: str) -> tuple[int, str]:
    """Set bundle stock to min(component stocks), pausing zero-stock bundles.

    The caller must hold an active write transaction.
    """
    cursor = await db.execute("SELECT component_skus_json FROM bundles WHERE bundle_sku = ?", (bundle_sku,))
    bundle = await cursor.fetchone()
    await cursor.close()
    if not bundle:
        raise DatabaseError(f"Bundle {bundle_sku!r} does not exist.")
    component_skus = json.loads(str(bundle["component_skus_json"]))
    if not isinstance(component_skus, list) or not component_skus:
        stock, status = 0, "paused"
    else:
        placeholders = ",".join("?" for _ in component_skus)
        cursor = await db.execute(
            f"SELECT sku, stock FROM products WHERE sku IN ({placeholders})", tuple(component_skus)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        stocks = {str(row["sku"]): int(row["stock"]) for row in rows}
        stock = min((stocks.get(str(sku), 0) for sku in component_skus), default=0)
        status = "active" if stock > 0 else "paused"
    await db.execute(
        "UPDATE products SET stock = ?, status = ?, updated_at = ? WHERE sku = ?",
        (stock, status, utc_now(), bundle_sku),
    )
    return stock, status


async def refresh_all_bundle_stocks() -> None:
    """Recalculate every bundle after an inventory ingest or reservation."""
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT bundle_sku FROM bundles")
            bundle_skus = [str(row["bundle_sku"]) for row in await cursor.fetchall()]
            await cursor.close()
            for bundle_sku in bundle_skus:
                await refresh_bundle_stock(db, bundle_sku)
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def reserve_order_stock(order_id: str, sku: str, quantity: int) -> None:
    """Atomically reserve product or bundle components for one idempotent order."""
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,))
            existing = await cursor.fetchone()
            await cursor.close()
            if existing and str(existing["status"]) in {"reserved", "dispatching", "dispatched"}:
                await db.commit()
                return

            cursor = await db.execute(
                "SELECT sku, stock, is_bundle, status FROM products WHERE sku = ?", (sku,)
            )
            product = await cursor.fetchone()
            await cursor.close()
            if not product or product["status"] != "active" or int(product["stock"]) < quantity:
                raise InsufficientStockError(f"Insufficient active stock for {sku}.")

            if int(product["is_bundle"]):
                cursor = await db.execute("SELECT component_skus_json FROM bundles WHERE bundle_sku = ?", (sku,))
                bundle = await cursor.fetchone()
                await cursor.close()
                if not bundle:
                    raise DatabaseError(f"Bundle metadata missing for {sku}.")
                component_skus = [str(item) for item in json.loads(str(bundle["component_skus_json"]))]
                for component_sku in component_skus:
                    cursor = await db.execute(
                        "UPDATE products SET stock = stock - ?, updated_at = ? "
                        "WHERE sku = ? AND is_bundle = 0 AND status = 'active' AND stock >= ?",
                        (quantity, utc_now(), component_sku, quantity),
                    )
                    changed = cursor.rowcount
                    await cursor.close()
                    if changed != 1:
                        raise InsufficientStockError(f"Insufficient component stock for {component_sku}.")
            else:
                cursor = await db.execute(
                    "UPDATE products SET stock = stock - ?, updated_at = ? "
                    "WHERE sku = ? AND status = 'active' AND stock >= ?",
                    (quantity, utc_now(), sku, quantity),
                )
                changed = cursor.rowcount
                await cursor.close()
                if changed != 1:
                    raise InsufficientStockError(f"Insufficient stock for {sku}.")

            cursor = await db.execute("SELECT bundle_sku FROM bundles")
            bundle_skus = [str(row["bundle_sku"]) for row in await cursor.fetchall()]
            await cursor.close()
            for bundle_sku in bundle_skus:
                await refresh_bundle_stock(db, bundle_sku)
            await db.execute(
                "UPDATE orders SET status = 'reserved', updated_at = ?, last_error = '' WHERE order_id = ?",
                (utc_now(), order_id),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def fetch_product(sku: str) -> dict[str, Any] | None:
    """Fetch one product for API responses."""
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM products WHERE sku = ?", (sku,))
        row = await cursor.fetchone()
        await cursor.close()
    return dict(row) if row else None


async def list_products(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """List products with bounded pagination."""
    safe_limit, safe_offset = min(max(limit, 1), 500), max(offset, 0)
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM products ORDER BY updated_at DESC, sku ASC LIMIT ? OFFSET ?",
            (safe_limit, safe_offset),
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [dict(row) for row in rows]


async def count_dashboard_metrics() -> dict[str, int]:
    """Return compact dashboard counters in one connection."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM products) AS product_count,
                (SELECT COUNT(*) FROM products WHERE status = 'active') AS active_product_count,
                (SELECT COUNT(*) FROM orders WHERE status IN ('received', 'reserved', 'dispatching')) AS pending_order_count,
                (SELECT COUNT(*) FROM logs WHERE level = 'ERROR' AND created_at >= datetime('now', '-24 hours')) AS error_count
            """
        )
        row = await cursor.fetchone()
        await cursor.close()
    return {key: int(row[key]) for key in row.keys()} if row else {}
