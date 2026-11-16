"""Central Connector Registry for Omni-Core Multi-Channel Commerce Platform.

Manages all registered sales platform connectors, capability discovery,
and connector lifecycle instantiation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Type

from services.connector_sdk import ConnectorCapability, SalesChannelConnector
from services.connectors.shopware import PuchaShopwareConnector
from services.connectors.ebay import PuchaEbayConnector
from services.connectors.amazon import PuchaAmazonConnector
from services.connectors.kaufland import PuchaKauflandConnector
from services.connectors.standard_connectors import (
    PuchaOttoConnector,
    PuchaShopifyConnector,
    PuchaWooCommerceConnector,
    PuchaEtsyConnector,
    PuchaTikTokShopConnector,
    PuchaAllegroConnector,
    PuchaGoogleMerchantConnector,
)

logger = logging.getLogger("omni.connectors.registry")


class ConnectorRegistry:
    """Central singleton managing all sales channel connectors."""

    _instance: Optional["ConnectorRegistry"] = None

    def __init__(self):
        self._connectors: Dict[str, Type[SalesChannelConnector]] = {}
        self._register_default_connectors()

    @classmethod
    def get_instance(cls) -> "ConnectorRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _register_default_connectors(self) -> None:
        """Register initial target connectors."""
        self.register(PuchaShopwareConnector)
        self.register(PuchaEbayConnector)
        self.register(PuchaAmazonConnector)
        self.register(PuchaKauflandConnector)
        self.register(PuchaOttoConnector)
        self.register(PuchaShopifyConnector)
        self.register(PuchaWooCommerceConnector)
        self.register(PuchaEtsyConnector)
        self.register(PuchaTikTokShopConnector)
        self.register(PuchaAllegroConnector)
        self.register(PuchaGoogleMerchantConnector)

    def register(self, connector_cls: Type[SalesChannelConnector]) -> None:
        """Register a connector class with the registry."""
        temp = connector_cls()
        cid = temp.connector_id
        self._connectors[cid] = connector_cls
        logger.info(f"Registered sales channel connector: {cid} ({temp.platform_name})")

    def get_connector_class(self, connector_id: str) -> Optional[Type[SalesChannelConnector]]:
        return self._connectors.get(connector_id)

    def instantiate(self, connector_id: str, config: Optional[Dict[str, Any]] = None, account_id: str = "") -> Optional[SalesChannelConnector]:
        cls = self.get_connector_class(connector_id)
        if not cls:
            return None
        return cls(config=config, account_id=account_id)

    def list_connectors(self) -> List[Dict[str, Any]]:
        """Return catalog of all registered connectors with metadata and capabilities."""
        results = []
        for cid, cls in self._connectors.items():
            instance = cls()
            caps = instance.get_capabilities()
            results.append({
                "connector_id": cid,
                "platform_name": instance.platform_name,
                "version": instance.version,
                "api_version": instance.api_version,
                "capabilities": [c.value for c in caps],
                "supported_operations": [c.value for c in caps],
            })
        return results
