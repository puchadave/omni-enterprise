"""Model Reference System for Uptempo Clubwear AI Photoshooting.

Manages model identities, references, and provides URLs for OmniRoute integration.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("omni.models")

# ---------------------------------------------------------------------------
# Konfigurierte Model-Liste (verbindlich)
# ---------------------------------------------------------------------------
# Quelle: Codex-Bild 3. Sept. 2026, 13_32_38.png
MODEL_NAMES = [
    "david", "luna", "raven", "nyx", "jade", "sera",
    "alex", "kim", "lina", "nika", "sarah",
]

# Zentrale Referenz auf alle Models
MODEL_MANIFEST: Dict[str, Dict[str, Any]] = {}
for name in MODEL_NAMES:
    MODEL_MANIFEST[name] = {
        "displayName": name.capitalize(),
        "referenceType": "identity",
        "views": ["front", "45_degree", "profile", "back", "expressions", "360_reference"],
        "active": True,
    }

# Pfad-Strukturen
# Lokale Entwicklung:
LOCAL_ASSETS_DIR = "assets/ai/models"
# GitHub Raw URLs (werden nach Commit generiert):
# https://raw.githubusercontent.com/puchadave/UptempoClubwear.de/main/assets/ai/models/<model>/reference-pack.png

# OmniRoute interne Pfade (nach Asset-Transfer)
OMNIINTERNAL_MODEL_DIR = "omniroute/models"


def get_model_names() -> List[str]:
    """Return the canonical list of 11 model names."""
    return list(MODEL_MANIFEST.keys())


def get_model(name: str) -> Optional[Dict[str, Any]]:
    """Return model definition by name, None if not found."""
    return MODEL_MANIFEST.get(name)


def get_all_models() -> Dict[str, Dict[str, Any]]:
    """Return all model definitions."""
    return dict(MODEL_MANIFEST)


def is_model_active(name: str) -> bool:
    """Check if a model is active."""
    model = get_model(name)
    return model is not None and model.get("active", False)


def get_active_models() -> Dict[str, Dict[str, Any]]:
    """Return all active models."""
    return {k: v for k, v in MODEL_MANIFEST.items() if v.get("active", False)}


def get_display_name(model_name: str) -> str:
    """Get the display name for a model."""
    model = get_model(model_name)
    if model:
        return model.get("displayName", model_name.capitalize())
    return model_name.capitalize()


# ---------------------------------------------------------------------------
# Pfad-Generierung
# ---------------------------------------------------------------------------

def get_local_reference_path(model_name: str, view: str | None = None) -> str:
    """Get the local file path for a model reference image.

    Development path: assets/ai/models/<model>/reference-pack.png
    """
    model_lower = model_name.lower()
    if model_lower not in MODEL_MANIFEST:
        model_lower = "david"  # fallback
    if view:
        return f"{LOCAL_ASSETS_DIR}/{model_lower}/{view}.png"
    return f"{LOCAL_ASSETS_DIR}/{model_lower}/reference-pack.png"


def get_github_raw_url(model_name: str) -> str:
    """Get the public GitHub Raw URL for a model reference.

    Format: https://raw.githubusercontent.com/puchadave/UptempoClubwear.de/main/assets/ai/models/<model>/reference-pack.png
    """
    model_lower = model_name.lower()
    if model_lower not in MODEL_MANIFEST:
        model_lower = "david"
    return f"https://raw.githubusercontent.com/puchadave/UptempoClubwear.de/main/assets/ai/models/{model_lower}/reference-pack.png"


def get_omniroute_internal_path(model_name: str) -> str:
    """Get the internal path for OmniRoute model reference transfer.

    Used when moving assets into the OmniRoute system.
    """
    model_lower = model_name.lower()
    if model_lower not in MODEL_MANIFEST:
        model_lower = "david"
    return f"{OMNIINTERNAL_MODEL_DIR}/{model_lower}/reference-pack.png"


# ---------------------------------------------------------------------------
# Manifest Export/Import
# ---------------------------------------------------------------------------

def export_manifest(path: str = "assets/ai/model-reference-manifest.json") -> None:
    """Export the model manifest to a JSON file.

    Creates the directory structure if it doesn't exist.
    """
    # Ensure directory exists
    manifest_dir = os.path.dirname(path)
    if manifest_dir:
        os.makedirs(manifest_dir, exist_ok=True)

    export_data = {
        "version": "1.0.0",
        "repository": "puchadave/UptempoClubwear.de",
        "generatedAt": datetime.now(timezone.utc).isoformat() + "Z",
        "models": {},
    }

    for name, definition in MODEL_MANIFEST.items():
        export_data["models"][name] = {
            "name": definition.get("displayName", name.capitalize()),
            "referenceType": definition.get("referenceType", "identity"),
            "referenceUrl": get_github_raw_url(name),
            "referencePackLocalPath": get_local_reference_path(name),
            "views": definition.get("views", []),
            "active": definition.get("active", True),
        }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(export_data, f, indent=2, ensure_ascii=False)

    logger.info(f"Model manifest exported to {path}")
    return export_data


def import_manifest(path: str) -> Dict[str, Any]:
    """Import a model manifest from a JSON file.

    Replaces the default manifest with the imported one.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            imported = json.load(f)

        # Update global manifest
        global MODEL_MANIFEST
        MODEL_MANIFEST = {}

        for name, data in imported.get("models", {}).items():
            MODEL_MANIFEST[name] = {
                "displayName": data.get("name", name.capitalize()),
                "referenceType": data.get("referenceType", "identity"),
                "views": data.get("views", ["front", "45_degree", "profile", "back", "expressions", "360_reference"]),
                "active": data.get("active", True),
            }

        logger.info(f"Model manifest imported from {path} with {len(MODEL_MANIFEST)} models")
        return MODEL_MANIFEST
    except Exception as e:
        logger.error(f"Failed to import model manifest: {e}")
        return MODEL_MANIFEST


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------

def validate_model_name(name: str) -> bool:
    """Validate that a model name is in the canonical list."""
    return name.lower() in MODEL_MANIFEST


def get_all_model_names() -> List[str]:
    """Return sorted list of all model names."""
    return sorted(MODEL_MANIFEST.keys())


def get_model_legacy(model_name: str, fallback: str = "david") -> Optional[Dict[str, Any]]:
    """Get model definition by name with fallback."""
    return MODEL_MANIFEST.get(model_name.lower(), MODEL_MANIFEST.get(fallback, {}))


def get_model_display_name(model_name: str) -> str:
    """Get display name for a model (backward compatibility)."""
    return get_display_name(model_name)


def get_model_views(model_name: str) -> List[str]:
    """Get available views for a model."""
    model = get_model(model_name)
    if model:
        return model.get("views", [])
    return []


# Alias functions for template/UI compatibility
def get_model_key(model_name: str) -> str:
    """Get the canonical key for a model."""
    return model_name.lower()


def model_exists(model_name: str) -> bool:
    """Check if a model key exists in the manifest."""
    return model_name.lower() in MODEL_MANIFEST