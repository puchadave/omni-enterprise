"""Shopware Product Mapping Engine.

Implements a visual field mapping system between Matterhorn supplier data
and Shopware product fields. Supports direct mapping, transformation,
fallback values, conditional logic, field concatenation, text cleaning,
and translation. Mapping versioning is supported for auditability.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .canonical_product_mapping import CanonicalProduct

logger = logging.getLogger(__name__)


# ── Mapping Types ─────────────────────────────────────────────────────────

MAPPING_DIRECT: str = "direct"
MAPPING_TRANSFORM: str = "transform"
MAPPING_FALLBACK: str = "fallback"
MAPPING_CONDITIONAL: str = "conditional"
MAPPING_CONCATENATE: str = "concatenate"
MAPPING_CLEAN: str = "clean"
MAPPING_TRANSLATE: str = "translate"


# ── Field Mapping Definition ──────────────────────────────────────────────

class FieldMapping:
    """Defines how a single Matterhorn field maps to a Shopware field."""

    def __init__(
        self,
        mapping_id: str,
        connection_id: str,
        matterhorn_field: str,
        shopware_field: str,
        mapping_type: str = MAPPING_DIRECT,
        mapping_config: dict[str, Any] | None = None,
        priority: int = 0,
        is_active: bool = True,
        version: int = 1,
    ) -> None:
        self.mapping_id: str = mapping_id
        self.connection_id: str = connection_id
        self.matterhorn_field: str = matterhorn_field
        self.shopware_field: str = shopware_field
        self.mapping_type: str = mapping_type
        self.mapping_config: dict[str, Any] = mapping_config or {}
        self.priority: int = priority
        self.is_active: bool = is_active
        self.version: int = version

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping_id": self.mapping_id,
            "connection_id": self.connection_id,
            "matterhorn_field": self.matterhorn_field,
            "shopware_field": self.shopware_field,
            "mapping_type": self.mapping_type,
            "mapping_config": self.mapping_config,
            "priority": self.priority,
            "is_active": self.is_active,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FieldMapping:
        return cls(
            mapping_id=data["mapping_id"],
            connection_id=data["connection_id"],
            matterhorn_field=data["matterhorn_field"],
            shopware_field=data["shopware_field"],
            mapping_type=data.get("mapping_type", MAPPING_DIRECT),
            mapping_config=data.get("mapping_config", {}),
            priority=data.get("priority", 0),
            is_active=data.get("is_active", True),
            version=data.get("version", 1),
        )


# ── Mapping Engine ────────────────────────────────────────────────────────

class MappingEngine:
    """Applies field mappings to transform Matterhorn data to Shopware format."""

    def __init__(self, mappings: list[FieldMapping]) -> None:
        self.mappings: list[FieldMapping] = [m for m in mappings if m.is_active]
        # Sort by priority (lower = higher priority)
        self.mappings.sort(key=lambda m: m.priority)

    def apply_mapping(
        self,
        matterhorn_data: dict[str, Any],
        canonical_product: CanonicalProduct | None = None,
    ) -> dict[str, Any]:
        """Apply all active mappings to transform data.

        Args:
            matterhorn_data: Raw data from Matterhorn API
            canonical_product: Optional canonical product for enriched fields

        Returns:
            Dict of Shopware field -> mapped value
        """
        result: dict[str, Any] = {}

        # Build a combined data source (Matterhorn + canonical)
        source_data: dict[str, Any] = {}
        source_data.update(matterhorn_data)
        if canonical_product:
            source_data.update(canonical_product.to_dict())

        for mapping in self.mappings:
            try:
                value = self._apply_single_mapping(mapping, source_data, result)
                if value is not None:
                    result[mapping.shopware_field] = value
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("Mapping %s -> %s failed: %s", mapping.matterhorn_field, mapping.shopware_field, exc)

        return result

    def _apply_single_mapping(
        self,
        mapping: FieldMapping,
        source_data: dict[str, Any],
        accumulated_result: dict[str, Any],
    ) -> Any:
        """Apply a single mapping rule."""
        mapping_type = mapping.mapping_type
        config = mapping.mapping_config

        if mapping_type == MAPPING_DIRECT:
            return self._map_direct(mapping, source_data, config)
        if mapping_type == MAPPING_TRANSFORM:
            return self._map_transform(mapping, source_data, config)
        if mapping_type == MAPPING_FALLBACK:
            return self._map_fallback(mapping, source_data, config, accumulated_result)
        if mapping_type == MAPPING_CONDITIONAL:
            return self._map_conditional(mapping, source_data, config)
        if mapping_type == MAPPING_CONCATENATE:
            return self._map_concatenate(mapping, source_data, config)
        if mapping_type == MAPPING_CLEAN:
            return self._map_clean(mapping, source_data, config)
        if mapping_type == MAPPING_TRANSLATE:
            return self._map_translate(mapping, source_data, config)

        logger.warning("Unknown mapping type: %s", mapping_type)
        return None

    # ── Individual Mapping Types ──────────────────────────────────────────

    def _map_direct(self, mapping: FieldMapping, source_data: dict[str, Any], config: dict[str, Any]) -> Any:
        """Direct field copy with optional default."""
        source_field = mapping.matterhorn_field
        value = source_data.get(source_field)
        if value is None or value == "":
            return config.get("default_value")
        return value

    def _map_transform(self, mapping: FieldMapping, source_data: dict[str, Any], config: dict[str, Any]) -> Any:
        """Apply a transformation function to the source value."""
        source_field = mapping.matterhorn_field
        value = source_data.get(source_field)
        if value is None:
            return config.get("default_value")

        transform_type = config.get("transform", "identity")

        if transform_type == "uppercase":
            return str(value).upper()
        if transform_type == "lowercase":
            return str(value).lower()
        if transform_type == "capitalize":
            return str(value).capitalize()
        if transform_type == "title":
            return str(value).title()
        if transform_type == "strip_html":
            return self._strip_html(str(value))
        if transform_type == "truncate":
            max_len = int(config.get("max_length", 255))
            return str(value)[:max_len]
        if transform_type == "prefix":
            return f"{config.get('prefix', '')}{value}"
        if transform_type == "suffix":
            return f"{value}{config.get('suffix', '')}"
        if transform_type == "replace":
            old = config.get("replace_from", "")
            new = config.get("replace_to", "")
            return str(value).replace(old, new)
        if transform_type == "round":
            decimals = int(config.get("decimals", 2))
            return round(float(value), decimals)
        if transform_type == "multiply":
            factor = float(config.get("factor", 1.0))
            return float(value) * factor
        if transform_type == "add_tax":
            tax_rate = float(config.get("tax_rate", 19.0))
            return round(float(value) * (1 + tax_rate / 100.0), 2)
        if transform_type == "remove_tax":
            tax_rate = float(config.get("tax_rate", 19.0))
            return round(float(value) / (1 + tax_rate / 100.0), 2)
        if transform_type == "format_price":
            decimals = int(config.get("decimals", 2))
            return f"{float(value):.{decimals}f}"
        if transform_type == "bool_to_int":
            return 1 if value in (True, 1, "1", "true", "True") else 0
        if transform_type == "int_to_bool":
            return bool(int(value))

        return value

    def _map_fallback(
        self,
        mapping: FieldMapping,
        source_data: dict[str, Any],
        config: dict[str, Any],
        accumulated_result: dict[str, Any],
    ) -> Any:
        """Try primary field, then fallback fields."""
        primary_value = source_data.get(mapping.matterhorn_field)
        if primary_value is not None and primary_value != "":
            return primary_value

        fallback_fields = config.get("fallback_fields", [])
        for fb_field in fallback_fields:
            fb_value = source_data.get(fb_field)
            if fb_value is not None and fb_value != "":
                return fb_value

        return config.get("default_value")

    def _map_conditional(self, mapping: FieldMapping, source_data: dict[str, Any], config: dict[str, Any]) -> Any:
        """Apply conditional logic: if condition, then value A, else value B."""
        condition_field = config.get("condition_field", "")
        condition_value = config.get("condition_value", "")
        condition_operator = config.get("condition_operator", "equals")

        source_value = source_data.get(condition_field, "")

        condition_met = False
        if condition_operator == "equals":
            condition_met = str(source_value) == str(condition_value)
        elif condition_operator == "not_equals":
            condition_met = str(source_value) != str(condition_value)
        elif condition_operator == "contains":
            condition_met = str(condition_value) in str(source_value)
        elif condition_operator == "not_contains":
            condition_met = str(condition_value) not in str(source_value)
        elif condition_operator == "greater_than":
            try:
                condition_met = float(source_value) > float(condition_value)
            except (ValueError, TypeError):
                condition_met = False
        elif condition_operator == "less_than":
            try:
                condition_met = float(source_value) < float(condition_value)
            except (ValueError, TypeError):
                condition_met = False

        if condition_met:
            return config.get("then_value", config.get("value"))
        return config.get("else_value", config.get("default_value"))

    def _map_concatenate(self, mapping: FieldMapping, source_data: dict[str, Any], config: dict[str, Any]) -> Any:
        """Concatenate multiple fields with a separator."""
        fields = config.get("fields", [])
        separator = config.get("separator", " ")
        prefix = config.get("prefix", "")
        suffix = config.get("suffix", "")

        parts = []
        for field in fields:
            value = source_data.get(field)
            if value is not None and value != "":
                parts.append(str(value))

        if not parts:
            return config.get("default_value")

        return f"{prefix}{separator.join(parts)}{suffix}"

    def _map_clean(self, mapping: FieldMapping, source_data: dict[str, Any], config: dict[str, Any]) -> Any:
        """Clean text: strip HTML, normalize whitespace, remove special chars."""
        source_field = mapping.matterhorn_field
        value = source_data.get(source_field)
        if value is None:
            return config.get("default_value")

        text = str(value)

        if config.get("strip_html", False):
            text = self._strip_html(text)
        if config.get("normalize_whitespace", True):
            text = re.sub(r"\s+", " ", text).strip()
        if config.get("remove_special_chars", False):
            text = re.sub(r"[^\w\s\-.,!?äöüÄÖÜß]", "", text)
        if config.get("lowercase", False):
            text = text.lower()
        if config.get("uppercase", False):
            text = text.upper()
        if config.get("title_case", False):
            text = text.title()

        max_length = config.get("max_length")
        if max_length and len(text) > int(max_length):
            text = text[: int(max_length)].rstrip()

        return text if text else config.get("default_value")

    def _map_translate(self, mapping: FieldMapping, source_data: dict[str, Any], config: dict[str, Any]) -> Any:
        """Apply translation mapping (value -> translated value)."""
        source_field = mapping.matterhorn_field
        value = source_data.get(source_field)
        if value is None:
            return config.get("default_value")

        translation_map = config.get("translation_map", {})
        key = str(value)
        return translation_map.get(key, config.get("default_value", value))

    # ── Utilities ─────────────────────────────────────────────────────────

    @staticmethod
    def _strip_html(text: str) -> str:
        """Remove HTML tags from text."""
        clean = re.compile("<.*?>")
        return re.sub(clean, "", text)

    def get_mapping_summary(self) -> dict[str, Any]:
        """Return a summary of all mappings."""
        by_type: dict[str, int] = {}
        for m in self.mappings:
            by_type[m.mapping_type] = by_type.get(m.mapping_type, 0) + 1

        return {
            "total_mappings": len(self.mappings),
            "active_mappings": sum(1 for m in self.mappings if m.is_active),
            "by_type": by_type,
            "fields_mapped": list({m.matterhorn_field for m in self.mappings}),
            "shopware_fields": list({m.shopware_field for m in self.mappings}),
        }


# ── Default Mappings ──────────────────────────────────────────────────────

def get_default_mappings(connection_id: str) -> list[FieldMapping]:
    """Return a sensible default set of Matterhorn -> Shopware field mappings."""
    mappings = [
        # Core product fields
        FieldMapping(
            mapping_id=str(uuid4()),
            connection_id=connection_id,
            matterhorn_field="supplier_product_id",
            shopware_field="productNumber",
            mapping_type=MAPPING_DIRECT,
            priority=1,
        ),
        FieldMapping(
            mapping_id=str(uuid4()),
            connection_id=connection_id,
            matterhorn_field="product_name",
            shopware_field="name",
            mapping_type=MAPPING_DIRECT,
            priority=1,
        ),
        FieldMapping(
            mapping_id=str(uuid4()),
            connection_id=connection_id,
            matterhorn_field="description",
            shopware_field="description",
            mapping_type=MAPPING_CLEAN,
            mapping_config={"strip_html": True, "normalize_whitespace": True},
            priority=1,
        ),
        FieldMapping(
            mapping_id=str(uuid4()),
            connection_id=connection_id,
            matterhorn_field="ean_codes",
            shopware_field="ean",
            mapping_type=MAPPING_DIRECT,
            priority=2,
        ),
        FieldMapping(
            mapping_id=str(uuid4()),
            connection_id=connection_id,
            matterhorn_field="brand",
            shopware_field="manufacturer",
            mapping_type=MAPPING_DIRECT,
            priority=1,
        ),
        FieldMapping(
            mapping_id=str(uuid4()),
            connection_id=connection_id,
            matterhorn_field="category",
            shopware_field="categories",
            mapping_type=MAPPING_DIRECT,
            priority=1,
        ),
        FieldMapping(
            mapping_id=str(uuid4()),
            connection_id=connection_id,
            matterhorn_field="supplier_price_netto",
            shopware_field="price",
            mapping_type=MAPPING_TRANSFORM,
            mapping_config={"transform": "add_tax", "tax_rate": 19.0},
            priority=1,
        ),
        FieldMapping(
            mapping_id=str(uuid4()),
            connection_id=connection_id,
            matterhorn_field="stock_total",
            shopware_field="stock",
            mapping_type=MAPPING_DIRECT,
            priority=1,
        ),
        FieldMapping(
            mapping_id=str(uuid4()),
            connection_id=connection_id,
            matterhorn_field="color",
            shopware_field="color",
            mapping_type=MAPPING_DIRECT,
            priority=3,
        ),
        FieldMapping(
            mapping_id=str(uuid4()),
            connection_id=connection_id,
            matterhorn_field="material",
            shopware_field="material",
            mapping_type=MAPPING_DIRECT,
            priority=3,
        ),
        # SEO fields (auto-generated)
        FieldMapping(
            mapping_id=str(uuid4()),
            connection_id=connection_id,
            matterhorn_field="product_name",
            shopware_field="seoUrl",
            mapping_type=MAPPING_TRANSFORM,
            mapping_config={"transform": "lowercase", "replace": {" ": "-"}},
            priority=5,
        ),
        FieldMapping(
            mapping_id=str(uuid4()),
            connection_id=connection_id,
            matterhorn_field="product_name",
            shopware_field="seoTitle",
            mapping_type=MAPPING_TRANSFORM,
            mapping_config={"transform": "title", "truncate": {"max_length": 70}},
            priority=5,
        ),
    ]
    return mappings