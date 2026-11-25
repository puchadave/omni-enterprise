from typing import List

from .enrichment_contracts import ProductSource, VariantResult


class VariantGenerationService:
    def generate(self, product: ProductSource) -> VariantResult:
        child_skus = [variant.sku for variant in (product.variants or []) if getattr(variant, "sku", None)]
        configurator_options: List[str] = []
        if product.variants:
            first = product.variants[0]
            options = getattr(first, "options", None)
            if isinstance(options, dict):
                configurator_options = [str(k) for k in options.keys()]
            elif isinstance(options, (list, tuple)):
                configurator_options = [str(o) for o in options]
        return VariantResult(parent_sku=product.sku, child_skus=child_skus, configurator_options=configurator_options)
