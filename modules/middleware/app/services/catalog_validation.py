from typing import List

from .assortment_profile import classify_product, get_profile_name
from .enrichment_contracts import ProductSource, CatalogValidationResult
from .error_codes import ErrorCode
from .gtin_intelligence import is_valid_gtin


class CatalogValidationService:
    def __init__(
        self,
        required_fields: List[str],
        whitelisted_categories: List[str],
        whitelisted_manufacturers: List[str],
        min_margin_pct: float = 0.0,
        assortment_profile: str | None = None,
    ):
        self.required_fields = required_fields
        self.whitelisted_categories = whitelisted_categories
        self.whitelisted_manufacturers = whitelisted_manufacturers
        self.min_margin_pct = float(min_margin_pct or 0.0)
        self.assortment_profile = get_profile_name(assortment_profile)

    def validate(self, product: ProductSource) -> CatalogValidationResult:
        errors = []
        warnings = []

        for field in self.required_fields:
            value = getattr(product, field, None)
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(ErrorCode.ERR_MISSING_REQUIRED_FIELD.value)

        if product.gtin:
            valid, _ = is_valid_gtin(product.gtin)
            if not valid:
                errors.append(ErrorCode.ERR_INVALID_EAN.value)
        else:
            warnings.append(ErrorCode.ERR_INVALID_EAN.value)

        if product.tax is None or product.tax <= 0:
            warnings.append(ErrorCode.WARN_TAX_FALLBACK_USED.value)
            product.tax = 19.0

        if self.whitelisted_categories and product.category not in self.whitelisted_categories:
            warnings.append(ErrorCode.WARN_CATEGORY_NOT_WHITELISTED.value)

        if self.whitelisted_manufacturers and product.manufacturer not in self.whitelisted_manufacturers:
            warnings.append(ErrorCode.WARN_MANUFACTURER_NOT_WHITELISTED.value)

        # Pricing sanity: EK != VK and margin checks (deterministic, configurable)
        ek = float(getattr(product, "purchase_price", 0.0) or 0.0)
        vk = float(getattr(product, "price", 0.0) or 0.0)
        min_margin = float(getattr(self, "min_margin_pct", 0.0) or 0.0)
        if ek > 0 and vk > 0:
            if abs(vk - ek) < 1e-9:
                errors.append("ERR_EK_EQUALS_VK")
            margin_pct = ((vk - ek) / vk) * 100 if vk else 0.0
            if min_margin > 0 and margin_pct < min_margin:
                warnings.append("WARN_MARGIN_BELOW_MIN")

        # Assortment style filter: strict hard exclusion of classic evening wear.
        assortment = classify_product(
            {
                "title": getattr(product, "name", ""),
                "description": getattr(product, "description", ""),
                "category": getattr(product, "category", ""),
                "brand": getattr(product, "manufacturer", ""),
            },
            self.assortment_profile,
        )
        if assortment.get("excluded_eveningwear"):
            errors.append("ERR_EXCLUDED_EVENINGWEAR")

        return CatalogValidationResult(
            is_valid=not errors,
            errors=errors,
            warnings=warnings,
            assortment_profile=assortment.get("assortment_profile", self.assortment_profile),
            style_score=float(assortment.get("style_score") or 0.0),
            style_tags=list(assortment.get("style_tags") or []),
            is_formal_eveningwear=bool(assortment.get("is_formal_eveningwear")),
            excluded_eveningwear=bool(assortment.get("excluded_eveningwear")),
            exclusion_reason=str(assortment.get("exclusion_reason") or ""),
        )
