from dataclasses import dataclass, field
from typing import Any, List, Optional


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
    assortment_profile: str = ""
    style_score: float = 0.0
    style_tags: List[str] = field(default_factory=list)
    is_formal_eveningwear: bool = False
    excluded_eveningwear: bool = False
    exclusion_reason: str = ""


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


@dataclass
class ProductFacts:
    sku: str
    title: str
    brand: str = ""
    category: str = ""
    description: str = ""
    price: Optional[float] = None
    currency: str = "EUR"
    ean: str = ""
    stock: int = 0
    images: List[dict[str, Any]] = field(default_factory=list)
    attributes: List[dict[str, Any]] = field(default_factory=list)
    variants: List[dict[str, Any]] = field(default_factory=list)
    logistics: dict[str, Any] = field(default_factory=dict)
    source_system: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "title": self.title,
            "brand": self.brand,
            "category": self.category,
            "description": self.description,
            "price": self.price,
            "currency": self.currency,
            "ean": self.ean,
            "stock": self.stock,
            "images": self.images,
            "attributes": self.attributes,
            "variants": self.variants,
            "logistics": self.logistics,
            "source_system": self.source_system,
        }


@dataclass
class AITaskResult:
    task: str
    status: str
    output: dict[str, Any]
    model: str = ""
    prompt_version: str = ""
    facts_hash: str = ""
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class MarketplaceDataset:
    product_sku: str
    channel: str
    status: str
    facts_hash: str
    content: dict[str, Any] = field(default_factory=dict)
    validation_errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    ai_model: str = ""
    prompt_version: str = ""

