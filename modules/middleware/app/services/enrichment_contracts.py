from dataclasses import dataclass
from typing import List, Optional

@dataclass
class ProductSource:
    sku: str
    name: str
    description: str
    price: float
    tax: float
    manufacturer: str
    category: str
    gtin: str
    images: List[str]
    variants: List['ProductSource']
    
@dataclass
class CatalogValidationResult:
    is_valid: bool
    errors: List[str]
    warnings: List[str]
    
@dataclass
class DescriptionSeoResult:
    title: str
    meta_description: str
    slug: str
    alt_texts: List[str]
    
@dataclass
class MediaIngestionResult:
    cover_url: Optional[str]
    image_urls: List[str]
    errors: List[str]
    
@dataclass
class VariantResult:
    parent_sku: str
    child_skus: List[str]
    configurator_options: List[str]
    
@dataclass
class ShopwareImportDecision:
    active: bool
    price_fallback_used: bool
    blocking_errors: List[str]
    export_gate: str
    hallucination_blocked: bool
    cover_url: Optional[str]
