from __future__ import annotations

import pytest

from services.gtin_intelligence import (
    gtin_type,
    is_valid_gtin,
    lookup_gtin,
    normalize_gtin,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("4006381333931", "4006381333931"),
        (" 4006-3813-33931 ", "4006381333931"),
        ("abc", ""),
        (None, ""),
        ("", ""),
    ],
)
def test_normalize_gtin(raw, expected):
    assert normalize_gtin(raw) == expected


@pytest.mark.parametrize(
    "value,valid,reason",
    [
        ("4006381333931", True, "ok"),  # valid EAN-13
        ("036000291452", True, "ok"),   # valid UPC-A/GTIN-12
        ("96385074", True, "ok"),       # valid GTIN-8
        ("4006381333932", False, "invalid_check_digit"),
        ("123", False, "unsupported_length_3"),
        ("abcdefghijklm", False, "empty_or_non_numeric"),
    ],
)
def test_is_valid_gtin(value, valid, reason):
    ok, why = is_valid_gtin(value)
    assert ok is valid
    assert why == reason


def test_gtin_type():
    assert gtin_type("96385074") == "GTIN-8/EAN-8"
    assert gtin_type("036000291452") == "GTIN-12/UPC-A"
    assert gtin_type("4006381333931") == "GTIN-13/EAN-13"
    assert gtin_type("10012345678902") == "GTIN-14"
    assert gtin_type("123") == "unknown"


@pytest.mark.asyncio
async def test_lookup_gtin_invalid_input():
    result = await lookup_gtin("")
    assert result["valid"] is False
    assert result["reconciliation"]["status"] == "invalid_input"
    assert result["providers"] == []


@pytest.mark.asyncio
async def test_lookup_gtin_no_providers_configured(monkeypatch):
    async def fake_list_providers(enabled_only=True):
        return []

    async def fake_save_lookup(gtin, record):
        return "lookup-1"

    monkeypatch.setattr("services.gtin_intelligence.gtin_list_providers", fake_list_providers)
    monkeypatch.setattr("services.gtin_intelligence.gtin_save_lookup", fake_save_lookup)
    result = await lookup_gtin("4006381333931")
    assert result["valid"] is True
    assert result["gtin"] == "4006381333931"
    assert result["type"] == "GTIN-13/EAN-13"
    assert result["reconciliation"]["status"] == "no_providers_configured"
    assert len(result["providers"]) >= 1
    assert all(p["found"] is False for p in result["providers"])
    assert result["confidence"] == 0.4


@pytest.mark.asyncio
async def test_lookup_gtin_unknown_provider(monkeypatch):
    async def fake_list_providers(enabled_only=True):
        return [{"name": "mystery_provider", "enabled": True, "config": {}}]

    async def fake_save_lookup(gtin, record):
        return "lookup-2"

    monkeypatch.setattr("services.gtin_intelligence.gtin_list_providers", fake_list_providers)
    monkeypatch.setattr("services.gtin_intelligence.gtin_save_lookup", fake_save_lookup)
    result = await lookup_gtin("4006381333931")
    assert result["reconciliation"]["status"] == "not_found_or_not_configured"
    assert result["providers"][0]["provider"] == "mystery_provider"
    assert result["providers"][0]["error"] == "unknown_provider"
