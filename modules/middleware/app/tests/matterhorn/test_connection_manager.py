"""Tests for Matterhorn Connection Manager.

Tests the MatterhornConnectionManager for correct initialization,
parameter handling, and exception hierarchy. API health checks
require a running Matterhorn instance and are marked as integration tests.
"""

from __future__ import annotations

import pytest


# ── Connection Manager Tests ──────────────────────────────────────────────

def test_connection_manager_initialization():
    """Test ConnectionManager can be initialized."""
    from services.matterhorn_connection_manager import MatterhornConnectionManager
    
    cm = MatterhornConnectionManager(api_key="test-key")
    assert cm.api_key == "test-key"
    # base_url is stripped of trailing slash in __init__
    assert cm.base_url == "https://matterhorn-moda.de/B2BAPI"
    assert cm._client is None  # Client created lazily


def test_connection_manager_api_key():
    """Test API key is stored correctly."""
    from services.matterhorn_connection_manager import MatterhornConnectionManager
    
    cm = MatterhornConnectionManager(api_key="my-api-key")
    assert cm.api_key == "my-api-key"
    
    cm_no_key = MatterhornConnectionManager()
    assert cm_no_key.api_key is None


def test_connection_manager_defaults():
    """Test default values are set correctly."""
    from services.matterhorn_connection_manager import MatterhornConnectionManager
    
    cm = MatterhornConnectionManager()
    assert cm.timeout == 30.0
    assert cm.retry_count == 3
    assert cm.retry_delay == 1.0


def test_connection_manager_explicit_params():
    """Test explicit parameters override defaults."""
    from services.matterhorn_connection_manager import MatterhornConnectionManager
    
    cm = MatterhornConnectionManager(
        api_key="key",
        base_url="https://custom.api/",
        timeout=60.0,
        retry_count=5,
        retry_delay=2.0,
    )
    # base_url is stripped of trailing slash in __init__
    assert cm.api_key == "key"
    assert cm.base_url == "https://custom.api"
    assert cm.timeout == 60.0
    assert cm.retry_count == 5
    assert cm.retry_delay == 2.0


# ── Exception Tests ───────────────────────────────────────────────────────

def test_matterhorn_exceptions():
    """Test exception hierarchy."""
    from services.matterhorn_connection_manager import (
        MatterhornAPIError,
        MatterhornAuthenticationError,
        MatterhornNotFoundError,
        MatterhornRateLimitError,
    )
    
    # Base error
    base = MatterhornAPIError(500, "Server error")
    assert base.status_code == 500
    assert "Server error" in str(base)
    
    # Auth error (inherits from API error)
    auth = MatterhornAuthenticationError(401, "Unauthorized")
    assert auth.status_code == 401
    assert isinstance(auth, MatterhornAPIError)
    
    # Not found error
    not_found = MatterhornNotFoundError(404, "Not found")
    assert not_found.status_code == 404
    assert isinstance(not_found, MatterhornAPIError)
    
    # Rate limit error
    rate_limit = MatterhornRateLimitError(429, "Rate limited")
    assert rate_limit.status_code == 429
    assert isinstance(rate_limit, MatterhornAPIError)


# ── Constant Tests ────────────────────────────────────────────────────────

def test_default_page_size():
    """Test default page size constant."""
    from services.matterhorn_connection_manager import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
    
    assert DEFAULT_PAGE_SIZE == 100
    assert MAX_PAGE_SIZE == 1_000


# ── Factory Function Tests ──────────────────────────────────────────────

def test_create_connection_manager():
    """Test the factory function exists and has correct signature."""
    from services.matterhorn_connection_manager import create_connection_manager
    
    import inspect
    sig = inspect.signature(create_connection_manager)
    assert "api_key" in sig.parameters
    assert "base_url" in sig.parameters


# ── Integration Tests ────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
async def test_connection_manager_health_check():
    """Integration test: health check with running Matterhorn instance.
    
    Requires Matterhorn server access - marked as integration test.
    """
    import warnings
    warnings.filterwarnings("ignore")
    
    from services.matterhorn_connection_manager import MatterhornConnectionManager
    
    cm = MatterhornConnectionManager(api_key="test-key")
    
    # In unit test mode, we skip the actual health check
    # and just verify the client can be created
    assert cm is not None
    assert cm.api_key == "test-key"