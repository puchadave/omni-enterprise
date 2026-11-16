"""Channel sync adapters for omnichannel CRM (Shopware, Amazon, eBay, Email, Messenger).

Adapters fetch/receive messages asynchronously (polling or webhook ingestion),
normalize them, and delegate to CRM service ingestion which is idempotent by
(external_message_id/channel) or computed hash fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from database import get_setting
from services.crm_service import ingest_channel_payload

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    channel: str
    status: str
    fetched: int = 0
    ingested: int = 0
    duplicates: int = 0
    errors: list[str] | None = None
    configured: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "status": self.status,
            "fetched": self.fetched,
            "ingested": self.ingested,
            "duplicates": self.duplicates,
            "errors": self.errors or [],
            "configured": self.configured,
            "message": self.message,
        }


class BaseCRMAdapter:
    channel: str = "base"

    async def is_configured(self) -> bool:
        return False

    async def fetch_messages(self) -> list[dict[str, Any]]:
        """Return raw channel messages. Polling hook; override per adapter."""
        return []

    async def sync(self) -> SyncResult:
        if not await self.is_configured():
            return SyncResult(channel=self.channel, status="not_configured", configured=False, message="Kanal nicht konfiguriert. Zugangsdaten/Verbindung in Einstellungen hinterlegen, um Sync zu aktivieren.")
        raw = await self.fetch_messages()
        ingested = 0
        duplicates = 0
        errors: list[str] = []
        for item in raw:
            try:
                res = await ingest_channel_payload(self.channel, item, direction=str(item.get("direction") or "inbound"))
                if res.get("idempotent_replay"):
                    duplicates += 1
                else:
                    ingested += 1
            except Exception as e:
                errors.append(str(e))
        status = "ok" if not errors else "partial"
        return SyncResult(channel=self.channel, status=status, fetched=len(raw), ingested=ingested, duplicates=duplicates, errors=errors, configured=True, message="Sync abgeschlossen." if not errors else "Sync mit Fehlern abgeschlossen.")


class ShopwareCRMAdapter(BaseCRMAdapter):
    channel = "shopware"

    async def is_configured(self) -> bool:
        # reuse existing shopware settings/connection indicators
        url = await get_setting("shopware_url") or await get_setting("shopware_base_url")
        token = await get_setting("shopware_access_token") or await get_setting("shopware_api_key")
        return bool(url and token)

    async def fetch_messages(self) -> list[dict[str, Any]]:
        # No fabricated API calls without verified endpoint contract/creds; webhook/push ingestion supported.
        return []


class AmazonCRMAdapter(BaseCRMAdapter):
    channel = "amazon"

    async def is_configured(self) -> bool:
        seller = await get_setting("amazon_seller_id")
        token = await get_setting("amazon_sp_api_refresh_token") or await get_setting("amazon_lwa_refresh_token")
        return bool(seller and token)

    async def fetch_messages(self) -> list[dict[str, Any]]:
        # SP-API Buyer Messaging polling requires authorized app/roles; not fabricated.
        return []


class EbayCRMAdapter(BaseCRMAdapter):
    channel = "ebay"

    async def is_configured(self) -> bool:
        token = await get_setting("ebay_oauth_token") or await get_setting("ebay_user_token")
        return bool(token)

    async def fetch_messages(self) -> list[dict[str, Any]]:
        # eBay Messaging/Negotiation APIs require scoped tokens; not fabricated.
        return []


class EmailCRMAdapter(BaseCRMAdapter):
    channel = "email"

    async def is_configured(self) -> bool:
        host = await get_setting("email_imap_host") or await get_setting("smtp_host")
        user = await get_setting("email_username") or await get_setting("smtp_user")
        return bool(host and user)

    async def fetch_messages(self) -> list[dict[str, Any]]:
        return []


class MessengerCRMAdapter(BaseCRMAdapter):
    channel = "messenger"

    async def is_configured(self) -> bool:
        token = await get_setting("messenger_page_token") or await get_setting("messenger_access_token")
        return bool(token)

    async def fetch_messages(self) -> list[dict[str, Any]]:
        return []


ADAPTERS: dict[str, BaseCRMAdapter] = {
    "shopware": ShopwareCRMAdapter(),
    "amazon": AmazonCRMAdapter(),
    "ebay": EbayCRMAdapter(),
    "email": EmailCRMAdapter(),
    "messenger": MessengerCRMAdapter(),
}


async def sync_channel(channel: str) -> dict[str, Any]:
    adapter = ADAPTERS.get(channel)
    if not adapter:
        return {"channel": channel, "status": "unknown_channel", "message": "Unbekannter Kanal."}
    return (await adapter.sync()).to_dict()


async def sync_all_channels() -> dict[str, Any]:
    results = [await sync_channel(ch) for ch in ADAPTERS]
    return {"status": "ok", "results": results}


async def ingest_webhook(channel: str, payload: Mapping[str, Any] | list[Any], *, direction: str = "inbound") -> dict[str, Any]:
    items: Iterable[Mapping[str, Any]]
    if isinstance(payload, list):
        items = [x for x in payload if isinstance(x, Mapping)]
    elif isinstance(payload, Mapping) and isinstance(payload.get("messages"), list):
        items = [x for x in payload.get("messages", []) if isinstance(x, Mapping)]
    else:
        items = [payload] if isinstance(payload, Mapping) else []
    ingested = 0
    duplicates = 0
    errors: list[str] = []
    details = []
    for item in items:
        try:
            res = await ingest_channel_payload(channel, item, direction=direction)
            details.append({"status": res.get("status"), "message_id": (res.get("message") or {}).get("id"), "thread_id": (res.get("thread") or {}).get("id")})
            if res.get("idempotent_replay"):
                duplicates += 1
            else:
                ingested += 1
        except Exception as e:
            errors.append(str(e))
    return {"channel": channel, "status": "ok" if not errors else "partial", "received": len(list(items)) if not isinstance(items, list) else len(items), "ingested": ingested, "duplicates": duplicates, "errors": errors, "details": details[:50]}
