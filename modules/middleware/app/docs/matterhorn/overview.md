# Matterhorn Integration Documentation

## Overview

The Matterhorn integration provides a complete bridge between the Omni-CERP middleware system and the Matterhorn (B2B) API. It enables product feed management, inventory synchronization, price calculation, order management, and image handling for Matterhorn-connected suppliers.

## Architecture

The integration follows a modular architecture with the following components:

1. **MatterhornConnectionManager** - Central API client with authentication, retry logic, pagination, and error handling
2. **MatterhornAPIClient** - Context manager for the full API client with all sub-modules
3. **FeedManager** - Unified feed parser supporting XML, CSV, XLS, Shopify, WooCommerce, and PrestaShop formats
4. **FeedDiscovery** - Automatic format detection and metadata extraction
5. **REST Client** - B2BAPI client for Items, Brands, Categories, Delivery, and Orders
6. **CanonicalProductMapper** - Maps Matterhorn data to the canonical product model
7. **InventorySyncService** - Synchronizes stock levels with buffer and deduplication
8. **PriceEngine** - Calculates net/gross prices with margin and tax rules
9. **OrderManagementService** - Handles order creation and status workflow
10. **MatterhornControlCenter** - Dashboard and diagnostic interface
11. **ImportWizard** - Multi-step import process with 10 steps
12. **ImageManager** - Image download, deduplication, and processing

## Performance Profiles

Three performance profiles are available, configurable via `set_profile()`:

- **ECONOMY**: Minimal parallelism, low RAM usage (max_workers: 1, batch_size: 10)
- **BALANCED** (default): Dynamic resource distribution, standard profile (max_workers: 4, batch_size: 50)
- **PERFORMANCE**: Higher parallelism, larger pools (max_workers: 8, batch_size: 200)

Use `set_profile("ECONOMY")`, `set_profile("BALANCED")`, or `set_profile("PERFORMANCE")` to switch.

## Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Set up environment
cp .env.example .env
# Configure Matterhorn API credentials

# Start the system
docker-compose up -d
```

## API Authentication

The MatterhornConnectionManager uses API key authentication:

```python
from services.matterhorn_connection_manager import MatterhornConnectionManager

cm = MatterhornConnectionManager(api_key="YOUR_API_KEY")
```

Authentication is automatically included in all API requests. The system handles:
- 401 Unauthorized errors
- Retry logic with exponential backoff
- Rate limiting detection and respect

## Feed Management

### Supported Formats

- **XML (FORMAT_XML)**: Standard product feed format
- **XML v2 (FORMAT_XML_V2)**: Extended format with EAN/GTIN
- **XML EAN (FORMAT_XML_EAN)**: EAN-coded product feed
- **CSV (FORMAT_CSV)**: Comma-separated values
- **XLS (FORMAT_XLS)**: Excel spreadsheets
- **Shopify (FORMAT_SHOPIFY)**: Shopify product export format
- **WooCommerce (FORMAT_WOOCOMMERCE)**: WooCommerce export format
- **PrestaShop Categories (FORMAT_PRESTASHOP_CATEGORIES)**: Category structure
- **PrestaShop Products (FORMAT_PRESTASHOP_PRODUCTS)**: Product feed
- **PrestaShop Stock (FORMAT_PRESTASHOP_STOCK)**: Stock levels only
- **XLS Dictionary (FORMAT_XLS_DICTIONARY)**: Brand/dictionary files

### Unified Parsing

```python
from services.feed_manager import parse_feed, FORMAT_XML

xml_text = """<?xml version="1.0"?><products><product><sku>TEST</sku><title>Test</title></product></products>"""

result = parse_feed(xml_text, format_hint=FORMAT_XML)
print(f"Parsed {result.product_count} products")
print(f"Format: {result.format}")
```

### Format Detection

```python
from services.feed_manager import detect_feed_format

xml_text = """<?xml version="1.0"?><products>...</products>"""
fmt, meta = detect_feed_format(xml_text, "products.xml")
```

## Inventory Synchronization

### Sync Process

```python
from services.inventory_sync import InventorySyncService
from services.canonical_product_mapping import CanonicalProduct
from services.matterhorn_connection_manager import MatterhornConnectionManager

# Initialize
db_products = {"SKU1": CanonicalProduct.from_matterhorn("SUPPLIER", data)}
cm = MatterhornConnectionManager(api_key="KEY")
service = InventorySyncService(cm, db_products)

# Sync
result = await service.sync_all(force_full=False)
print(f"Synced {result.products_synced} products")
print(f"Errors: {result.errors}")
```

### Stock Buffer

A 10% buffer is automatically applied to stock levels to prevent overselling:

```python
# The service adds a 10% buffer to supplier stock
# If supplier has 100 units, available becomes 90 (after buffer)
```

## Price Calculation

### Price Engine

```python
from services.price_engine import PriceEngine, calculate_sales_price
from decimal import Decimal
from services.canonical_product_mapping import CanonicalProduct

engine = PriceEngine()

# Calculate price with default margin (15%)
product = CanonicalProduct.from_matterhorn("SUPPLIER", data)
result = engine.calculate_price(product)

print(f"Netto price: {result.net_price}")
print(f"Brutto price: {result.brutto_price}")
print(f"Margin: {result.margin_percent}%")

# Convenience function
result2 = calculate_sales_price(
    supplier_price_netto=Decimal("10.00"),
    margin_percent=Decimal("30.00"),
    tax_percent=Decimal("19.00")
)
```

### Rules

- Global rules apply to all products
- Brand-specific rules can be configured
- Category-specific rules can be configured
- Product-specific overrides are supported
- 10% safety margin is included in all calculations (minimum price protection)

## Order Management

### Order Creation

```python
from services.order_management import OrderManagementService
from services.matterhorn_connection_manager import MatterhornConnectionManager

cm = MatterhornConnectionManager(api_key="KEY")
order_svc = OrderManagementService(cm)

# Create order
items = [{"variant_uid": "VAR-001", "quantity": 2}]
delivery = {
    "first_name": "Test",
    "second_name": "User",
    "country": "DE",
    "street": "Teststr.",
    "house_number": "1",
    "zip": "12345",
    "city": "Testort"
}

result = await order_svc.create_order(items, delivery)
print(f"Order ID: {result['order_id']}")
print(f"Status: {result['status']}")
```

### Status Workflow

Orders follow this workflow:
```
entwurf → bereit_zur_bestellung → abgeschlossen
```

- Status transitions are validated
- Idempotent order creation via UUID
- PUT /ACCOUNT/ORDERS/{order_id} for updates

## Import Wizard

### 10-Step Process

1. **Step 1**: File Upload - Select feed file
2. **Step 2**: Format Detection - Auto-detect or manual selection
3. **Step 3**: Mapping Configuration - Map feed columns to product fields
4. **Step 4**: Product Selection - Choose which products to import
5. **Step 5**: Inventory Settings - Configure stock handling
6. **Step 6**: Price Configuration - Set pricing rules
7. **Step 7**: Category Mapping - Map categories
8. **Step 8**: Brand Configuration - Set brand assignments
9. **Step 9**: Import Execution - Run the import process
10. **Step 10**: Result Report - Generate import summary

### Import Modes

- **Vollimport (FULL)**: Complete re-import of all products
- **Deltimport (DELTA)**: Only new/changed products since last import
- **Bestand-Aktualisierung (STOCK_UPDATE)**: Only update stock levels
- **Preis-Aktualisierung (PRICE_UPDATE)**: Only update prices

```python
from services.import_wizard import ImportWizard, ImportMode

wizard = ImportWizard()
wizard.set_mode(ImportMode.FULL)
```

## Image Management

### Image Processing

```python
from services.image_manager import ImageManager, ImageResult
from pathlib import Path
import tempfile

manager = ImageManager()

# Download and process image
with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
    # Write dummy data
    import os
    os.write(tmp.fileno(), b"fake image data")
    tmp.flush()
    
    result = manager.download_image(Path(tmp.name))
    print(f"SHA-256: {result.sha256_hash}")
    print(f"Size: {result.width}x{result.height}")
    print(f"Is duplicate: {result.is_duplicate}")
```

### Deduplication

Images are deduplicated using SHA-256 hashing. Duplicate images are detected and prevented from being re-downloaded or re-stored.

## Control Center

### Dashboard Data

The MatterhornControlCenter provides dashboard information:

```python
from services.matterhorn_control_center import MatterhornControlCenter

cc = MatterhornControlCenter(api_key="KEY")
dashboard = cc.get_dashboard_data()

print(f"Total products: {dashboard['total_products']}")
print(f"Synced products: {dashboard['synced_products']}")
print(f"Import queue: {dashboard['import_queue']}")
```

### API Diagnostics

```python
diagnostics = cc.run_diagnostics()
print(f"API Status: {diagnostics['api_status']}")
print(f"Response time: {diagnostics['response_time_ms']}ms")
```

## Troubleshooting

### Common Issues

1. **Authentication errors**: Verify API key is correct and has proper permissions
2. **Connection timeouts**: Check network connectivity and Matterhorn server status
3. **Feed parsing errors**: Ensure feed format matches expected structure
4. **Price calculation issues**: Check that supplier prices are set correctly
5. **Inventory sync failures**: Verify database product entries exist

### Performance Tuning

- Switch profiles with `set_profile("ECONOMY")` for low-resource deployments
- Use `set_profile("PERFORMANCE")` for high-volume operations
- Monitor with `PerformanceMonitor` class
- Adjust batch sizes per profile

## API Reference

### MatterhornConnectionManager

- `check_health()` - API health check
- `iterate_paged(endpoint)` - Paginated iteration
- `retrieve_item(sku)` - Retrieve single item
- `create_order(items, delivery)` - Create order
- Context manager support: `async with cm as client`

### ProductAPI

- `list_items(limit, page)` - List products
- `get_item(sku)` - Get single product
- `create_item(data)` - Create new product

### OrderAPI

- `create_order(items, delivery)` - Create order
- `get_order(order_id)` - Get order status
- `update_order_status(order_id, status)` - Update status

### DictionaryAPI

- `list_brands()` - List all brands
- `list_categories()` - List all categories
- `get_brand(brand_id)` - Get brand details

## Contributing

See the main project README for contribution guidelines. This module follows the existing code style and conventions of the Omni-CERP system.

## License

See LICENSE file in the project root.