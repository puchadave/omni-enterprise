# Matterhorn Integration - API Reference

## Classes and Functions

### MatterhornConnectionManager

```python
class MatterhornConnectionManager:
    """Central API client for Matterhorn/B2B API interaction.
    
    Features:
    - API key authentication
    - Retry logic with exponential backoff
    - Paginated request handling
    - Error classification (auth, not_found, rate_limit)
    - Connection pooling
    - Health check functionality
    """
    
    def __init__(self, api_key: str, 
                 retry_count: int = 3,
                 retry_delay: float = 1.0,
                 timeout: int = 30):
        """Initialize the connection manager.
        
        Args:
            api_key: Matterhorn API authentication key
            retry_count: Number of retry attempts on failure
            retry_delay: Initial delay between retries (seconds)
            timeout: Request timeout in seconds
        """
    
    async def check_health(self) -> dict[str, Any]:
        """Check API health status.
        
        Returns:
            dict with status, api_version, response_time_ms
        """
    
    async def iterate_paged(self, endpoint: str, **params) -> Iterator[dict[str, Any]]:
        """Iterate over paginated API results.
        
        Args:
            endpoint: API endpoint (e.g., "/ITEMS/")
            **params: Additional parameters (limit, page)
            
        Yields:
            Individual items from each page
        """
    
    async def _request(self, method: str, endpoint: str, **kwargs) -> httpx.Response:
        """Make an authenticated API request.
        
        Handles:
        - Authentication header injection
        - Retry logic
        - Error classification
        - Response validation
        """
    
    async def close(self) -> None:
        """Close the HTTP session and release resources."""
    
    async def __aenter__(self) -> MatterhornConnectionManager:
        """Async context manager entry."""
    
    async def __aexit__(self, *args) -> None:
        """Async context manager exit."""
```

### MatterhornAPIClient

```python
class MatterhornAPIClient:
    """Full-featured API client with all sub-modules.
    
    Provides access to all Matterhorn API domains through
    dedicated sub-objects.
    """
    
    products: ProductAPI
    dictionaries: DictionaryAPI
    orders: OrderAPI
    inventory: InventorySyncService
    pricing: PriceEngine
    
    @classmethod
    async def create(cls, api_key: str, **kwargs) -> MatterhornAPIClient:
        """Factory method to create a new client.
        
        Args:
            api_key: Matterhorn API key
            **kwargs: Additional configuration options
            
        Returns:
            Initialized MatterhornAPIClient instance
        """
    
    async def __aenter__(self) -> MatterhornAPIClient:
        """Async context manager entry."""
    
    async def __aexit__(self, *args) -> None:
        """Async context manager exit."""
```

### ProductAPI

```python
class ProductAPI:
    """Product-related API operations.
    
    Methods:
    - list_items(limit, page) - Paginated product listing
    - get_item(sku) - Retrieve single product
    - create_item(data) - Create new product
    - update_item(sku, data) - Update existing product
    - delete_item(sku) - Delete product
    """
```

### DictionaryAPI

```python
class DictionaryAPI:
    """Dictionary/Reference data API operations.
    
    Methods:
    - list_brands() - List all brands
    - list_categories() - List all categories
    - get_brand(brand_id) - Get brand details
    - get_category(category_id) - Get category details
    """
```

### OrderAPI

```python
class OrderAPI:
    """Order management API operations.
    
    Methods:
    - create_order(items, delivery) - Create new order
    - get_order(order_id) - Retrieve order status
    - update_order_status(order_id, status) - Update order status
    - cancel_order(order_id) - Cancel order
    """
```

### InventorySyncService

```python
class InventorySyncService:
    """Inventory synchronization service.
    
    Features:
    - Full and delta sync modes
    - 10% stock buffer application
    - Last-update filtering to avoid redundant syncs
    - Fallback to XML for error recovery
    - Idempotent operations
    """
    
    def __init__(self, connection_manager, db_products: dict[str, CanonicalProduct]):
        """Initialize the sync service.
        
        Args:
            connection_manager: MatterhornConnectionManager instance
            db_products: Dict of SKU -> CanonicalProduct from database
        """
    
    async def sync_all(self, force_full: bool = False) -> InventorySyncResult:
        """Synchronize all products' inventory.
        
        Args:
            force_full: If True, force a full sync ignoring last-update timestamps
            
        Returns:
            InventorySyncResult with sync statistics
        """
    
    async def sync_product(self, sku: str) -> InventorySyncResult:
        """Synchronize inventory for a single product.
        
        Args:
            sku: Product SKU to sync
            
        Returns:
            InventorySyncResult with sync result for this product
        """
    
    async def _sync_stock(self, product: CanonicalProduct, api_stock: int) -> None:
        """Internal method to sync stock for a single product.
        
        Applies 10% buffer and updates the product's stock fields.
        """
```

### PriceEngine

```python
class PriceEngine:
    """Price calculation engine with rule support.
    
    Features:
    - Netto/Brutto calculation
    - Global, brand, category, and product-specific rules
    - Margin and tax percentage configuration
    - 10% safety margin inclusion
    - Rule inheritance and override
    """
    
    def __init__(self):
        """Initialize the price engine with default rules."""
    
    def set_global_rule(self, key: str, value: Decimal | float | int) -> None:
        """Set a global rule that applies to all products.
        
        Args:
            rule_key: Rule identifier (e.g., "margin_percent")
            rule_value: Rule value
        """
    
    def set_brand_rule(self, brand_id: str, key: str, value: Decimal | float | int) -> None:
        """Set a rule that applies to products from a specific brand.
        
        Args:
            brand_id: Brand identifier
            rule_key: Rule identifier
            rule_value: Rule value
        """
    
    def set_category_rule(self, category_id: str, key: str, value: Decimal | float | int) -> None:
        """Set a rule that applies to products in a specific category.
        
        Args:
            category_id: Category identifier
            rule_key: Rule identifier
            rule_value: Rule value
        """
    
    def calculate_price(self, product: CanonicalProduct) -> PriceCalculationResult:
        """Calculate the sales price for a product.
        
        Applies:
        - Supplier price (netto)
        - Margin percentage
        - Tax percentage
        - 10% safety margin (minimum price protection)
        - Brand/category overrides if configured
        
        Args:
            product: CanonicalProduct instance with supplier pricing
            
        Returns:
            PriceCalculationResult with all calculated values
        """
    
    def get_effective_margin(self, product: CanonicalProduct) -> Decimal:
        """Get the effective margin for a product, considering overrides.
        
        Args:
            product: The product to check
            
        Returns:
            Effective margin percentage as Decimal
        """
```

### PriceCalculationResult

```python
@dataclass
class PriceCalculationResult:
    """Result of a price calculation."""
    purchase_netto: Decimal  # Base purchase price netto
    net_price: Decimal       # Calculated net sales price
    brutto_price: Decimal    # Calculated gross sales price
    margin_percent: Decimal  # Applied margin percentage
    margin_amount: Decimal   # Absolute margin amount
    tax_percent: Decimal     # Applied tax percentage
    errors: list[str]        # Any validation errors
    warnings: list[str]      # Any warnings generated
```

### CanonicalProduct

```python
class CanonicalProduct:
    """Canonical product model used throughout the system.
    
    This is the central product representation that separates
    supplier-specific data (supplier_product_id) from internal
    identifiers (sku, variant_uid).
    
    Key separation rule:
    - supplier_product_id: The supplier's internal product identifier
    - sku: The internal SKU used in the Omni-CERP system
    - variant_uid: Internal variant identifier for options (size, color, etc.)
    """
    
    @classmethod
    def from_matterhorn(cls, supplier_id: str, data: dict[str, Any]) -> CanonicalProduct:
        """Create a CanonicalProduct from Matterhorn API data.
        
        Args:
            supplier_id: The supplier identifier
            data: Raw data from Matterhorn API
            
        Returns:
            Populated CanonicalProduct instance
        """
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
    
    def merge(self, other: CanonicalProduct) -> CanonicalProduct:
        """Merge with another product, keeping supplier IDs from this instance.
        
        Args:
            other: The product to merge from
            
        Returns:
            Merged CanonicalProduct instance
        """
```

### InventorySyncResult

```python
@dataclass
class InventorySyncResult:
    """Result of an inventory synchronization operation."""
    products_synced: int         # Number of products updated
    products_failed: int         # Number of products that failed
    products_unchanged: int      # Number of products not needing update
    total_supplier_stock: int    # Total stock from supplier
    total_available: int         # Total available stock (with buffer applied)
    errors: list[str]           # Error messages for failed products
    warnings: list[str]         # Warning messages
```

### ImageManager

```python
class ImageManager:
    """Image download and processing manager.
    
    Features:
    - SHA-256 based deduplication
    - Original image storage
    - Download queue management
    - Thumbnail generation (async)
    - Size and dimension extraction
    """
    
    def download_image(self, path: Path) -> ImageResult:
        """Download and process a single image.
        
        Args:
            path: Path to the image file (or URL)
            
        Returns:
            ImageResult with processing information
        """
    
    def process_download_queue(self) -> list[ImageResult]:
        """Process the download queue of pending images.
        
        Returns:
            List of ImageResult for processed images
        """
    
    def _calculate_sha256(self, path: Path) -> str:
        """Calculate SHA-256 hash of an image file.
        
        Args:
            path: Path to the image file
            
        Returns:
            Hex digest of the SHA-256 hash (64 characters)
        """
```

### ImportWizard

```python
class ImportWizard:
    """Multi-step import wizard with 10 steps.
    
    Guides the user through the import process with validation
    at each step.
    """
    
    current_step: int
    mode: ImportMode
    state: dict[str, Any]
    errors: list[str]
    
    def set_mode(self, mode: ImportMode) -> None:
        """Set the import mode (FULL/DELTA/STOCK_UPDATE/PRICE_UPDATE).
        
        Args:
            mode: The ImportMode enum value
        """
    
    def advance(self) -> bool:
        """Advance to the next step.
        
        Returns:
            True if advancement successful, False if at final step
        """
    
    def can_advance(self) -> bool:
        """Check if we can advance to the next step.
        
        Returns:
            True if next step is available
        """
    
    def set_state(self, key: str, value: Any) -> None:
        """Set state for the current import process.
        
        Args:
            key: State key
            value: State value
        """
    
    def get_state(self, key: str, default: Any = None) -> Any:
        """Get state value.
        
        Args:
            key: State key
            default: Return value if key not found
            
        Returns:
            State value or default
        """
    
    def set_error(self, error: str) -> None:
        """Set an error message.
        
        Args:
            error: Error description
        """
    
    def clear_errors(self) -> None:
        """Clear all error messages."""
    
    def get_error(self) -> str | None:
        """Get the first error message, or None if no errors.
        
        Returns:
            Error message or None
        """
```

### ImportMode

```python
class ImportMode(Enum):
    """Import mode enumeration."""
    FULL = auto()       # Vollimport - complete re-import
    DELTA = auto()      # Deltimport - only new/changed since last
    STOCK_UPDATE = auto()  # Bestand-Aktualisierung - only stock
    PRICE_UPDATE = auto()  # Preis-Aktualisierung - only prices
```

### get_import_mode_by_name

```python
def get_import_mode_by_name(name: str) -> ImportMode:
    """Convert a string name to ImportMode enum.
    
    Args:
        name: Mode name (e.g., "Vollimport")
        
    Returns:
        ImportMode enum value
"""

def get_import_mode_display(mode: ImportMode) -> str:
    """Get the display name for an ImportMode.
    
    Args:
        mode: The ImportMode enum value
        
    Returns:
        Human-readable mode name
```