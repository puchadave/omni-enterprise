"""Pucha Connector SDK - Standardized Sales Channel Connector Framework.

Defines the contract, capability discovery, validation, and lifecycle interfaces
for all multi-channel sales platform connectors.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class ConnectorCapability(str, enum.Enum):
    """Capabilities explicitly declared and supported by a sales channel connector."""
    PRODUCT_CREATE = "PRODUCT_CREATE"
    PRODUCT_UPDATE = "PRODUCT_UPDATE"
    PRODUCT_DELETE = "PRODUCT_DELETE"
    INVENTORY_SYNC = "INVENTORY_SYNC"
    PRICE_SYNC = "PRICE_SYNC"
    ORDER_IMPORT = "ORDER_IMPORT"
    FULFILLMENT_UPDATE = "FULFILLMENT_UPDATE"
    MEDIA_SYNC = "MEDIA_SYNC"
    VARIANT_MANAGEMENT = "VARIANT_MANAGEMENT"
    CATEGORY_MAPPING = "CATEGORY_MAPPING"
    ATTRIBUTE_MAPPING = "ATTRIBUTE_MAPPING"
    LISTING_STATUS = "LISTING_STATUS"


class ConnectionState(str, enum.Enum):
    """Connection and readiness status of a connector."""
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    CONFIG_REQUIRED = "config_required"
    PENDING_APPROVAL = "pending_approval"
    SANDBOX_ACTIVE = "sandbox_active"
    ERROR = "error"


@dataclass
class HealthResult:
    """Result of a connector connection or health check."""
    status: ConnectionState
    healthy: bool
    latency_ms: Optional[float] = None
    message: str = ""
    account_id: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ValidationResult:
    """Result of validating a canonical product against marketplace listing rules."""
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    missing_required_attributes: List[str] = field(default_factory=list)
    sku: str = ""
    channel: str = ""


@dataclass
class ChannelProduct:
    """Marketplace-specific transformed product ready for export."""
    sku: str
    channel: str
    title: str
    description: str
    price: float
    currency: str
    stock: int
    category_id: Optional[str] = None
    category_path: Optional[str] = None
    gtin_ean: Optional[str] = None
    brand: Optional[str] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    variants: List[Dict[str, Any]] = field(default_factory=list)
    images: List[str] = field(default_factory=list)
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    transformed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ExportResult:
    """Outcome of exporting a product to a marketplace."""
    success: bool
    sku: str
    channel: str
    remote_listing_id: Optional[str] = None
    message: str = ""
    error_code: Optional[str] = None
    raw_response: Dict[str, Any] = field(default_factory=dict)
    exported_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ListingStatus:
    """Live status of a listing on an external marketplace."""
    sku: str
    channel: str
    remote_listing_id: str
    status: str  # active, draft, suppressed, pending_review, rejected, out_of_stock
    current_price: Optional[float] = None
    current_stock: Optional[int] = None
    last_synced_at: Optional[str] = None
    rejection_reasons: List[str] = field(default_factory=list)


class SalesChannelConnector(abc.ABC):
    """Abstract base class that all sales channel connectors MUST implement.
    
    Adheres to the Pucha Connector SDK standard.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None, account_id: str = ""):
        self.config = config or {}
        self.account_id = account_id

    @property
    @abc.abstractmethod
    def connector_id(self) -> str:
        """Unique identifier, e.g. 'pucha_shopware', 'pucha_ebay'."""
        pass

    @property
    @abc.abstractmethod
    def platform_name(self) -> str:
        """Human-readable platform name, e.g. 'Shopware 6', 'eBay'."""
        pass

    @property
    @abc.abstractmethod
    def version(self) -> str:
        """Semantic version of the connector."""
        pass

    @property
    @abc.abstractmethod
    def api_version(self) -> str:
        """Official API version targeted by this connector."""
        pass

    @abc.abstractmethod
    def get_capabilities(self) -> List[ConnectorCapability]:
        """Declare all capabilities supported by this connector."""
        pass

    @abc.abstractmethod
    async def test_connection(self) -> HealthResult:
        """Execute a live ping or healthcheck against the marketplace API."""
        pass

    @abc.abstractmethod
    async def validate_product(self, product: Any) -> ValidationResult:
        """Validate canonical product against channel-specific listing requirements."""
        pass

    @abc.abstractmethod
    async def transform_product(self, product: Any, channel_override: Optional[Dict[str, Any]] = None) -> ChannelProduct:
        """Transform a canonical product into marketplace-specific format."""
        pass

    @abc.abstractmethod
    async def export_product(self, product: ChannelProduct) -> ExportResult:
        """Export or create listing on the marketplace."""
        pass

    @abc.abstractmethod
    async def update_product(self, listing_id: str, product: ChannelProduct) -> ExportResult:
        """Update an existing marketplace listing."""
        pass

    @abc.abstractmethod
    async def delete_product(self, listing_id: str) -> ExportResult:
        """Delete or withdraw a listing where supported."""
        pass

    @abc.abstractmethod
    async def get_listing_status(self, listing_id: str) -> ListingStatus:
        """Fetch remote listing status from the marketplace."""
        pass
