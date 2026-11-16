"""Matterhorn Feed Discovery Service.

Automatically discovers and analyzes Matterhorn product feeds.
Determines format, categories, languages, currencies, and size systems
without prior configuration.
"""

from __future__ import annotations

import re
import logging
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Detection Result ───────────────────────────────────────────────────────

class FeedDiscoveryResult:
    """Result of feed discovery analysis."""
    def __init__(self) -> None:
        self.format: str = "unknown"
        self.categories: list[str] = []
        self.languages: list[str] = []
        self.currencies: list[str] = []
        self.size_systems: list[str] = []
        self.has_ean: bool = False
        self.has_gtin: bool = False
        self.product_count_estimate: int = 0
        self.metadata: dict[str, Any] = {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "categories": self.categories,
            "languages": self.languages,
            "currencies": self.currencies,
            "size_systems": self.size_systems,
            "has_ean": self.has_ean,
            "has_gtin": self.has_gtin,
            "product_count_estimate": self.product_count_estimate,
            "metadata": self.metadata,
        }

# ── Feed Discovery Service ────────────────────────────────────────────────

class FeedDiscoveryService:
    """Discovers and analyzes Matterhorn product feeds automatically."""

    # ── Public API ─────────────────────────────────────────────────────────

    @staticmethod
    def analyze(
        text: str,
        filename: str | None = None,
        extra_text: str | None = None,
    ) -> FeedDiscoveryResult:
        """Analyze a feed and return discovery results."""
        result = FeedDiscoveryResult()
        text_combined = (text or "") + (extra_text or "")

        # 1. Format detection
        result.format = _detect_format(text_combined)

        # 2. Product count estimate
        result.product_count_estimate = _estimate_product_count(text_combined)

        # 3. Category extraction
        result.categories = _extract_categories(text_combined)

        # 3b. Language detection
        result.languages = _extract_languages(text_combined)

        # 4. Currency detection
        result.currencies = _extract_currencies(text_combined)

        # 5. EAN/GTIN detection
        result.has_ean, result.has_gtin = _detect_ean_gtin(text_combined)

        # 6. Size system detection
        result.size_systems = _extract_size_systems(text_combined)

        # 7. Metadata (anything else notable)
        result.metadata = _extract_metadata(text_combined)

        logger.info(
            "Feed discovered: format=%s, products≈%d, categories=%s, "
            "currencies=%s, EAN=%s",
            result.format,
            result.product_count_estimate,
            result.categories,
            result.currencies,
            result.has_ean,
        )

        return result

    # ── Internal detection methods ────────────────────────────────────────

    @staticmethod
    def _detect_format(text: str) -> str:
        """Detect the feed format from content."""
        text_lower = text.lower()

        # XML detection
        if text.startswith("<") and ("<product>" in text_lower or "<offer>" in text_lower):
            # Check for XML 2.0 markers
            if "<xmlversion" in text_lower or "version=" in text_lower:
                return "xml_v2"
            # Check for EAN-focused structure
            if re.search(r"<ean>|\<gtin>", text_lower):
                return "xml_ean"
            return "xml"

        # CSV detection (semicolon or comma separated with header-like first line)
        lines = text.splitlines()
        if lines and (";" in lines[0] or "," in lines[0]):
            # Check for typical product CSV headers
            first_line = lines[0].lower()
            if any(h in first_line for h in ["sku", "title", "price", "stock"]):
                return "csv"
            # Could also be XLS export format
            return "csv"

        # XLS/XLSX indicators (tab-heavy, grid-like structure)
        if "\t" in text[:200] and text.count("\t") > 3:
            return "xls"

        # Default
        return "unknown"

    @staticmethod
    def _estimate_product_count(text: str) -> int:
        """Estimate the number of products in the feed."""
        # Count product markers
        text_lower = text.lower()

        xml_products = len(re.findall(r"<product>|<offer>|<item>", text_lower))
        csv_lines = len([l for l in text.splitlines() if l.strip()])
        csv_header_indicator = len(re.findall(r"sku|title|price", text_lower))

        # Heuristic: if XML markers found, use that count divided by typical per-page amount
        if xml_products > 0:
            # Typical XML feeds have 10-100 products per file, but could be more
            # Count unique <product> tags as estimate
            estimated = min(xml_products, 500)  # Cap at 500 for safety
            return max(estimated, 1)

        # CSV: rough estimate based on non-empty lines
        if csv_lines > 1:
            # First line is header, count data lines
            data_lines = csv_lines - 1
            return min(data_lines, 1000)

        # Default
        return 1

    @staticmethod
    def _extract_categories(text: str) -> list[str]:
        """Extract category names from feed content."""
        categories: set[str] = set()

        # XML: find <category> or <cat> elements
        xml_matches = re.findall(r"<category>\s*([^<>\n]+)</category>", text, re.IGNORECASE)
        categories.update(xml_matches)

        # CSV: look in header or data for category-like entries
        lines = text.splitlines()
        if lines:
            # Check header row
            headers = lines[0].lower().split(";") if ";" in lines[0] else lines[0].split(",")
            for h in headers:
                h = h.strip()
                if h and len(h) > 2 and len(h) < 50:
                    categories.add(h)

        # General word extraction for category-like terms
        word_matches = re.findall(r"\b[A-Z][A-Za-zäöüÄÖÜß]+\b", text)
        for w in word_matches:
            if w.lower() not in ("the", "and", "for", "with", "from", "are", "but", "not"):
                categories.add(w)

        # Filter to reasonable category names (3-50 chars, multiple words possible)
        filtered: set[str] = set()
        for c in categories:
            if 3 <= len(c) <= 50:
                filtered.add(c)

        return sorted(list(filtered))[:20]  # Limit to 20 categories

    @staticmethod
    def _extract_languages(text: str) -> list[str]:
        """Extract languages mentioned or detected in feed."""
        languages: set[str] = set()

        # Common language tokens in feeds
        lang_patterns: dict[str, str] = {
            "de": "deutsch|german",
            "en": "english",
            "fr": "francais|french",
            "it": "italiano|italian",
            "es": "español|spanish",
        }

        text_lower = text.lower()
        for lang_code, pattern in lang_patterns.items():
            if re.search(pattern, text_lower):
                languages.add(lang_code)

        # If no languages detected, default to 'en' (English) as most feeds
        # have English content even if other languages are present
        if not languages:
            languages.add("en")

        return sorted(list(languages))

    @staticmethod
    def _extract_currencies(text: str) -> list[str]:
        """Extract currency codes from feed content."""
        currencies: set[str] = set()

        # Match ISO currency codes €, $, £, or explicit codes
        # € Euro
        if "€" in text or re.search(r"\bEUR\b", text, re.IGNORECASE):
            currencies.add("EUR")
        # $ US Dollar
        if re.search(r"\bUSD\b", text, re.IGNORECASE):
            currencies.add("USD")
        # £ British Pound
        if re.search(r"\bGBP\b", text, re.IGNORECASE):
            currencies.add("GBP")
        # CHF Swiss Franc
        if re.search(r"\bCHF\b", text, re.IGNORECASE):
            currencies.add("CHF")

        # If no currencies detected, default to EUR (Matterhorn default)
        if not currencies:
            currencies.add("EUR")

        return sorted(list(currencies))

    @staticmethod
    def _detect_ean_gtin(text: str) -> tuple[bool, bool]:
        """Detect if EAN or GTIN codes are present in the feed."""
        has_ean = bool(re.search(r"\b\d{8,13}\b", text)) or "ean" in text.lower()
        has_gtin = bool(re.search(r"\bGTIN\b|\bEAN13\b", text, re.IGNORECASE))
        return has_ean, has_gtin

    @staticmethod
    def _extract_size_systems(text: str) -> list[str]:
        """Extract size systems mentioned in the feed."""
        systems: set[str] = set()

        # Common size system indicators
        size_patterns: dict[str, str] = {
            "eu": r"\bEU\b|\b3[0-9]{2}\b",  # EU sizes like 36, 37, 38
            "uk": r"\bUK\b|\b[4-9][0-9]\b",  # UK sizes
            "us": r"\bUS\b|\b[5-12][0-9]\b(?!\s*EU)",  # US sizes
            "fr": r"\bFR\b|\b[3][0-9]{2}\b",  # French sizes
            "it": r"\bIT\b|\b[38-48]\b",  # Italian sizes
            "cm": r"\bcm\b",  # Centimeter sizes
            "inch": r"\bin\b",  # Inch sizes
        }

        text_lower = text.lower()
        for system_code, pattern in size_patterns.items():
            if re.search(pattern, text_lower):
                systems.add(system_code)

        # If no size systems detected, add 'unknown' as placeholder
        if not systems:
            systems.add("unknown")

        return sorted(list(systems))

    @staticmethod
    def _extract_metadata(text: str) -> dict[str, Any]:
        """Extract any notable metadata from the feed."""
        metadata: dict[str, Any] = {}

        # Look for version info
        version_match = re.search(r'version["\s]*[:=]["\s]*["\']?([^"\']+)["\']?', text, re.IGNORECASE)
        if version_match:
            metadata["xml_version"] = version_match.group(1)

        # Look for timestamp
        ts_match = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", text)
        if ts_match:
            metadata["last_updated"] = ts_match.group(0)

        # Look for supplier info
        supplier_match = re.search(r"supplier[:\s]+([^\n\r]+)", text, re.IGNORECASE)
        if supplier_match:
            metadata["supplier"] = supplier_match.group(1).strip()

        return metadata