import pytest

from services.google_export_engine import (
    EXPORT_ERROR,
    AutonomousExportEngine,
    ExportQueueItem,
)


@pytest.mark.asyncio
async def test_retry_failed_can_target_one_export_item():
    engine = AutonomousExportEngine(None, None, None)
    selected = ExportQueueItem(
        export_id="export-1",
        account_id="account-1",
        product_sku="SKU-1",
        offer_id="offer-1",
        payload={},
        status=EXPORT_ERROR,
    )
    other = ExportQueueItem(
        export_id="export-2",
        account_id="account-1",
        product_sku="SKU-2",
        offer_id="offer-2",
        payload={},
        status=EXPORT_ERROR,
    )
    engine._errors.extend([selected, other])

    retried = await engine.retry_failed(account_id="account-1", export_id="export-1")

    assert retried == 1
    assert engine._queue == [selected]
    assert engine._errors == [other]
    assert selected.status == "pending"
