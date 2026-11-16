import pytest

import database
from database import FulfillmentBlockedError


@pytest.mark.asyncio
async def test_stock_reservation_requires_paid_unlocked_order(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "fulfillment.sqlite")
    await database.init_db()

    await database.upsert_product(
        sku="SKU-PAID-GUARD",
        title="Guard test",
        category="",
        supplier_price=1.0,
        stock=5,
        source_hash="guard-product",
    )
    await database.create_order(
        order_id="ORDER-UNPAID",
        sku="SKU-PAID-GUARD",
        quantity=1,
        marketplace="test",
    )

    with pytest.raises(FulfillmentBlockedError, match="paid and unlocked"):
        await database.reserve_order_stock("ORDER-UNPAID", "SKU-PAID-GUARD", 1)

    async with database.get_connection() as db:
        cursor = await db.execute(
            "UPDATE orders SET payment_status='paid' WHERE order_id='ORDER-UNPAID'"
        )
        await cursor.close()
        await db.commit()
    await database.reserve_order_stock("ORDER-UNPAID", "SKU-PAID-GUARD", 1)
    assert (await database.fetch_order("ORDER-UNPAID"))["status"] == "reserved"

    await database.create_order(
        order_id="ORDER-LOCKED",
        sku="SKU-PAID-GUARD",
        quantity=1,
        marketplace="test",
    )
    async with database.get_connection() as db:
        cursor = await db.execute(
            "UPDATE orders SET payment_status='paid', fulfillment_locked=1 WHERE order_id='ORDER-LOCKED'"
        )
        await cursor.close()
        await db.commit()

    with pytest.raises(FulfillmentBlockedError, match="paid and unlocked"):
        await database.reserve_order_stock("ORDER-LOCKED", "SKU-PAID-GUARD", 1)
