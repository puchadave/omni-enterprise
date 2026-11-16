"""Unified CRM & communication data models and normalization helpers.

Channels: shopware, amazon, ebay, email, messenger.
All inbound/outbound payloads are normalized into a canonical message shape
before persistence to guarantee idempotent, channel-agnostic processing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

CRM_CHANNELS = {"shopware", "amazon", "ebay", "email", "messenger"}
MESSAGE_DIRECTIONS = {"inbound", "outbound"}
THREAD_STATUSES = {"open", "pending", "resolved", "closed"}
APPOINTMENT_STATUSES = {"scheduled", "confirmed", "completed", "cancelled", "no_show"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def normalize_channel(channel: str | None) -> str:
    c = (channel or "").strip().lower()
    aliases = {"shopware6": "shopware", "amazon_sp_api": "amazon", "ebay_marketplace": "ebay", "mail": "email"}
    c = aliases.get(c, c)
    if c not in CRM_CHANNELS:
        raise ValueError(f"Unsupported channel: {channel}")
    return c


def compute_external_message_key(channel: str, external_message_id: str | None, *, thread_external_id: str = "", sender: str = "", sent_at: str = "", body: str = "") -> str:
    """Stable idempotency key for deduplication across retries/webhooks/polling."""
    ch = normalize_channel(channel)
    ext = (external_message_id or "").strip()
    if ext:
        return f"{ch}:{ext}"
    material = "|".join([ch, thread_external_id.strip(), sender.strip(), sent_at.strip(), hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:16]])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class NormalizedMessage:
    channel: str
    direction: str
    body: str
    subject: str = ""
    sender: str = ""
    recipient: str = ""
    external_message_id: str = ""
    external_thread_id: str = ""
    customer_external_id: str = ""
    customer_email: str = ""
    order_external_id: str = ""
    sent_at: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        self.channel = normalize_channel(self.channel)
        if self.direction not in MESSAGE_DIRECTIONS:
            raise ValueError(f"Unsupported direction: {self.direction}")
        self.body = (self.body or "").strip()
        if not self.body and not self.attachments:
            raise ValueError("Message body or attachment required")
        self.customer_email = normalize_email(self.customer_email)
        self.sent_at = self.sent_at or utc_now_iso()

    @property
    def idempotency_key(self) -> str:
        return compute_external_message_key(
            self.channel,
            self.external_message_id,
            thread_external_id=self.external_thread_id,
            sender=self.sender,
            sent_at=self.sent_at,
            body=self.body,
        )

    def to_record(self) -> dict[str, Any]:
        self.validate()
        return {
            "channel": self.channel,
            "direction": self.direction,
            "subject": self.subject,
            "body": self.body,
            "sender": self.sender,
            "recipient": self.recipient,
            "external_message_id": self.external_message_id,
            "external_thread_id": self.external_thread_id,
            "customer_external_id": self.customer_external_id,
            "customer_email": self.customer_email,
            "order_external_id": self.order_external_id,
            "sent_at": self.sent_at,
            "attachments_json": json.dumps(self.attachments, ensure_ascii=False),
            "metadata_json": json.dumps(self.metadata, ensure_ascii=False),
            "idempotency_key": self.idempotency_key,
        }


def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def normalize_shopware_message(payload: Mapping[str, Any], *, direction: str = "inbound") -> NormalizedMessage:
    # Supports contact form/customer message/webhook-like payloads; fields are best-effort.
    cust = payload.get("customer") or {}
    msg = NormalizedMessage(
        channel="shopware",
        direction=direction,
        body=_safe_str(payload.get("comment") or payload.get("message") or payload.get("content") or payload.get("body")),
        subject=_safe_str(payload.get("subject") or payload.get("title")),
        sender=_safe_str(payload.get("email") or cust.get("email") or payload.get("sender")),
        recipient=_safe_str(payload.get("recipient") or payload.get("salesChannelId")),
        external_message_id=_safe_str(payload.get("id") or payload.get("messageId")),
        external_thread_id=_safe_str(payload.get("threadId") or payload.get("ticketId") or payload.get("orderNumber")),
        customer_external_id=_safe_str(payload.get("customerId") or cust.get("id")),
        customer_email=_safe_str(payload.get("email") or cust.get("email")),
        order_external_id=_safe_str(payload.get("orderId") or payload.get("orderNumber")),
        sent_at=_safe_str(payload.get("createdAt") or payload.get("sentAt")),
        metadata={"source": "shopware", "raw_keys": list(payload.keys())[:30]},
    )
    msg.validate()
    return msg


def normalize_amazon_message(payload: Mapping[str, Any], *, direction: str = "inbound") -> NormalizedMessage:
    # Amazon SP-API Buyer Messaging style fields (best-effort normalization)
    msg = NormalizedMessage(
        channel="amazon",
        direction=direction,
        body=_safe_str(payload.get("message") or payload.get("body") or payload.get("text")),
        subject=_safe_str(payload.get("subject")),
        sender=_safe_str(payload.get("sender") or payload.get("buyerEmail") or payload.get("buyerName")),
        recipient=_safe_str(payload.get("recipient") or payload.get("sellerId")),
        external_message_id=_safe_str(payload.get("messageId") or payload.get("id")),
        external_thread_id=_safe_str(payload.get("threadId") or payload.get("conversationId") or payload.get("orderId")),
        customer_external_id=_safe_str(payload.get("buyerId") or payload.get("customerId")),
        customer_email=_safe_str(payload.get("buyerEmail")),
        order_external_id=_safe_str(payload.get("orderId") or payload.get("amazonOrderId")),
        sent_at=_safe_str(payload.get("createdDate") or payload.get("sentAt") or payload.get("timestamp")),
        metadata={"source": "amazon_sp_api", "marketplaceId": _safe_str(payload.get("marketplaceId"))},
    )
    msg.validate()
    return msg


def normalize_ebay_message(payload: Mapping[str, Any], *, direction: str = "inbound") -> NormalizedMessage:
    msg = NormalizedMessage(
        channel="ebay",
        direction=direction,
        body=_safe_str(payload.get("body") or payload.get("message") or payload.get("text") or payload.get("content")),
        subject=_safe_str(payload.get("subject")),
        sender=_safe_str(payload.get("sender") or payload.get("buyerUsername") or payload.get("senderUsername")),
        recipient=_safe_str(payload.get("recipient") or payload.get("sellerUsername")),
        external_message_id=_safe_str(payload.get("messageId") or payload.get("id")),
        external_thread_id=_safe_str(payload.get("conversationId") or payload.get("threadId") or payload.get("itemId")),
        customer_external_id=_safe_str(payload.get("buyerUsername") or payload.get("buyerId")),
        customer_email=_safe_str(payload.get("buyerEmail") or payload.get("senderEmail")),
        order_external_id=_safe_str(payload.get("orderId") or payload.get("transactionId")),
        sent_at=_safe_str(payload.get("creationDate") or payload.get("sentAt") or payload.get("messageCreationDate")),
        metadata={"source": "ebay"},
    )
    msg.validate()
    return msg


def normalize_email_message(payload: Mapping[str, Any], *, direction: str = "inbound") -> NormalizedMessage:
    msg = NormalizedMessage(
        channel="email",
        direction=direction,
        body=_safe_str(payload.get("body") or payload.get("text") or payload.get("html") or payload.get("content")),
        subject=_safe_str(payload.get("subject")),
        sender=_safe_str(payload.get("from") or payload.get("sender")),
        recipient=_safe_str(payload.get("to") or payload.get("recipient")),
        external_message_id=_safe_str(payload.get("messageId") or payload.get("id") or payload.get("providerMessageId")),
        external_thread_id=_safe_str(payload.get("threadId") or payload.get("inReplyTo") or payload.get("conversationId")),
        customer_external_id=_safe_str(payload.get("customerId")),
        customer_email=_safe_str(payload.get("from") or payload.get("customerEmail")),
        order_external_id=_safe_str(payload.get("orderId")),
        sent_at=_safe_str(payload.get("date") or payload.get("sentAt")),
        metadata={"source": "email"},
    )
    msg.validate()
    return msg


def normalize_messenger_message(payload: Mapping[str, Any], *, direction: str = "inbound") -> NormalizedMessage:
    msg = NormalizedMessage(
        channel="messenger",
        direction=direction,
        body=_safe_str(payload.get("text") or payload.get("message") or payload.get("body")),
        subject=_safe_str(payload.get("subject")),
        sender=_safe_str(payload.get("senderId") or payload.get("from") or payload.get("sender")),
        recipient=_safe_str(payload.get("recipientId") or payload.get("to") or payload.get("pageId")),
        external_message_id=_safe_str(payload.get("mid") or payload.get("messageId") or payload.get("id")),
        external_thread_id=_safe_str(payload.get("threadId") or payload.get("conversationId")),
        customer_external_id=_safe_str(payload.get("senderId") or payload.get("customerId")),
        customer_email=_safe_str(payload.get("email")),
        order_external_id=_safe_str(payload.get("orderId")),
        sent_at=_safe_str(payload.get("timestamp") or payload.get("sentAt")),
        metadata={"source": "messenger", "platform": _safe_str(payload.get("platform") or "messenger")},
    )
    msg.validate()
    return msg


NORMALIZERS = {
    "shopware": normalize_shopware_message,
    "amazon": normalize_amazon_message,
    "ebay": normalize_ebay_message,
    "email": normalize_email_message,
    "messenger": normalize_messenger_message,
}


def normalize_message(channel: str, payload: Mapping[str, Any], *, direction: str = "inbound") -> NormalizedMessage:
    ch = normalize_channel(channel)
    return NORMALIZERS[ch](payload, direction=direction)


def payment_verified(order: Mapping[str, Any] | None) -> bool:
    if not order:
        return False
    status = str(order.get("payment_status") or "").strip().lower()
    return status in {"paid", "verified", "captured", "completed"}


def fulfillment_release_allowed(order: Mapping[str, Any] | None) -> bool:
    if not order:
        return False
    locked = order.get("fulfillment_locked")
    if isinstance(locked, str):
        locked = locked.strip().lower() in {"1", "true", "yes"}
    return payment_verified(order) and not bool(locked)
