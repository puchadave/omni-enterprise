"""Multimodal Creative / AI Photoshooting agents (foundation, step 2).

Provides ImageGenerationAgent and ProductPhotographyAgent that plan and execute creative jobs
using the Creative Engine foundation (capability routing, DAM, model identity, fidelity checks).
Generations run via configured OmniRoute/provider capabilities when available; otherwise jobs are
recorded with a not-configured outcome (no fabricated outputs). Matterhorn/original images are
never overwritten.
"""

from __future__ import annotations

import json
import logging
import os
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from database import fetch_product, get_setting, utc_now
from services.creative_foundation import (
    CreativeCapabilityRegistry,
    CreativeJobService,
    MediaDAM,
    ModelIdentityManager,
    ProductFidelityEngine,
)

logger = logging.getLogger(__name__)

GENERATED_DIR = Path("media/generated")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Provider interface (backward compatible) ──────────────────────────────

class ImageGenerationProvider:
    async def generate(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def health_check(self) -> bool:
        return False


class OmniRouteProvider(ImageGenerationProvider):
    """Delegates to OmniRoute gateway if configured."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None, timeout: int = 30) -> None:
        self.base_url = (base_url or os.environ.get("OMNIROUTE_BASE_URL") or "").rstrip("/")
        self.api_key = api_key or os.environ.get("OMNIROUTE_API_KEY") or ""
        self.model = model or os.environ.get("OMNIROUTE_IMAGE_MODEL") or ""
        self.timeout = timeout

    async def generate(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url or not self.model:
            return {"error": "omniroute_not_configured", "configured": False}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {
            "model": self.model,
            "prompt": request.get("prompt", ""),
            "negative_prompt": request.get("negative_prompt", ""),
            "seed": request.get("seed", random.randint(0, 2**31 - 1)),
        }
        endpoint = request.get("endpoint") or "/v1/images/generations"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                async with session.post(f"{self.base_url}{endpoint}", json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        return {"error": f"http_{resp.status}", "configured": True}
                    data = await resp.json()
                    return {"image_url": data.get("url") or data.get("image"), "provider": "omniroute", "raw": data, "configured": True}
        except Exception as exc:
            logger.warning("OmniRoute image generation failed: %s", exc)
            return {"error": str(exc), "configured": True}

    async def health_check(self) -> bool:
        if not self.base_url:
            return False
        try:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.get(f"{self.base_url}/models", headers=headers) as resp:
                    return resp.status == 200
        except Exception:
            return False


class LocalPlaceholderProvider(ImageGenerationProvider):
    """Explicit placeholder/simulation (no real generation)."""

    async def generate(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"simulated": True, "provider": "local_placeholder", "configured": False, "image_url": "", "note": "No image provider configured; generation not executed."}

    async def health_check(self) -> bool:
        return True


_default_provider: ImageGenerationProvider | None = None


def get_provider() -> ImageGenerationProvider:
    global _default_provider
    if _default_provider is None:
        _default_provider = OmniRouteProvider()
    return _default_provider


def set_provider(provider: ImageGenerationProvider) -> None:
    global _default_provider
    _default_provider = provider


async def _resolve_provider(runtime: Any = None) -> tuple[ImageGenerationProvider, dict[str, Any]]:
    base = await get_setting("omniroute_url") or await get_setting("omni_route_base_url") or os.environ.get("OMNIROUTE_BASE_URL") or ""
    key = await get_setting("omniroute_api_key") or await get_setting("omni_route_api_key") or os.environ.get("OMNIROUTE_API_KEY") or ""
    model = await get_setting("omniroute_image_model") or os.environ.get("OMNIROUTE_IMAGE_MODEL") or ""
    if base and model:
        return OmniRouteProvider(base, key, model), {"configured": True, "base_url": base, "model": model}
    return LocalPlaceholderProvider(), {"configured": False, "reason": "omniroute_image_not_configured"}


# ── Agents ───────────────────────────────────────────────────────────────

class ImageGenerationAgent:
    capability = "image_generation"

    @classmethod
    async def plan(
        cls,
        prompt: str,
        *,
        negative_prompt: str = "",
        variants: int = 1,
        product_sku: str = "",
        model_profile_id: str = "",
        references: list[str] | None = None,
        runtime: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        variants = max(1, min(int(variants or 1), 8))
        route = await CreativeCapabilityRegistry.route_with_fallback(runtime, cls.capability)
        _, prov_info = await _resolve_provider(runtime)
        model_profile = await ModelIdentityManager.get_profile(model_profile_id) if model_profile_id else None
        plan = {
            "agent": "ImageGenerationAgent",
            "capability": cls.capability,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "variants": variants,
            "product_sku": product_sku,
            "model_profile_id": model_profile_id,
            "model_profile": model_profile,
            "references": references or [],
            "routing": route,
            "provider": prov_info,
            "executable": bool(prov_info.get("configured") and route.get("ok")),
            "notes": [] if (prov_info.get("configured") and route.get("ok")) else ["Provider/model not configured or capability unavailable; execution will record not-configured outcome (no fabricated images)."],
            "extra": extra or {},
        }
        return plan

    @classmethod
    async def execute_plan(cls, plan: dict[str, Any], *, actor: str = "system", project_id: str | None = None, runtime: Any = None) -> dict[str, Any]:
        prompt = str(plan.get("prompt") or "")
        variants = max(1, min(int(plan.get("variants") or 1), 8))
        provider, prov_info = await _resolve_provider(runtime)
        route = plan.get("routing") or await CreativeCapabilityRegistry.route_with_fallback(runtime, cls.capability)
        selected = (route.get("selected") or {}) if isinstance(route, dict) else {}
        job = await CreativeJobService.create_job({
            "project_id": project_id,
            "job_type": "image_generation",
            "capability": cls.capability,
            "status": "running",
            "provider_id": selected.get("provider_id") or "omniroute",
            "model_id": selected.get("model_id") or "",
            "prompt": prompt,
            "negative_prompt": str(plan.get("negative_prompt") or ""),
            "input_json": {"plan": plan},
            "output_json": {},
            "cost_json": {},
            "created_by": actor,
            "started_at": _now(),
        })
        outputs: list[dict[str, Any]] = []
        fidelity: list[dict[str, Any]] = []
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        for i in range(variants):
            req = {"prompt": prompt, "negative_prompt": plan.get("negative_prompt", ""), "seed": random.randint(0, 2**31 - 1)}
            gen = await provider.generate(req)
            asset_rec = None
            if prov_info.get("configured") and not gen.get("error") and gen.get("image_url"):
                # register reference/URL asset (download pipeline can materialize later)
                asset_rec = await MediaDAM.register_file(
                    GENERATED_DIR / f"{job['id']}_{i+1}.url",
                    asset_type="image",
                    source_kind="generated",
                    product_sku=str(plan.get("product_sku") or ""),
                    model_profile_id=str(plan.get("model_profile_id") or ""),
                    job_id=job["id"],
                    metadata={"generation": gen, "variant": i + 1, "materialized": False},
                    url=str(gen.get("image_url") or ""),
                )
            else:
                # no fabricated output
                gen = {**gen, "status": "not_executed", "reason": gen.get("error") or prov_info.get("reason") or "not_configured"}
            out_entry = {"variant": i + 1, "generation": gen, "asset": asset_rec}
            outputs.append(out_entry)
            # fidelity check hook (manual review when no automated vision scorer configured)
            if asset_rec and plan.get("product_sku"):
                try:
                    chk = await ProductFidelityEngine.create_check(
                        job_id=job["id"], output_asset_id=str(asset_rec.get("id") or ""), product_sku=str(plan.get("product_sku")),
                        original_asset_ids=[r for r in (plan.get("references") or []) if isinstance(r, str)], scores={}, vision_provider_configured=False,
                        details={"variant": i + 1})
                    fidelity.append(chk)
                except Exception:
                    logger.exception("Fidelity check hook failed")
        status = "succeeded" if (prov_info.get("configured") and any(o.get("asset") for o in outputs)) else "failed"
        if not prov_info.get("configured"):
            status = "review_required"
        job = await CreativeJobService.update_job(
            job["id"], status=status, output_json={"outputs": outputs, "fidelity": fidelity, "provider": prov_info, "routing": route},
            error_message="" if status != "failed" else "generation_not_configured_or_failed", completed_at=_now(), requires_review=(status == "review_required"),
        ) or job
        return {"job": job, "outputs": outputs, "fidelity": fidelity, "status": status}


class ProductPhotographyAgent:
    capability = "image_generation"

    @classmethod
    async def plan(
        cls,
        product_sku: str,
        *,
        models: list[str] | None = None,
        scene: str = "studio",
        shot_type: str = "product",
        variants: int = 2,
        pose: str = "",
        camera: str = "",
        lighting: str = "",
        aspect_ratio: str = "4:5",
        runtime: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        product = await fetch_product(product_sku) if product_sku else None
        model_profiles = []
        for slug in (models or []):
            mp = await ModelIdentityManager.get_profile(slug)
            if mp:
                model_profiles.append(mp)
        # collect original/reference assets for product (Matterhorn originals preserved)
        refs = await MediaDAM.list_assets(product_sku=product_sku) if product_sku else []
        original_refs = [a for a in refs if str(a.get("source_kind")) == "original" or a.get("immutable_original")]
        prompt = (
            f"{shot_type} photography, scene={scene}, product={product.get('title') if product else product_sku}, "
            f"sku={product_sku}, aspect_ratio={aspect_ratio}, pose={pose}, camera={camera}, lighting={lighting}. "
            f"Preserve product fidelity (color, material, pattern, cut, logos, seams, details, item count)."
        )
        route = await CreativeCapabilityRegistry.route_with_fallback(runtime, cls.capability)
        _, prov_info = await _resolve_provider(runtime)
        return {
            "agent": "ProductPhotographyAgent",
            "capability": cls.capability,
            "product_sku": product_sku,
            "product": {k: (product or {}).get(k) for k in ("sku", "title", "category", "raw_description", "ai_title") if (product or {}).get(k) is not None} if product else None,
            "models": models or [],
            "model_profiles": model_profiles,
            "scene": scene,
            "shot_type": shot_type,
            "pose": pose, "camera": camera, "lighting": lighting, "aspect_ratio": aspect_ratio,
            "variants": max(1, min(int(variants or 1), 12)),
            "reference_assets": [{"id": a.get("id"), "path": a.get("storage_path"), "source_kind": a.get("source_kind"), "immutable_original": a.get("immutable_original")} for a in (original_refs or refs)[:20]],
            "prompt": prompt,
            "routing": route,
            "provider": prov_info,
            "executable": bool(prov_info.get("configured") and route.get("ok")),
            "notes": ["Matterhorn/original product images are treated as immutable references and are never overwritten."],
            "extra": extra or {},
        }

    @classmethod
    async def execute_plan(cls, plan: dict[str, Any], *, actor: str = "system", project_id: str | None = None, runtime: Any = None) -> dict[str, Any]:
        # reuse image generation per variant; model identity context carried in prompt/metadata
        prompt = str(plan.get("prompt") or "")
        variants = max(1, min(int(plan.get("variants") or 1), 2))
        provider, prov_info = await _resolve_provider(runtime)
        route = plan.get("routing") or await CreativeCapabilityRegistry.route_with_fallback(runtime, cls.capability)
        selected = (route.get("selected") or {}) if isinstance(route, dict) else {}
        job = await CreativeJobService.create_job({
            "project_id": project_id,
            "job_type": "product_photography",
            "capability": cls.capability,
            "status": "running",
            "provider_id": selected.get("provider_id") or "omniroute",
            "model_id": selected.get("model_id") or "",
            "prompt": prompt,
            "input_json": {"plan": plan},
            "output_json": {},
            "created_by": actor,
            "started_at": _now(),
        })
        outputs: list[dict[str, Any]] = []
        fidelity: list[dict[str, Any]] = []
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        original_ids = [r.get("id") for r in (plan.get("reference_assets") or []) if isinstance(r, dict) and r.get("id")]
        for i in range(variants):
            gen = await provider.generate({"prompt": prompt, "seed": random.randint(0, 2**31 - 1)})
            asset_rec = None
            if prov_info.get("configured") and not gen.get("error") and gen.get("image_url"):
                asset_rec = await MediaDAM.register_file(
                    GENERATED_DIR / f"{job['id']}_{i+1}.url",
                    asset_type="image", source_kind="generated",
                    product_sku=str(plan.get("product_sku") or ""), job_id=job["id"],
                    metadata={"generation": gen, "variant": i + 1, "scene": plan.get("scene"), "shot_type": plan.get("shot_type"), "models": plan.get("models"), "materialized": False},
                    url=str(gen.get("image_url") or ""),
                )
            else:
                gen = {**gen, "status": "not_executed", "reason": gen.get("error") or prov_info.get("reason") or "not_configured"}
            outputs.append({"variant": i + 1, "generation": gen, "asset": asset_rec})
            if asset_rec and plan.get("product_sku"):
                try:
                    chk = await ProductFidelityEngine.create_check(job_id=job["id"], output_asset_id=str(asset_rec.get("id") or ""), product_sku=str(plan.get("product_sku")), original_asset_ids=original_ids, scores={}, vision_provider_configured=False, details={"variant": i + 1, "scene": plan.get("scene")})
                    fidelity.append(chk)
                except Exception:
                    logger.exception("Fidelity check hook failed")
        status = "succeeded" if (prov_info.get("configured") and any(o.get("asset") for o in outputs)) else "review_required"
        if prov_info.get("configured") and not any(o.get("asset") for o in outputs):
            status = "failed"
        job = await CreativeJobService.update_job(job["id"], status=status, output_json={"outputs": outputs, "fidelity": fidelity, "provider": prov_info, "routing": route}, completed_at=_now(), requires_review=(status != "succeeded"), error_message="" if status != "failed" else "generation_failed") or job
        return {"job": job, "outputs": outputs, "fidelity": fidelity, "status": status}


# backward-compat aliases expected by older imports
def get_model_names() -> list[str]:
    try:
        from services.model_reference import MODEL_NAMES
        return list(MODEL_NAMES)
    except Exception:
        return []
