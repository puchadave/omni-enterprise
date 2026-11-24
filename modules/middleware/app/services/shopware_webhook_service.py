"""Shopware inbound webhook verification, idempotent inbox, and processing."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any

from database import shopware_webhook_inbox_insert, shopware_webhook_inbox_mark, write_log

logger = logging.getLogger(__name__)


def verify_shopware_signature(*, raw_body: bytes, signature_header: str, secret: str) -> bool:
    if not secret or not signature_header:
        return False
    provided = signature_header.strip()
    if provided.lower().startswith("sha256="):
        provided = provided.split("=", 1)[1].strip()
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided.lower(), expected.lower())


def _extract_secret(cm: Any) -> str:
    for attr in ("webhook_secret", "shared_secret", "client_secret"):
        val = getattr(cm, attr, None)
        if isinstance(val, str) and val.strip():
            return val.strip()
    cfg = getattr(cm, "config", None)
    if isinstance(cfg, dict):
        for k in ("webhook_secret", "shared_secret", "shopware_webhook_secret"):
            v = cfg.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return os.getenv("SHOPWARE_WEBHOOK_SECRET", "").strip()


async def process_shopware_webhook(*, runtime: Any, connection_id: str, raw_body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
    cm = (getattr(runtime, "shopware_connections", {}) or {}).get(connection_id)
    if not cm:
        return 404, {"status": "error", "error": "unknown_connection"}

    event_name = (headers.get("x-omni-event") or headers.get("x-shopware-event") or "").strip()
    delivery_id = (headers.get("x-omni-delivery-id") or headers.get("x-shopware-delivery-id") or "").strip()
    idem = (headers.get("x-omni-idempotency-key") or headers.get("x-shopware-idempotency-key") or "").strip()
    signature = (headers.get("x-omni-signature") or headers.get("x-shopware-shop-signature") or headers.get("shopware-shop-signature") or "").strip()
    corr = (headers.get("x-correlation-id") or headers.get("x-request-id") or delivery_id or idem).strip()

    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            return 400, {"status": "error", "error": "payload_must_be_object"}
    except Exception:
        return 400, {"status": "error", "error": "invalid_json"}

    event_name = event_name or str(payload.get("event") or payload.get("event_name") or "shopware.unknown")
    source = str(payload.get("source") or getattr(cm, "shop_name", "") or connection_id)
    if not idem:
        base = f"{connection_id}|{event_name}|{delivery_id}|{hashlib.sha256(raw_body).hexdigest()}"
        idem = hashlib.sha256(base.encode("utf-8")).hexdigest()

    secret = _extract_secret(cm)
    sig_ok = verify_shopware_signature(raw_body=raw_body, signature_header=signature, secret=secret)
    if secret and not sig_ok:
        await write_log("WARN", "shopware_webhook_signature_invalid", "Invalid Shopware webhook signature", {"connection_id": connection_id, "event": event_name, "correlation_id": corr})
        return 401, {"status": "error", "error": "invalid_signature", "correlation_id": corr}

    stored_headers = {k.lower(): v for k, v in headers.items() if k.lower().startswith("x-omni") or k.lower().startswith("x-shopware") or k.lower() in {"x-correlation-id", "x-request-id", "content-type"}}
    row = await shopware_webhook_inbox_insert(
        connection_id=connection_id,
        event_name=event_name,
        source=source,
        delivery_id=delivery_id,
        idempotency_key=idem,
        signature_valid=bool(sig_ok or not secret),
        payload=payload,
        headers=stored_headers,
        correlation_id=corr,
    )
    if row.get("duplicate"):
        await write_log("INFO", "shopware_webhook_duplicate", "Duplicate Shopware webhook ignored", {"connection_id": connection_id, "event": event_name, "idempotency_key": idem, "correlation_id": corr})
        return 200, {"status": "duplicate", "idempotency_key": idem, "correlation_id": corr}

    inbox_id = str((row.get("row") or {}).get("id") or "")
    try:
        await shopware_webhook_inbox_mark(inbox_id, status="processing")
        result = await _dispatch_event(runtime=runtime, connection_id=connection_id, event_name=event_name, payload=payload, correlation_id=corr)
        await shopware_webhook_inbox_mark(inbox_id, status="processed")
        await write_log("INFO", "shopware_webhook_processed", "Shopware webhook processed", {"connection_id": connection_id, "event": event_name, "idempotency_key": idem, "correlation_id": corr, "result": result})
        return 202, {"status": "accepted", "idempotency_key": idem, "correlation_id": corr, "result": result}
    except Exception as e:
        logger.exception("Shopware webhook processing failed")
        await shopware_webhook_inbox_mark(inbox_id, status="failed", error_message=str(e))
        await write_log("ERROR", "shopware_webhook_failed", "Shopware webhook processing failed", {"connection_id": connection_id, "event": event_name, "idempotency_key": idem, "correlation_id": corr, "error": str(e)[:500]})
        return 500, {"status": "error", "error": "processing_failed", "idempotency_key": idem, "correlation_id": corr}


async def _dispatch_event(*, runtime: Any, connection_id: str, event_name: str, payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    name = (event_name or "").lower()
    body = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    if "order" in name:
        try:
            from services.crm_adapters import ingest_webhook
            res = await ingest_webhook("shopware", {"event": event_name, "connection_id": connection_id, "correlation_id": correlation_id, **body}, direction="inbound")
            return {"handler": "crm_ingest", "result": res}
        except Exception as e:
            return {"handler": "crm_ingest", "error": str(e)[:300]}
    if "product" in name:
        # No destructive sync; record/audit only until field mapping contract is finalized.
        return {"handler": "audit_only", "reason": "product_event_mapping_not_finalized"}
    return {"handler": "ignored", "reason": "unsupported_event"}
