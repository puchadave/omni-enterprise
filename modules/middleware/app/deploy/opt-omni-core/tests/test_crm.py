from services.crm_models import normalize_message, payment_verified, fulfillment_release_allowed


def test_normalize_shopware_message_maps_core_fields():
    msg = normalize_message("shopware", {"id": "m1", "threadId": "t1", "customerId": "c1", "email": "a@example.com", "comment": "Hallo", "orderNumber": "1001"})
    assert msg.channel == "shopware"
    assert msg.external_message_id == "m1"
    assert msg.external_thread_id == "t1"
    assert msg.customer_external_id == "c1"
    assert msg.order_external_id == "1001"
    assert msg.body == "Hallo"
    assert msg.idempotency_key


def test_normalize_message_rejects_unknown_channel():
    try:
        normalize_message("fax", {"id": "1"})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_payment_verified_and_fulfillment_guard_logic():
    paid = {"payment_status": "paid", "fulfillment_locked": False}
    unpaid = {"payment_status": "pending", "fulfillment_locked": False}
    locked = {"payment_status": "verified", "fulfillment_locked": True}
    assert payment_verified(paid) is True
    assert fulfillment_release_allowed(paid) is True
    assert payment_verified(unpaid) is False
    assert fulfillment_release_allowed(unpaid) is False
    assert payment_verified(locked) is True
    assert fulfillment_release_allowed(locked) is False
