"""Core IAM/RBAC primitives (incremental, compatible with existing session auth).

Provides DB-backed users, roles, permissions, and password hashing helpers.
Existing cookie-session/API-key auth in main.py remains functional; this module enables
progressive hardening and RBAC enforcement.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone
from typing import Any

from database import (
    iam_create_user, iam_get_user_by_email, iam_get_user, iam_list_users, iam_update_user,
    iam_ensure_role, iam_assign_role, iam_list_roles, iam_list_permissions, iam_grant_permission,
)

DEFAULT_ROLES = {
    "SUPERADMIN": ["*"],
    "ADMIN": [
        "system.read", "system.configure", "users.manage", "roles.manage", "sessions.manage",
        "audit.read", "products.read", "products.write", "products.delete", "pricing.read",
        "pricing.write", "bundles.read", "bundles.manage", "ai.generate", "ai.configure",
        "matterhorn.read", "matterhorn.configure", "matterhorn.import", "shopware.read",
        "shopware.configure", "shopware.export", "connectors.read", "connectors.configure",
        "connectors.activate", "accounts.manage", "mapping.manage", "exports.execute",
        "exports.cancel", "exports.retry", "competitor.read", "competitor.manage",
        "trends.read", "trends.manage", "fulfillment.read", "fulfillment.manage",
        "finance.read", "finance.write",
    ],
    "MANAGER": [
        "system.read", "products.read", "products.write", "pricing.read", "pricing.write",
        "bundles.read", "bundles.manage", "matterhorn.read", "matterhorn.import",
        "shopware.read", "connectors.read", "connectors.configure", "exports.execute",
        "competitor.read", "competitor.manage", "trends.read", "trends.manage",
        "fulfillment.read", "fulfillment.manage", "finance.read",
    ],
    "EDITOR": [
        "system.read", "products.read", "products.write", "pricing.read", "bundles.read",
        "ai.generate", "matterhorn.read", "connectors.read", "fulfillment.read",
    ],
    "ANALYST": [
        "system.read", "audit.read", "products.read", "pricing.read", "bundles.read",
        "shopware.read", "connectors.read", "competitor.read", "trends.read",
        "fulfillment.read", "finance.read",
    ],
    "ACCOUNTANT": ["system.read", "finance.read", "finance.write", "audit.read"],
    "VIEWER": [
        "system.read", "products.read", "pricing.read", "bundles.read",
        "connectors.read", "fulfillment.read",
    ],
}


def hash_password(password: str, iterations: int = 200_000) -> str:
    if not password:
        raise ValueError("password required")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(iterations, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, it_s, salt_b64, hash_b64 = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        it = int(it_s)
        salt = base64.b64decode(salt_b64.encode())
        expected = base64.b64decode(hash_b64.encode())
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, it)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


async def bootstrap_defaults() -> None:
    for role, perms in DEFAULT_ROLES.items():
        await iam_ensure_role(role, description=f"Default role {role}")
        for p in perms:
            await iam_grant_permission(role, p)
