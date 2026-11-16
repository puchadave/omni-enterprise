# Matterhorn Integration - Performance Guide

## Performance Profiles

Three predefined profiles are available, switchable at runtime:

```python
from services.performance import set_profile, get_profile

# Set profile
set_profile("ECONOMY")   # Low resource usage
set_profile("BALANCED")  # Default, balanced performance
set_profile("PERFORMANCE")  # High throughput
```

### ECONOMY Profile

```python
{
    "max_workers": 1,
    "batch_size": 10,
    "max_connections": 5,
    "streaming": True,
    "buffer_size": 4 * 1024,     # 4KB
    "queue_priority": "low",
    "max_retries": 2,
    "connection_timeout": 10,    # seconds
}
```

**Use case**: Low-resource deployments, development, occasional imports

### BALANCED Profile (Default)

```python
{
    "max_workers": 4,
    "batch_size": 50,
    "max_connections": 15,
    "streaming": True,
    "buffer_size": 8 * 1024,     # 8KB
    "queue_priority": "normal",
    "max_retries": 3,
    "connection_timeout": 30,    # seconds
}
```

**Use case**: Standard production deployments, typical workload

### PERFORMANCE Profile

```python
{
    "max_workers": 8,
    "batch_size": 200,
    "max_connections": 50,
    "streaming": True,
    "buffer_size": 16 * 1024,    # 16KB
    "queue_priority": "high",
    "max_retries": 5,
    "connection_timeout": 60,    # seconds
}
```

**Use case**: High-volume operations, many concurrent users, large product catalogs

## Performance Best Practices

### 1. Streaming XML Parsing

The system uses streaming XML parsing instead of loading the entire document into memory:

```python
from services.performance import stream_xml_products

xml_text = """<?xml..."""  # Large XML document

# Yield products in batches of 50
for batch in stream_xml_products(xml_text, batch_size=50):
    # Process batch
    process_products(batch)
```

### 2. Bulk Inserts

Use bulk inserts for database operations instead of individual inserts:

```python
from services.performance import bulk_insert_products

products = [product1, product2, ...]  # List of product dicts

# Insert in batches of 100
total = bulk_insert_products(products, batch_size=100)
print(f"Inserted {total} products")
```

### 3. Connection Pooling

HTTP connections are pooled and reused:

```python
from services.performance import ConnectionPool

pool = ConnectionPool(max_connections=15, timeout=30)

# Get a connection
client = await pool.get_connection()

# Use the client
response = await client.get("https://api.matterhorn.de/ITEMS/")

# Release when done
await pool.release_connection(client)
```

### 4. Queue Prioritization

Different item types get different priority levels based on the profile:

```python
from services.performance import get_priority, QueuePriority

priority = get_priority("BALANCED", "order")  # Returns QueuePriority.HIGH
priority = get_priority("ECONOMY", "product")  # Returns QueuePriority.LOW
```

### 5. Performance Monitoring

Track operation durations and identify bottlenecks:

```python
from services.performance import PerformanceMonitor

monitor = PerformanceMonitor()
monitor.start_timer()

# ... perform operation ...

duration = monitor.stop_timer("product_sync")
avg = monitor.get_average("product_sync")

report = monitor.get_report()
print(f"Report: {report}")
```

### 6. Incremental Synchronization

Avoid full re-imports by using incremental sync:

```python
from services.inventory_sync import InventorySyncService

# Only sync products changed since last update
result = await service.sync_all(force_full=False)
# result.products_unchanged will reflect products not needing update
```

### 7. Image Deduplication

Prevent duplicate image downloads/storage:

```python
from services.image_manager import ImageManager
from pathlib import Path

manager = ImageManager()

# Check if image already processed
result = manager.download_image(Path("/path/to/image.jpg"))
if result.is_duplicate:
    print("Image already in cache, skipping download")
```

## Monitoring and Profiling

### Performance Monitor

The `PerformanceMonitor` class tracks operation durations:

```python
monitor = PerformanceMonitor()

# Start timing
monitor.start_timer()

# ... operation ...

# Stop and record
duration = monitor.stop_timer("my_operation")

# Get statistics
avg_duration = monitor.get_average("my_operation")
report = monitor.get_report()  # {operation: {count, avg_seconds}}
```

### Profile Switching During Runtime

```python
from services.performance import set_profile, get_profile

# Start with balanced
set_profile("BALANCED")

# Switch to economy under load
set_profile("ECONOMY")

# Verify current profile
current = get_profile()
print(f"Current profile: {current}")
```

## Configuration via Environment

Performance settings can also be configured via environment variables
(even though this module uses in-code profiles):

```bash
# These are handled by the broader system, not directly by performance.py
# But the principles apply:
# - OMNI_PERFORMANCE_PROFILE=ECONOMY
# - OMNI_MAX_WORKERS=4
# - OMNI_BATCH_SIZE=50
```

## Testing Performance

### Unit Tests

Performance-related tests verify correct behavior across profiles:

```python
def test_profile_switching():
    """Test that profiles can be switched."""
    from services.performance import set_profile, get_profile
    
    set_profile("ECONOMY")
    assert get_profile() == "ECONOMY"
    
    set_profile("PERFORMANCE")
    assert get_profile() == "PERFORMANCE"
    
    # Reset
    set_profile("BALANCED")
```

### Integration Tests

Test that performance settings affect actual operations:

```python
@pytest.mark.asyncio
async def test_batch_processing():
    """Test that batch sizes are respected."""
    from services.performance import stream_xml_products
    
    # Create XML with 150 products
    xml_text = """<?xml...150 products..."""
    
    batches = list(stream_xml_products(xml_text, batch_size=50))
    assert len(batches) == 3  # 150 / 50 = 3 batches
```

## Troubleshooting Performance Issues

### High Memory Usage

- Switch to ECONOMY profile
- Reduce batch sizes
- Enable streaming mode (default: True)

### Slow API Responses

- Increase connection timeout
- Switch to PERFORMANCE profile
- Check connection pool size

### Rate Limiting

- Reduce max_retries
- Increase connection_timeout
- Use ECONOMY profile with lower parallelism

### Memory Pressure

- Enable streaming (default)
- Reduce buffer sizes per profile
- Use smaller batch sizes