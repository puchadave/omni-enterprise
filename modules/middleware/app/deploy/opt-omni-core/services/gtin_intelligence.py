"""GTIN/EAN Product Intelligence: normalization, validation, provider framework, enrichment & confidence.

Design notes:
- No external provider calls are fabricated. Provider adapters are configurable and run only when
  explicitly configured with verified endpoint/credentials and enabled.
- EAN/GTIN values are never invented; provenance/confidence are tracked per field.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional

from database import (
    gtin_save_lookup, gtin_list_lookups, gtin_save_enrichment, gtin_list_enrichments,
    gtin_save_conflict, gtin_list_conflicts, gtin_upsert_provider, gtin_list_providers,
)

logger = logging.getLogger(__name__)


def normalize_gtin(value: str | None) -> str:
    if not value:
        return ""
    s = re.sub(r"\s+", "", str(value)).replace("-", "")
    if not s.isdigit():
        return ""
    # pad common lengths to GTIN-14 for comparison when appropriate? keep normalized digits
    return s


def is_valid_gtin(value: str | None) -> tuple[bool, str]:
    s = normalize_gtin(value)
    if not s:
        return False, "empty_or_non_numeric"
    if len(s) not in (8, 12, 13, 14):
        return False, f"unsupported_length_{len(s)}"
    # check digit (mod10) - rightmost is check digit
    digits = [int(c) for c in s]
    body, check = digits[:-1], digits[-1]
    # weights from right to left on body: 3,1,3,1...
    total = 0
    weight = 3
    for d in reversed(body):
        total += d * weight
        weight = 1 if weight == 3 else 3
    calc = (10 - (total % 10)) % 10
    return (calc == check), ("ok" if calc == check else "invalid_check_digit")


def gtin_type(value: str | None) -> str:
    s = normalize_gtin(value)
    return {8: "GTIN-8/EAN-8", 12: "GTIN-12/UPC-A", 13: "GTIN-13/EAN-13", 14: "GTIN-14"}.get(len(s), "unknown")


@dataclass
class ProviderResult:
    provider: str
    found: bool
    data: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    trust_level: str = "C"  # A|B|C
    license_info: str = ""
    source_record_id: str = ""
    error: str = ""


class BaseProviderAdapter:
    name: str = "base"
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}

    async def lookup(self, gtin: str) -> ProviderResult:
        return ProviderResult(provider=self.name, found=False, error="not_configured")


class OpenEANAdapter(BaseProviderAdapter):
    name = "openean"
    async def lookup(self, gtin: str) -> ProviderResult:
        # Real implementation requires verified API docs/auth/terms/license. Do not fabricate endpoints.
        endpoint = (self.config or {}).get("endpoint") or ""
        if not endpoint or not (self.config or {}).get("enabled"):
            return ProviderResult(provider=self.name, found=False, error="not_configured_or_unverified", trust_level="C",
                                  data={"note": "OpenEAN-Adapter deaktiviert: Endpoint/Doku/Lizenz/Zugang nicht verifiziert konfiguriert."})
        # Placeholder for verified implementation (http client call would go here with rate limiting)
        return ProviderResult(provider=self.name, found=False, error="implementation_requires_verified_api_access", trust_level="C")


class GS1VerifiedAdapter(BaseProviderAdapter):
    name = "gs1_verified"
    async def lookup(self, gtin: str) -> ProviderResult:
        if not (self.config or {}).get("enabled"):
            return ProviderResult(provider=self.name, found=False, error="not_configured_requires_authorized_access", trust_level="A")
        return ProviderResult(provider=self.name, found=False, error="implementation_requires_authorized_access", trust_level="A")


PROVIDER_REGISTRY: dict[str, type[BaseProviderAdapter]] = {
    "openean": OpenEANAdapter,
    "gs1_verified": GS1VerifiedAdapter,
}


async def lookup_gtin(gtin: str, providers: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    normalized = normalize_gtin(gtin)
    valid, reason = is_valid_gtin(normalized)
    record = {
        "gtin": normalized,
        "type": gtin_type(normalized),
        "valid": valid,
        "validation": reason,
        "providers": [],
        "reconciliation": {"status": "no_external_data", "conflicts": [], "chosen": {}},
        "confidence": 0.0 if not valid else 0.4,
        "trust_level": "C",
        "provenance": [],
    }
    if not normalized:
        record["reconciliation"]["status"] = "invalid_input"
        return record

    provider_configs = providers or await gtin_list_providers(enabled_only=True)
    # if none configured, use registry placeholders as status info (no calls)
    if not provider_configs:
        for name in PROVIDER_REGISTRY:
            record["providers"].append({"provider": name, "found": False, "error": "not_configured", "note": "Provider nicht konfiguriert/aktiviert."})
        record["reconciliation"]["status"] = "no_providers_configured"
    else:
        results: list[ProviderResult] = []
        for pc in provider_configs:
            name = (pc.get("name") or pc.get("provider") or "").lower()
            cls = PROVIDER_REGISTRY.get(name)
            if not cls:
                record["providers"].append({"provider": name or "unknown", "found": False, "error": "unknown_provider"})
                continue
            adapter = cls(pc.get("config") if isinstance(pc.get("config"), dict) else pc)
            res = await adapter.lookup(normalized)
            results.append(res)
            record["providers"].append({
                "provider": res.provider, "found": res.found, "confidence": res.confidence,
                "trust_level": res.trust_level, "error": res.error, "license_info": res.license_info,
                "data_keys": list(res.data.keys()) if res.data else [],
            })
        found = [r for r in results if r.found and r.data]
        if found:
            # simple reconciliation: prefer higher trust A>B>C then confidence
            rank = {"A": 3, "B": 2, "C": 1}
            found.sort(key=lambda r: (rank.get(r.trust_level, 0), r.confidence), reverse=True)
            chosen = found[0]
            record["reconciliation"] = {"status": "matched", "chosen_provider": chosen.provider, "conflicts": [], "chosen": chosen.data}
            record["confidence"] = chosen.confidence
            record["trust_level"] = chosen.trust_level
            record["provenance"].append({"field": "*", "source_provider": chosen.provider, "source_record_id": chosen.source_record_id, "license": chosen.license_info})
        else:
            record["reconciliation"]["status"] = "not_found_or_not_configured"

    await gtin_save_lookup(normalized, record)
    return record
