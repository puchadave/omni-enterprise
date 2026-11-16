"""Multimodal creative agent stubs (image/editing/video/audio/analysis) with safe orchestration.

All write/generation actions are plannable and executable as creative jobs with capability-based
routing via configured providers/models. If required capability/provider is not configured or
execution is unavailable, jobs are recorded as review_required/not_executed without fabricated
outputs. Matterhorn/original assets must never be overwritten.
"""

from __future__ import annotations

import random
from typing import Any

from services.creative_foundation import (
    CreativeCapabilityRegistry,
    CreativeJobService,
    MediaDAM,
    ModelIdentityManager,
    ProductFidelityEngine,
)
from services.ai_photoshooting import ImageGenerationAgent, ProductPhotographyAgent, _resolve_provider, _now


async def _execute_generic_job(
    *,
    job_type: str,
    capability: str,
    prompt: str,
    plan: dict[str, Any],
    actor: str,
    project_id: str | None,
    runtime: Any,
    product_sku: str = "",
    variants: int = 1,
) -> dict[str, Any]:
    variants = max(1, min(int(variants or 1), 8))
    provider, prov_info = await _resolve_provider(runtime)
    route = plan.get("routing") or await CreativeCapabilityRegistry.route_with_fallback(runtime, capability)
    selected = (route.get("selected") or {}) if isinstance(route, dict) else {}
    est_cost = route.get("estimated_cost") if isinstance(route, dict) else None
    job = await CreativeJobService.create_job({
        "project_id": project_id,
        "job_type": job_type,
        "capability": capability,
        "status": "running",
        "provider_id": selected.get("provider_id") or "",
        "model_id": selected.get("model_id") or "",
        "prompt": prompt,
        "input_json": {"plan": plan},
        "output_json": {},
        "cost_json": {"estimated": est_cost} if est_cost else {},
        "created_by": actor,
        "started_at": _now(),
    })
    outputs = []
    # Try generation only when configured and capability appears routable; provider impl currently image-focused,
    # so for non-image we still avoid fabricating outputs and mark unsupported/not_configured.
    can_attempt = bool(prov_info.get("configured") and route.get("ok") and capability in {"image_generation", "image_editing", "image_upscaling", "background_removal", "inpainting", "outpainting"})
    for i in range(variants):
        gen: dict[str, Any]
        asset_rec = None
        if can_attempt:
            gen = await provider.generate({"prompt": prompt, "seed": random.randint(0, 2**31 - 1), "capability": capability})
            if not gen.get("error") and gen.get("image_url"):
                asset_rec = await MediaDAM.register_file(
                    f"media/generated/{job['id']}_{i+1}.url",
                    asset_type="image" if capability.startswith("image") or capability in {"background_removal","inpainting","outpainting","image_upscaling","image_editing"} else "file",
                    source_kind="generated",
                    product_sku=product_sku,
                    job_id=job["id"],
                    metadata={"generation": gen, "variant": i + 1, "capability": capability, "materialized": False},
                    url=str(gen.get("image_url") or ""),
                )
            elif not gen.get("error"):
                gen = {**gen, "status": "not_executed", "reason": "provider_returned_no_output"}
        else:
            reason = prov_info.get("reason") or (route.get("reason") if isinstance(route, dict) else None) or "capability_or_provider_not_configured"
            gen = {"status": "not_executed", "reason": reason, "configured": prov_info.get("configured", False), "capability": capability}
        outputs.append({"variant": i + 1, "generation": gen, "asset": asset_rec})

    status = "succeeded" if any(o.get("asset") for o in outputs) else ("review_required" if not can_attempt else "failed")
    job = await CreativeJobService.update_job(
        job["id"], status=status, output_json={"outputs": outputs, "provider": prov_info, "routing": route},
        error_message="" if status != "failed" else "execution_failed_or_not_configured", completed_at=_now(), requires_review=(status != "succeeded"),
    ) or job
    return {"job": job, "outputs": outputs, "status": status, "routing": route, "provider": prov_info}


class CreativeOrchestrator:
    """Plans/selects specialized agents by capability; execution via jobs with confirmation."""

    @staticmethod
    async def plan(capability: str, **kwargs: Any) -> dict[str, Any]:
        runtime = kwargs.get("runtime")
        route = await CreativeCapabilityRegistry.route_with_fallback(runtime, capability, request=kwargs)
        base = {
            "capability": capability,
            "routing": route,
            "executable": bool(route.get("ok")),
            "notes": [] if route.get("ok") else ["No configured model/provider for capability; execution will not fabricate outputs."],
        }
        if capability == "image_generation" and kwargs.get("agent") == "product_photography":
            return await ProductPhotographyAgent.plan(
                kwargs.get("product_sku", ""), models=kwargs.get("models") or [], scene=kwargs.get("scene", "studio"),
                shot_type=kwargs.get("shot_type", "product"), variants=kwargs.get("variants", 2), pose=kwargs.get("pose", ""),
                camera=kwargs.get("camera", ""), lighting=kwargs.get("lighting", ""), aspect_ratio=kwargs.get("aspect_ratio", "4:5"), runtime=runtime,
            )
        if capability == "image_generation":
            return await ImageGenerationAgent.plan(
                kwargs.get("prompt", ""), negative_prompt=kwargs.get("negative_prompt", ""), variants=kwargs.get("variants", 1),
                product_sku=kwargs.get("product_sku", ""), model_profile_id=kwargs.get("model_profile_id", ""), runtime=runtime,
            )
        # generic stubs for other capabilities
        agent_map = {
            "image_editing": "ImageEditingAgent", "image_upscaling": "ImageUpscalingAgent", "background_removal": "BackgroundRemovalAgent",
            "inpainting": "InpaintingAgent", "outpainting": "OutpaintingAgent", "image_analysis": "ImageAnalysisAgent",
            "text_to_video": "VideoGenerationAgent", "image_to_video": "VideoGenerationAgent", "video_to_video": "VideoEditingAgent",
            "video_analysis": "VideoAnalysisAgent", "audio_generation": "AudioGenerationAgent", "speech_generation": "SpeechGenerationAgent",
            "transcription": "TranscriptionAgent", "multimodal_understanding": "MediaIntelligenceAgent", "text_generation": "TextGenerationAgent",
        }
        return {**base, "agent": agent_map.get(capability, "CreativeAgent"), "prompt": kwargs.get("prompt", ""), "negative_prompt": kwargs.get("negative_prompt", ""), "variants": kwargs.get("variants", 1), "product_sku": kwargs.get("product_sku", ""), "input": {k: v for k, v in kwargs.items() if k not in {"runtime"}}}

    @staticmethod
    async def execute(plan: dict[str, Any], *, actor: str = "system", project_id: str | None = None, runtime: Any = None) -> dict[str, Any]:
        cap = plan.get("capability") or "image_generation"
        agent = plan.get("agent")
        if agent == "ProductPhotographyAgent" or plan.get("shot_type"):
            return await ProductPhotographyAgent.execute_plan(plan, actor=actor, project_id=project_id, runtime=runtime)
        if cap == "image_generation":
            return await ImageGenerationAgent.execute_plan(plan, actor=actor, project_id=project_id, runtime=runtime)
        job_type = {"text_to_video": "video_generation", "image_to_video": "video_generation", "video_to_video": "video_editing"}.get(cap, cap)
        return await _execute_generic_job(job_type=job_type, capability=cap, prompt=str(plan.get("prompt") or ""), plan=plan, actor=actor, project_id=project_id, runtime=runtime, product_sku=str(plan.get("product_sku") or ""), variants=int(plan.get("variants") or 1))
