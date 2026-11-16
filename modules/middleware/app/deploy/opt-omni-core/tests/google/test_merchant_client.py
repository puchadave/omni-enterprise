"""Tests for Google Merchant Center Client."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from services.google_merchant_client import (
    GoogleMerchantClient,
    GoogleMerchantError,
    GoogleMerchantAuthenticationError,
    GoogleMerchantNotFoundError,
    GoogleMerchantRateLimitError,
    GoogleOAuth2Token,
)


# ── OAuth2 Token Tests ──────────────────────────────────────────────────

def test_oauth2_token_expiry():
    """Test token expiry detection."""
    token = GoogleOAuth2Token(access_token="test", expires_in=3600)
    assert not token.is_expired
    assert token.authorization_header == {"Authorization": "Bearer test"}


def test_oauth2_token_headers():
    """Test token authorization header."""
    token = GoogleOAuth2Token(access_token="abc123", expires_in=100)
    headers = token.authorization_header
    assert headers["Authorization"] == "Bearer abc123"


# ── Client Initialization Tests ─────────────────────────────────────────

def test_client_initialization():
    """Test client initialization."""
    client = GoogleMerchantClient(merchant_id="123456789")
    assert client.merchant_id == "123456789"
    assert client._client is None


def test_client_set_token():
    """Test setting OAuth2 token."""
    client = GoogleMerchantClient(merchant_id="123456789")
    client.set_oauth2_token("token123", 3600, "refresh123")
    assert client._token is not None
    assert client._token.access_token == "token123"
    assert client._token.refresh_token == "refresh123"


def test_client_set_service_account():
    """Test setting service account credentials."""
    client = GoogleMerchantClient(merchant_id="123456789")
    client.set_service_account({"client_id": "client123", "private_key": "key123"})
    assert client._service_account_credentials is not None


# ── Exception Tests ─────────────────────────────────────────────────────

def test_google_exceptions():
    """Test exception hierarchy."""
    base = GoogleMerchantError(500, "Server error")
    assert base.status_code == 500
    assert "Server error" in str(base)

    auth = GoogleMerchantAuthenticationError(401, "Unauthorized")
    assert auth.status_code == 401
    assert isinstance(auth, GoogleMerchantError)

    not_found = GoogleMerchantNotFoundError(404, "Not found")
    assert not_found.status_code == 404
    assert isinstance(not_found, GoogleMerchantError)

    rate_limit = GoogleMerchantRateLimitError(429, "Rate limited")
    assert rate_limit.status_code == 429
    assert isinstance(rate_limit, GoogleMerchantError)


# ── Integration Test Marker ─────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
async def test_client_health_check():
    """Integration test: requires running Google Merchant API."""
    client = GoogleMerchantClient(merchant_id="123456789")
    # Just verify client can be created without API access
    assert client is not None
    assert not client.is_authenticated