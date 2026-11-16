import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from database import init_db, set_setting, get_setting, get_db
from services.omniroute_client import OmniRouteClient
from services.pricing_engine import PricingEngine
from services.inventory_guard import InventoryGuard
from services.fulfillment import FulfillmentService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events."""
    # Startup
    await init_db()
    logger.info("Database initialized and ready.")
    
    # Initialize services
    omni_route_url = await get_setting("omniroute_url")
    omni_route_key = await get_setting("omniroute_api_key")
    omniroute_client = OmniRouteClient(omni_route_url, omni_route_key) if omni_route_url and omni_route_key else None

    app.state.omniroute_client = omniroute_client
    app.state.pricing_engine = PricingEngine()
    app.state.inventory_guard = InventoryGuard()
    app.state.fulfillment_service = FulfillmentService()
    app.state.queue = asyncio.Queue()
    
    # Perform initial ami route health check
    if omniroute_client:
        is_healthy = await omniroute_client.check_health()
        app.state.omniroute_healthy = is_healthy
        logger.info(f"OmniRoute connection: {'OK' if is_healthy else 'FAILED'}")

    yield

    # Shutdown
    logger.info("Shutting down application...")


app = FastAPI(
    title="Omni-Core Middleware",
    description="Enterprise E-Commerce Middleware for Multi-Channel Integration",
    version="1.0.0",
    lifespan=lifespan
)

# Templates
templates = Jinja2Templates(directory="templates")


# --- Core Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def master_dashboard(request):
    """Master Dashboard with system overview."""
    return templates.TemplateResponse("master_dashboard.html", {"request": request})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request):
    """Settings configuration page."""
    return templates.TemplateResponse("settings.html", {"request": request})


@app.post("/api/settings")
async def save_settings(settings: Dict[str, str]):
    """Save configuration settings."""
    for key, value in settings.items():
        await set_setting(key, value)
    return {"status": "success", "message": "Settings saved"}


@app.get("/api/settings")
async def get_all_settings():
    """Retrieve all settings."""
    async with get_db() as db:
        async with db.execute("SELECT key, value FROM settings") as cursor:
            rows = await cursor.fetchall()
            return {row["key"]: row["value"] for row in rows}


@app.get("/api/health")
async def health_check():
    """System health check endpoint."""
    return {
        "status": "healthy",
        "omniroute_healthy": getattr(app.state, "omniroute_healthy", False),
        "queue_size": app.state.queue.qsize()
    }


@app.get("/api/products")
async def get_products():
    """Get all products."""
    async with get_db() as db:
        async with db.execute("SELECT * FROM products ORDER BY sku") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


@app.post("/api/products")
async def create_product(product: Dict[str, Any]):
    """Create a new product."""
    sku = product["sku"]
    async with get_db() as db:
        await db.execute(
            "INSERT INTO products (sku, title, category, supplier_price, stock, is_bundle, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sku, product["title"], product["category"], product["supplier_price"], 
             product["stock"], product["is_bundle"], product["status"])
        )
        await db.commit()
    return {"status": "success", "product_sku": sku}


@app.get("/api/bundles")
async def get_bundles():
    """Get all bundles."""
    async with get_db() as db:
        async with db.execute("SELECT * FROM bundles ORDER BY bundle_sku") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


@app.post("/api/orders")
async def create_order(order: Dict[str, Any]):
    """Create a new order."""
    order_id = order["order_id"]
    async with get_db() as db:
        await db.execute(
            "INSERT INTO orders (order_id, sku, quantity, customer_email, marketplace, status) VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, order["sku"], order["quantity"], order["customer_email"], order["marketplace"], "pending")
        )
        await db.commit()
    
    # Enqueue for fulfillment
    await app.state.queue.put(order)
    return {"status": "success", "order_id": order_id, "message": "Order queued for fulfillment"}


@app.get("/api/stats")
async def get_stats():
    """Get system statistics."""
    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) as total_products FROM products") as cursor:
            product_count_row = await cursor.fetchone()
            product_count = product_count_row["total_products"] if product_count_row else 0

        async with db.execute("SELECT COUNT(*) as total_orders FROM orders") as cursor:
            order_count_row = await cursor.fetchone()
            order_count = order_count_row["total_orders"] if order_count_row else 0

        async with db.execute("SELECT COUNT(*) as pending_orders FROM orders WHERE status = 'pending'") as cursor:
            pending_row = await cursor.fetchone()
            pending_orders = pending_row[0] if pending_row else 0

    return {
        "total_products": product_count,
        "total_orders": order_count,
        "pending_orders": pending_orders,
        "queue_size": app.state.queue.qsize()
    }


# --- Fulfillment Worker ---

async def fulfillment_worker():
    """Background worker that processes orders."""
    while True:
        try:
            order = await app.state.queue.get()
            logger.info(f"Processing order: {order}")
            result = await app.state.fulfillment_service.process_order(order)
            if result["status"] == "dispatched":
                async with get_db() as db:
                    await db.execute(
                        "UPDATE orders SET status = ?, tracking_number = ? WHERE order_id = ?",
                        (result["status"], result["tracking_number"], result["order_id"])
                    )
                    await db.commit()
            app.state.queue.task_done()
        except Exception as e:
            logger.error(f"Fulfillment worker failed: {e}")


# Start the worker
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(fulfillment_worker())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)