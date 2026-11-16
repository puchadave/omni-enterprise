"""CRM & omnichannel communication service.

Responsibilities: customer resolution, thread/message persistence with idempotent
ingestion, appointment management, order/payment linkage, guarded actions, and
event publication hooks.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from database import (
    crm_add_message,
    crm_create_appointment,
    crm_get_customer,
    crm_get_thread,
    crm_list_appointments,
    crm_list_customers,
    crm_list_messages,
    crm_list_threads,
    crm_upsert_customer,
    crm_upsert_thread,
    fetch_order,
    get_setting,
)
from services.crm_models import NormalizedMessage, fulfillment_release_allowed, normalize_message, payment_verified, utc_now_iso

logger = logging.getLogger(__name__)


def _loads_maybe(v: Any, default: Any):
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except Exception:
            return default
    return default


async def resolve_or_create_customer(*, channel: str, normalized: NormalizedMessage) -> dict[str, Any]:
    existing = None
    if normalized.customer_external_id:
        existing = await crm_get_customer(channel=channel, external_id=normalized.customer_external_id)
    if not existing and normalized.customer_email:
        existing = await crm_get_customer(email=normalized.customer_email)
    payload = {
        "id": (existing or {}).get("id"),
        "email": normalized.customer_email or (existing or {}).get("email") or "",
        "display_name": (existing or {}).get("display_name") or normalized.sender or normalized.customer_email or f"{channel} Kunde",
        "shopware_id": normalized.customer_external_id if channel == "shopware" else (existing or {}).get("shopware_id", ""),
        "amazon_id": normalized.customer_external_id if channel == "amazon" else (existing or {}).get("amazon_id", ""),
        "ebay_id": normalized.customer_external_id if channel == "ebay" else (existing or {}).get("ebay_id", ""),
        "phone": (existing or {}).get("phone", ""),
        "metadata_json": _loads_maybe((existing or {}).get("metadata_json"), {}),
    }
    # merge metadata hint
    meta = payload["metadata_json"] if isinstance(payload["metadata_json"], dict) else {}
    meta.setdefault("channels_seen", [])
    if channel not in meta["channels_seen"]:
        meta["channels_seen"].append(channel)
    payload["metadata_json"] = meta
    return await crm_upsert_customer(payload)


async def resolve_or_create_thread(*, customer_id: str, normalized: NormalizedMessage, order_id: str | None = None) -> dict[str, Any]:
    existing = None
    if normalized.external_thread_id:
        existing = await crm_get_thread(channel=normalized.channel, external_thread_id=normalized.external_thread_id)
    subject = normalized.subject or (f"Bestellung {order_id}" if order_id else f"{normalized.channel.title()} Anfrage")
    payload = {
        "id": (existing or {}).get("id"),
        "customer_id": customer_id,
        "channel": normalized.channel,
        "external_thread_id": normalized.external_thread_id or normalized.idempotency_key,
        "subject": (existing or {}).get("subject") or subject,
        "status": (existing or {}).get("status") or "open",
        "last_message_at": normalized.sent_at or utc_now_iso(),
        "order_id": order_id or (existing or {}).get("order_id") or "",
        "metadata_json": _loads_maybe((existing or {}).get("metadata_json"), {}),
    }
    return await crm_upsert_thread(payload)


async def ingest_normalized_message(normalized: NormalizedMessage, *, auto_events: bool = True) -> dict[str, Any]:
    normalized.validate()
    customer = await resolve_or_create_customer(channel=normalized.channel, normalized=normalized)
    # try resolve linked order
    order = None
    order_id = normalized.order_external_id or ""
    if order_id:
        order = await fetch_order(order_id)
        if not order:
            # keep external ref in thread even if local order not found
            pass
    thread = await resolve_or_create_thread(customer_id=str(customer["id"]), normalized=normalized, order_id=(order or {}).get("order_id") or order_id or None)
    rec = normalized.to_record()
    rec.update({"thread_id": thread["id"], "customer_id": customer["id"]})
    saved, created = await crm_add_message(rec)
    result = {
        "status": "created" if created else "duplicate",
        "idempotent_replay": not created,
        "customer": customer,
        "thread": thread,
        "message": saved,
        "order": await enrich_order_context(thread.get("order_id") or order_id),
    }
    if auto_events and created:
        try:
            from services.event_bus import get_event_bus
            bus = await get_event_bus()
            await bus.publish(event_type="crm.message.received" if normalized.direction == "inbound" else "crm.message.sent", source_module="crm_service", payload={"channel": normalized.channel, "thread_id": thread["id"], "customer_id": customer["id"], "message_id": saved.get("id"), "order_id": thread.get("order_id")}, entity_id=str(saved.get("id") or ""))
        except Exception:
            logger.exception("Failed to publish CRM event")
    return result


async def ingest_channel_payload(channel: str, payload: Mapping[str, Any], *, direction: str = "inbound") -> dict[str, Any]:
    normalized = normalize_message(channel, payload, direction=direction)
    return await ingest_normalized_message(normalized)


async def enrich_order_context(order_id: str | None) -> dict[str, Any] | None:
    if not order_id:
        return None
    order = await fetch_order(str(order_id))
    if not order:
        return {"order_id": order_id, "linked": False, "payment_status": "unknown", "fulfillment_locked": None}
    locked = order.get("fulfillment_locked")
    if isinstance(locked, str):
        locked = locked.lower() in {"1", "true", "yes"}
    return {
        "order_id": order.get("order_id"),
        "sku": order.get("sku"),
        "quantity": order.get("quantity"),
        "marketplace": order.get("marketplace"),
        "status": order.get("status"),
        "payment_status": order.get("payment_status"),
        "fulfillment_locked": bool(locked),
        "payment_verified": payment_verified(order),
        "fulfillment_release_allowed": fulfillment_release_allowed(order),
        "tracking_number": order.get("tracking_number"),
        "tracking_url": order.get("tracking_url"),
        "last_error": order.get("last_error"),
        "linked": True,
    }


async def list_threads_enriched(limit: int = 100, channel: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    threads = await crm_list_threads(limit=limit, channel=channel, status=status)
    out = []
    for t in threads:
        item = dict(t)
        item["order"] = await enrich_order_context(item.get("order_id"))
        msgs = await crm_list_messages(thread_id=str(item.get("id")), limit=1)
        item["last_message"] = msgs[-1] if msgs else None
        out.append(item)
    return out


async def guarded_manual_action(*, action: str, order_id: str, actor: str = "crm_user", payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Security guard: actions requiring verified payment must fail otherwise.

    Applies to manual fulfillment releases/approvals and goodwill credits.
    """
    order = await fetch_order(order_id)
    if not order:
        raise ValueError(f"Bestellung nicht gefunden: {order_id}")
    if action in {"manual_fulfillment_release", "manual_approval", "goodwill_credit", "kulanz_gutschrift"}:
        if not payment_verified(order):
            raise PermissionError("Aktion abgelehnt: Zahlung ist nicht vollständig verifiziert.")
        if action in {"manual_fulfillment_release", "manual_approval"} and bool(order.get("fulfillment_locked")):
            raise PermissionError("Aktion abgelehnt: Fulfillment ist gesperrt (fulfillment_locked).")
    # Record guarded action in audit/event log only (no external side effects fabricated)
    try:
        from services.event_bus import get_event_bus
        bus = await get_event_bus()
        await bus.publish(event_type="crm.action.executed", source_module="crm_service", payload={"action": action, "order_id": order_id, "actor": actor, "request": dict(payload or {})}, entity_id=order_id)
    except Exception:
        pass
    return {"status": "accepted", "action": action, "order_id": order_id, "payment_status": order.get("payment_status"), "fulfillment_locked": bool(order.get("fulfillment_locked")), "message": "Aktion protokolliert. Externe Ausführung erfolgt nur über konfigurierte Systeme."}


async def create_appointment(payload: Mapping[str, Any]) -> dict[str, Any]:
    # ensure linked entities optional but validated if provided
    if payload.get("order_id"):
        ctx = await enrich_order_context(str(payload.get("order_id")))
        if ctx and not ctx.get("linked") and str(payload.get("order_id")):
            # allow storing external/unresolved order ref, but keep normalized
            pass
    appt = await crm_create_appointment(dict(payload))
    try:
        from services.event_bus import get_event_bus
        bus = await get_event_bus()
        await bus.publish(event_type="crm.appointment.created", source_module="crm_service", payload={"appointment_id": appt.get("id"), "customer_id": appt.get("customer_id"), "order_id": appt.get("order_id")}, entity_id=str(appt.get("id") or ""))
    except Exception:
        pass
    return appt


async def get_crm_overview() -> dict[str, Any]:
    threads = await crm_list_threads(limit=200)
    appts = await crm_list_appointments(limit=200)
    customers = await crm_list_customers(limit=200)
    open_threads = sum(1 for t in threads if str(t.get("status")) in {"open", "pending"})
    by_channel: dict[str, int] = {}
    for t in threads:
        by_channel[str(t.get("channel") or "unknown")] = by_channel.get(str(t.get("channel") or "unknown"), 0) + 1
    return {
        "customers": len(customers),
        "threads": len(threads),
        "open_threads": open_threads,
        "appointments": len(appts),
        "channels": by_channel,
        "generated_at": utc_now_iso(),
    }
