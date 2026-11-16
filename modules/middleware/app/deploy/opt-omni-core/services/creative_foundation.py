"""Multimodal Creative Engine foundation.

Provides: dynamic capability registry (from real configured providers/models, no hardcoding),
DAM/media asset registration (Matterhorn originals preserved/immutable), model identity profile
seeding/management (incl. Luna/Raven/Nyx from canonical references), and product-fidelity/QC
records + scoring helpers. Designed to be wired into OmniRoute client/provider & model registry
and callable by the Control Agent tool layer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any

from database import (
    creative_job_create,
    creative_job_get,
    creative_job_list,
    creative_job_update,
    creative_project_list,
    creative_project_upsert,
    fidelity_check_create,
    fidelity_check_list,
    media_asset_create,
    media_asset_list,
    model_profile_get,
    model_profile_list,
    model_profile_upsert,
)

logger = logging.getLogger(__name__)

# ── Capability taxonomy (labels, not hardcoded providers/models) ──────────
CAPABILITY_CATEGORIES = [
    "text_generation",
    "image_generation",
    "image_editing",
    "image_analysis",
    "image_upscaling",
    "background_removal",
    "inpainting",
    "outpainting",
    "text_to_video",
    "image_to_video",
    "video_to_video",
    "video_analysis",
    "audio_generation",
    "speech_generation",
    "transcription",
    "multimodal_understanding",
]


def _loads(v: Any, default: Any):
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except Exception:
            return default
    return default


class CreativeCapabilityRegistry:
    """Dynamic capability registry built from real configured provider/model state.

    No provider or model names are hardcoded; resolution is based on capabilities and
    availability reported by ProviderManager/ModelRegistry and/or DB config.
    """

    @staticmethod
    def from_runtime(runtime: Any = None) -> dict[str, Any]:
        providers = []
        models = []
        pm = getattr(runtime, "omniroute_provider_manager", None) if runtime else None
        mr = getattr(runtime, "omniroute_model_registry", None) if runtime else None
        try:
            if pm and hasattr(pm, "list_providers"):
                for p in pm.list_providers():
                    providers.append(p.to_dict() if hasattr(p, "to_dict") else getattr(p, "__dict__", {}))
            if mr and hasattr(mr, "list_models"):
                for m in mr.list_models(active_only=False):
                    models.append(m.to_dict() if hasattr(m, "to_dict") else getattr(m, "__dict__", {}))
        except Exception:
            logger.exception("Failed to read runtime provider/model registries")
        return {"providers": providers, "models": models, "capability_taxonomy": CAPABILITY_CATEGORIES}

    @staticmethod
    def resolve_model(runtime: Any, capability: str, *, prefer_low_cost: bool = False) -> dict[str, Any] | None:
        """Select an available model supporting capability from real registries (dynamic)."""
        reg = CreativeCapabilityRegistry.from_runtime(runtime)
        candidates = []
        for m in reg["models"]:
            caps = m.get("capabilities") or []
            if capability in caps and m.get("is_active", True):
                candidates.append(m)
        if not candidates:
            return None
        # basic dynamic choice: prefer healthy/active provider priority if known, else cost/latency hints
        if prefer_low_cost:
            candidates.sort(key=lambda m: (float(m.get("input_price_per_1k") or 0), float(m.get("image_price_per_image") or 0)))
        else:
            candidates.sort(key=lambda m: (-(m.get("context_window") or 0), float(m.get("input_price_per_1k") or 0)))
        chosen = candidates[0]
        return {
            "capability": capability,
            "provider_id": chosen.get("provider_id"),
            "model_id": chosen.get("model_id") or chosen.get("id"),
            "model_name": chosen.get("model_name"),
            "source": "runtime_registry",
            "candidates": len(candidates),
            "model": chosen,
        }

    @staticmethod
    async def route_with_fallback(runtime: Any, capability: str, *, request: dict[str, Any] | None = None) -> dict[str, Any]:
        """Capability-based routing with availability/cost-aware fallback (no hardcoded provider)."""
        reg = CreativeCapabilityRegistry.from_runtime(runtime)
        candidates = [m for m in reg["models"] if capability in (m.get("capabilities") or []) and m.get("is_active", True)]
        if not candidates:
            return {"ok": False, "capability": capability, "reason": "no_active_model_for_capability", "candidates": []}
        def cost_key(m: dict[str, Any]) -> float:
            return float(m.get("input_price_per_1k") or 0) + float(m.get("output_price_per_1k") or 0) + float(m.get("image_price_per_image") or 0)
        candidates.sort(key=lambda m: (cost_key(m), -(m.get("context_window") or 0)))
        req = request or {}
        units = float(req.get("estimated_units") or 1)
        def est(m: dict[str, Any]) -> dict[str, Any]:
            unit = (float(m.get("image_price_per_image") or 0) or (float(m.get("input_price_per_1k") or 0) + float(m.get("output_price_per_1k") or 0)) or 0)
            return {"currency": "USD", "unit_cost": unit, "estimated_units": units, "estimated_total": round(unit * units, 6)}
        selected = candidates[0]
        return {
            "ok": True,
            "capability": capability,
            "selected": {"provider_id": selected.get("provider_id"), "model_id": selected.get("model_id") or selected.get("id"), "model_name": selected.get("model_name"), "cost": est(selected)},
            "fallbacks": [{"provider_id": c.get("provider_id"), "model_id": c.get("model_id") or c.get("id"), "model_name": c.get("model_name"), "cost": est(c)} for c in candidates[1:5]],
            "estimated_cost": est(selected),
            "policy": "capability_match_cost_aware",
            "candidates_count": len(candidates),
        }


class MediaDAM:
    """Digital Asset Management helpers. Preserves Matterhorn/original images as immutable."""

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    @classmethod
    async def register_file(
        cls,
        path: str | os.PathLike[str],
        *,
        asset_type: str = "image",
        source_kind: str = "generated",
        immutable_original: bool = False,
        product_sku: str = "",
        model_profile_id: str = "",
        job_id: str | None = None,
        parent_asset_id: str = "",
        metadata: dict[str, Any] | None = None,
        url: str = "",
    ) -> dict[str, Any]:
        p = Path(path)
        exists = p.exists() and p.is_file()
        sha = cls._hash_file(p) if exists else ""
        size = p.stat().st_size if exists else None
        mime = mimetypes.guess_type(str(p))[0] or ""
        meta = dict(metadata or {})
        meta.setdefault("registered_from_path", str(p))
        meta.setdefault("exists", exists)
        # never overwrite originals: immutable_original assets are recorded as-is
        return await media_asset_create({
            "asset_type": asset_type,
            "source_kind": source_kind,
            "immutable_original": immutable_original,
            "storage_path": str(p),
            "url": url,
            "sha256": sha,
            "mime_type": mime,
            "size_bytes": size,
            "product_sku": product_sku,
            "model_profile_id": model_profile_id,
            "job_id": job_id,
            "parent_asset_id": parent_asset_id,
            "metadata_json": meta,
        })

    @classmethod
    async def register_matterhorn_original(
        cls, storage_path: str, *, product_sku: str = "", source_url: str = "", metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        meta = {"provider": "matterhorn", **(metadata or {})}
        if source_url:
            meta["source_url"] = source_url
        return await cls.register_file(
            storage_path,
            asset_type="image",
            source_kind="original",
            immutable_original=True,
            product_sku=product_sku,
            metadata=meta,
            url=source_url,
        )

    @staticmethod
    async def list_assets(limit: int = 200, product_sku: str | None = None, job_id: str | None = None):
        return await media_asset_list(limit=limit, product_sku=product_sku, job_id=job_id)


class ModelIdentityManager:
    """Model/identity profiles (persisted). Seeds canonical profiles incl. Luna/Raven/Nyx."""

    @staticmethod
    async def seed_canonical_profiles() -> list[dict[str, Any]]:
        try:
            from services.model_reference import MODEL_MANIFEST, get_github_raw_url, get_local_reference_path
        except Exception:
            MODEL_MANIFEST, get_github_raw_url, get_local_reference_path = {}, (lambda n: ""), (lambda n, v=None: "")
        seeded = []
        for slug, defn in (MODEL_MANIFEST or {}).items():
            rec = await model_profile_upsert({
                "slug": slug,
                "display_name": defn.get("displayName") or slug.title(),
                "description": defn.get("description") or f"Canonical AI model profile: {slug}",
                "reference_images_json": [{"type": "reference_pack", "url": get_github_raw_url(slug), "local_path": get_local_reference_path(slug), "views": defn.get("views", [])}],
                "identity_profile_json": {"reference_type": defn.get("referenceType", "identity"), "canonical": True},
                "rights_consent_json": {"status": "unverified", "note": "Consent/rights must be documented before production use."},
                "usage_allowed": bool(defn.get("active", True)),
                "active": bool(defn.get("active", True)),
                "history_json": [{"event": "seeded_from_canonical_reference", "source": "services.model_reference"}],
            })
            seeded.append(rec)
        return seeded

    @staticmethod
    async def list_profiles(limit: int = 200):
        return await model_profile_list(limit=limit)

    @staticmethod
    async def get_profile(slug: str):
        return await model_profile_get(slug)

    @staticmethod
    async def upsert_profile(data: dict[str, Any]):
        # append history entry if provided/changed
        if "history_entry" in data:
            existing = await model_profile_get(str(data.get("slug") or ""))
            hist = _loads((existing or {}).get("history_json"), [])
            if not isinstance(hist, list):
                hist = []
            hist.append(data.pop("history_entry"))
            data["history_json"] = hist
        return await model_profile_upsert(data)


class ProductFidelityEngine:
    """Product fidelity/QC record helpers.

    Stores structured checks (color/material/pattern/cut/logos/seams/details/count/outfit combo).
    Automated scoring hooks can be plugged in later; without a configured vision provider we
    record a manual-review-required result (no fabricated pass).
    """

    CRITERIA = ["color", "material", "pattern", "cut", "logos", "seams", "details", "item_count", "outfit_combination"]

    @classmethod
    async def create_check(
        cls,
        *,
        job_id: str | None,
        output_asset_id: str,
        product_sku: str,
        original_asset_ids: list[str] | None = None,
        scores: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
        vision_provider_configured: bool = False,
    ) -> dict[str, Any]:
        scores = scores or {}
        # if no automated scorer, do not fabricate a pass
        if not scores or not vision_provider_configured:
            overall = 0.0
            verdict = "manual_review"
            critical = ["automated_scoring_unavailable"]
            details = {**(details or {}), "note": "No configured vision scorer; manual QC required. Original Matterhorn references preserved."}
        else:
            vals = [float(v) for v in scores.values() if isinstance(v, (int, float))]
            overall = sum(vals) / len(vals) if vals else 0.0
            critical = [k for k, v in scores.items() if isinstance(v, (int, float)) and v < 0.6]
            verdict = "fail" if critical else ("pass" if overall >= 0.8 else "manual_review")
        return await fidelity_check_create({
            "job_id": job_id,
            "output_asset_id": output_asset_id,
            "product_sku": product_sku,
            "original_asset_ids_json": original_asset_ids or [],
            "scores_json": scores,
            "overall_score": overall,
            "verdict": verdict,
            "critical_issues_json": critical,
            "details_json": details or {},
        })

    @staticmethod
    async def list_checks(limit: int = 200, job_id: str | None = None):
        return await fidelity_check_list(limit=limit, job_id=job_id)


class CreativeJobService:
    """Lightweight job/project persistence helpers (execution/orchestration layered above)."""

    @staticmethod
    async def create_project(data: dict[str, Any]):
        return await creative_project_upsert(data)

    @staticmethod
    async def list_projects(limit: int = 100):
        return await creative_project_list(limit=limit)

    @staticmethod
    async def create_job(data: dict[str, Any]):
        return await creative_job_create(data)

    @staticmethod
    async def update_job(job_id: str, **fields: Any):
        return await creative_job_update(job_id, **fields)

    @staticmethod
    async def get_job(job_id: str):
        return await creative_job_get(job_id)

    @staticmethod
    async def list_jobs(limit: int = 100, project_id: str | None = None):
        return await creative_job_list(limit=limit, project_id=project_id)
