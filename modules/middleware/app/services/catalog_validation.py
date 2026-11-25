from typing import List

from .enrichment_contracts import ProductSource, CatalogValidationResult
from .error_codes import ErrorCode
from .gtin_intelligence import is_valid_gtin


class CatalogValidationService:
    def __init__(
        self,
        required_fields: List[str],
        whitelisted_categories: List[str],
        whitelisted_manufacturers: List[str],
    ):
        self.required_fields = required_fields
        self.whitelisted_categories = whitelisted_categories
        self.whitelisted_manufacturers = whitelisted_manufacturers

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

        return CatalogValidationResult(is_valid=not errors, errors=errors, warnings=warnings)
