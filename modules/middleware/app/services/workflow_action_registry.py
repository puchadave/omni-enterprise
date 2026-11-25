from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class WorkflowAction:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool = True
    requires_confirmation: bool = False
    destructive: bool = False
    allowed_roles: list[str] = field(default_factory=lambda: ["admin", "operator"])
    handler: Callable[..., Awaitable[dict[str, Any]]] | None = None


class WorkflowActionRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, WorkflowAction] = {}

    def register(self, action: WorkflowAction) -> None:
        self._actions[action.name] = action

    def get(self, name: str) -> WorkflowAction | None:
        return self._actions.get(name)

    def list_actions(self) -> list[dict[str, Any]]:
        return [{
            "name": a.name, "description": a.description, "input_schema": a.input_schema,
            "read_only": a.read_only, "requires_confirmation": a.requires_confirmation,
            "destructive": a.destructive, "allowed_roles": a.allowed_roles,
        } for a in self._actions.values()]

    def validate_step(self, step: dict[str, Any]) -> tuple[bool, list[str]]:
        errors = []
        name = str(step.get("action") or "")
        action = self.get(name)
        if not action:
            return False, [f"unknown_action:{name}"]
        params = step.get("params") if isinstance(step.get("params"), dict) else {}
        required = (action.input_schema or {}).get("required") or []
        for k in required:
            if k not in params:
                errors.append(f"missing_param:{name}.{k}")
        # reject unknown params if schema declares properties and additionalProperties false
        schema = action.input_schema or {}
        props = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            for k in params.keys():
                if k not in props:
                    errors.append(f"unknown_param:{name}.{k}")
        return len(errors) == 0, errors


def build_default_registry(runtime: Any = None) -> WorkflowActionRegistry:
    reg = WorkflowActionRegistry()

    async def _noop(params: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "echo": params}

    async def _get_product(params: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        from database import fetch_product
        sku = str(params.get("sku") or "").strip()
        p = await fetch_product(sku)
        return {"status": "ok" if p else "not_found", "product": p}

    async def _list_import_runs(params: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        from database import matterhorn_list_import_runs
        runs = await matterhorn_list_import_runs(limit=int(params.get("limit") or 20))
        return {"status": "ok", "runs": runs}

    async def _run_matterhorn_import(params: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        importer = getattr(runtime, "importer", None)
        if importer is None:
            return {"status": "error", "error": "importer_not_available"}
        res = await importer.import_products(
            mode=str(params.get("mode") or "safe"),
            feed_url=params.get("feed_url"),
            dry_run=bool(params.get("dry_run", False)),
            confirm_full_without_preview=bool(params.get("confirm_full_without_preview", False)),
        )
        return {"status": "ok", "result": res}

    async def _enrich_products(params: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        from services.marketplace_enrichment_pipeline import enrich_products_for_channels
        skus = params.get("skus") or []
        channels = params.get("channels") or []
        res = await enrich_products_for_channels(skus=skus, channels=channels, import_run_id=str(params.get("import_run_id") or ""), runtime=runtime)
        return {"status": "ok", "result": res}

    async def _list_marketplace_datasets(params: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        from database import marketplace_dataset_list
        rows = await marketplace_dataset_list(sku=params.get("sku"), channel=params.get("channel"), limit=int(params.get("limit") or 50))
        return {"status": "ok", "datasets": rows}

    reg.register(WorkflowAction("product.get", "Fetch product by SKU", {"type":"object","properties":{"sku":{"type":"string"}},"required":["sku"],"additionalProperties":False}, read_only=True, handler=_get_product))
    reg.register(WorkflowAction("matterhorn.list_import_runs", "List recent Matterhorn import runs", {"type":"object","properties":{"limit":{"type":"integer"}},"additionalProperties":False}, read_only=True, handler=_list_import_runs))
    reg.register(WorkflowAction("marketplace.list_datasets", "List marketplace/channel datasets", {"type":"object","properties":{"sku":{"type":"string"},"channel":{"type":"string"},"limit":{"type":"integer"}},"additionalProperties":False}, read_only=True, handler=_list_marketplace_datasets))
    reg.register(WorkflowAction("matterhorn.import", "Run Matterhorn import", {"type":"object","properties":{"mode":{"type":"string"},"feed_url":{"type":"string"},"dry_run":{"type":"boolean"},"confirm_full_without_preview":{"type":"boolean"}},"additionalProperties":False}, read_only=False, requires_confirmation=True, handler=_run_matterhorn_import))
    reg.register(WorkflowAction("marketplace.enrich_products", "Generate AI marketplace datasets for products", {"type":"object","properties":{"skus":{"type":"array","items":{"type":"string"}},"channels":{"type":"array","items":{"type":"string"}},"import_run_id":{"type":"string"}},"additionalProperties":False}, read_only=False, requires_confirmation=True, handler=_enrich_products))
    reg.register(WorkflowAction("workflow.noop", "No-op for testing", {"type":"object","properties":{},"additionalProperties":True}, read_only=True, handler=_noop))
    return reg
