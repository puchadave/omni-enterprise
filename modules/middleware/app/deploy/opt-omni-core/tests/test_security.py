import hashlib

from fastapi.testclient import TestClient

import main
from main import app


def _set_auth_secrets(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-0123456789-abcdefgh")


def test_internal_api_key_required_for_protected_routes(monkeypatch):
    _set_auth_secrets(monkeypatch)

    with TestClient(app) as client:
        assert client.get("/api/stats").status_code == 401
        assert client.get("/api/export/channels").status_code == 401


def test_health_liveness_is_public_and_readiness_is_protected(monkeypatch):
    _set_auth_secrets(monkeypatch)

    with TestClient(app) as client:
        liveness = client.get("/healthz")
        assert liveness.status_code == 200
        assert liveness.json() == {"status": "ok"}
        assert client.get("/api/health").status_code == 401

        authorized = client.get("/api/health", headers={"X-Internal-API-Key": "test-internal-key"})
        assert authorized.status_code == 200
        assert "database" in authorized.json()


def test_docs_and_unknown_routes_require_authorization_and_security_headers(monkeypatch):
    _set_auth_secrets(monkeypatch)

    with TestClient(app) as client:
        docs = client.get("/docs", follow_redirects=False)
        assert docs.status_code == 303
        assert docs.headers["X-Content-Type-Options"] == "nosniff"

        unknown = client.get(
            "/api/admin/unclassified",
            headers={"X-Internal-API-Key": "test-internal-key"},
        )
        assert unknown.status_code == 403
        assert unknown.json()["required_permission"] == "system.unclassified"


def test_permission_lookup_uses_exact_root_and_head_as_get():
    assert main._required_permission("GET", "/") == "system.read"
    assert main._required_permission("GET", "/api/products-unrelated") == "system.unclassified"
    assert main._required_permission("HEAD", "/api/stats") == "system.read"
    assert not main._has_permission({"authenticated": True, "permissions": []}, None)


def test_browser_session_authenticates_api(monkeypatch):
    _set_auth_secrets(monkeypatch)

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"api_key": "test-internal-key"})
        assert login.status_code == 200
        assert client.get("/api/stats").status_code == 200


def test_invalid_browser_login_is_rejected(monkeypatch):
    _set_auth_secrets(monkeypatch)

    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"api_key": "wrong-key"}).status_code == 401


def test_login_fails_when_session_signing_secret_is_missing(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.delenv("SESSION_SECRET", raising=False)

    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={"api_key": "test-internal-key"})
        assert response.status_code == 503
        assert response.json()["detail"] == "SESSION_SECRET must contain at least 32 characters."


def test_database_session_token_hash_is_validated(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-0123456789-abcdefgh")
    token = main._issue_session_token(principal_type="user", user_id="user-1", sid="session-1")

    async def get_session(_session_id):
        return {
            "id": "session-1",
            "user_id": "user-1",
            "session_token_hash": hashlib.sha256(b"a different token").hexdigest(),
            "expires_at": None,
            "revoked": False,
        }

    async def unexpected_user_lookup(_user_id):
        raise AssertionError("A mismatched token hash must be rejected before loading the user.")

    monkeypatch.setattr(main, "iam_get_session", get_session)
    monkeypatch.setattr(main, "iam_get_user", unexpected_user_lookup)

    with TestClient(app) as client:
        client.cookies.set(main.SESSION_COOKIE_NAME, token)
        response = client.get("/api/auth/me")
        assert response.status_code == 401


def test_logout_requires_authentication_and_csrf(monkeypatch):
    _set_auth_secrets(monkeypatch)
    token = main._issue_session_token(principal_type="user", user_id="user-1", sid="session-1")

    async def get_session(_session_id):
        return {
            "id": "session-1",
            "user_id": "user-1",
            "session_token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "expires_at": None,
            "revoked": False,
        }

    async def get_user(_user_id):
        return {"id": "user-1", "email": "user@example.test", "is_active": True}

    async def get_roles(_user_id):
        return ["VIEWER"]

    async def list_permissions(role_name):
        return [{"permission": "system.read"}] if role_name == "VIEWER" else []

    async def revoke_session(_session_id):
        return None

    monkeypatch.setattr(main, "iam_get_session", get_session)
    monkeypatch.setattr(main, "iam_get_user", get_user)
    monkeypatch.setattr(main, "iam_get_user_roles", get_roles)
    monkeypatch.setattr(main, "iam_list_permissions", list_permissions)
    monkeypatch.setattr(main, "iam_revoke_session", revoke_session)

    with TestClient(app) as client:
        assert client.post("/api/auth/logout").status_code == 401
        client.cookies.set(main.SESSION_COOKIE_NAME, token)
        assert client.post("/api/auth/logout").status_code == 403

        csrf = "valid-csrf"
        client.cookies.set(main.CSRF_COOKIE_NAME, csrf)
        response = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
        assert response.status_code == 200
