from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import database
from database import _finance_amount
from main import _has_permission, _required_permission, app
from services.finance import calculate_contribution_margin, reconcile_payment_to_documents


def _document(document_id: str, amount: str, order_reference: str = "ORDER-1") -> dict:
    return {
        "id": document_id,
        "direction": "receivable",
        "order_reference": order_reference,
        "currency": "EUR",
        "gross_amount": Decimal(amount),
    }


def test_reconciliation_requires_unambiguous_reference():
    payment = {"currency": "EUR", "order_reference": "ORDER-1", "amount": Decimal("119.00")}

    matched = reconcile_payment_to_documents(payment, [_document("invoice-1", "119.00")])
    assert matched["status"] == "matched"
    assert matched["target_id"] == "invoice-1"

    partial = reconcile_payment_to_documents(
        {**payment, "amount": Decimal("50.00")},
        [_document("invoice-1", "119.00")],
    )
    assert partial["status"] == "partial_match"
    assert partial["matched_amount"] == "50.00"

    ambiguous = reconcile_payment_to_documents(
        payment,
        [_document("invoice-1", "119.00"), _document("invoice-2", "119.00")],
    )
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["target_id"] == ""


def test_amount_only_reconciliation_is_review_required():
    result = reconcile_payment_to_documents(
        {"currency": "EUR", "order_reference": "", "amount": Decimal("119.00")},
        [_document("invoice-1", "119.00")],
    )

    assert result["status"] == "review_required"
    assert result["target_id"] == ""


def test_reconciliation_does_not_auto_match_credit_notes_or_overpayments():
    payment = {"currency": "EUR", "order_reference": "ORDER-1", "amount": Decimal("119.00")}
    credit_note = {
        **_document("credit-1", "119.00"),
        "document_type": "credit_note",
    }
    result = reconcile_payment_to_documents(payment, [credit_note])
    assert result["status"] == "review_required"

    overpayment = reconcile_payment_to_documents(
        {**payment, "amount": Decimal("120.00")},
        [_document("invoice-1", "119.00")],
    )
    assert overpayment["status"] == "mismatch"

    missing = reconcile_payment_to_documents(payment, [])
    assert missing["status"] == "missing_document"


def test_contribution_margin_does_not_infer_missing_costs():
    incomplete = calculate_contribution_margin(
        revenue="100.00",
        currency="EUR",
        cost_of_goods="40.00",
        marketplace_fee=None,
        payment_fee="2.00",
        shipping="5.00",
        packaging="1.00",
        returns="0.00",
    )
    assert incomplete["status"] == "incomplete"
    assert incomplete["contribution"] is None
    assert incomplete["missing_inputs"] == ["marketplace_fee"]

    complete = calculate_contribution_margin(
        revenue="100.00",
        currency="EUR",
        cost_of_goods="40.00",
        marketplace_fee="10.00",
        payment_fee="2.00",
        shipping="5.00",
        packaging="1.00",
        returns="0.00",
    )
    assert complete["status"] == "complete"
    assert Decimal(complete["contribution"]) == Decimal("42.00")

    zero_revenue = calculate_contribution_margin(
        revenue="0",
        currency="EUR",
        cost_of_goods="0",
        marketplace_fee="0",
        payment_fee="0",
        shipping="0",
        packaging="0",
        returns="0",
    )
    assert zero_revenue["margin_percent"] is None


def test_finance_amount_precision_matches_database_contract():
    assert _finance_amount("99999999999999.999999", field="amount") == Decimal("99999999999999.999999")
    with pytest.raises(ValueError):
        _finance_amount("100000000000000", field="amount")
    with pytest.raises(ValueError):
        _finance_amount("0.0000001", field="amount")


@pytest.mark.asyncio
async def test_finance_persistence_is_idempotent_and_reconciliation_is_durable(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "finance.sqlite")
    await database.init_db()

    first = await database.finance_document_upsert(
        {
            "provider": "shopware",
            "external_id": "invoice-1",
            "document_type": "invoice",
            "direction": "receivable",
            "order_reference": "ORDER-1",
            "currency": "EUR",
            "net_amount": Decimal("100.00"),
            "tax_amount": Decimal("19.00"),
            "gross_amount": Decimal("119.00"),
            "payload_json": {"source": "test"},
        }
    )
    second = await database.finance_document_upsert(
        {
            "provider": "shopware",
            "external_id": "invoice-1",
            "document_type": "invoice",
            "direction": "receivable",
            "order_reference": "ORDER-1",
            "currency": "EUR",
            "net_amount": Decimal("100.00"),
            "tax_amount": Decimal("19.00"),
            "gross_amount": Decimal("119.00"),
            "payload_json": {"source": "updated"},
        }
    )
    assert first["id"] == second["id"]
    assert second["payload_json"] == {"source": "updated"}
    assert len(await database.finance_document_list()) == 1

    transaction = await database.finance_transaction_upsert(
        {
            "provider": "paypal",
            "external_id": "payment-1",
            "transaction_type": "payment",
            "order_reference": "ORDER-1",
            "currency": "EUR",
            "amount": Decimal("119.00"),
            "payload_json": {},
        }
    )
    outcome = reconcile_payment_to_documents(
        transaction,
        await database.finance_document_candidates(
            currency="EUR",
            order_reference="ORDER-1",
            gross_amount=transaction["amount"],
        ),
    )
    persisted = await database.finance_reconciliation_create(
        {
            "source_type": "transaction",
            "source_id": transaction["id"],
            "target_type": outcome["target_type"],
            "target_id": outcome["target_id"],
            "status": outcome["status"],
            "currency": outcome["currency"],
            "matched_amount": outcome["matched_amount"],
            "details_json": {"candidate_ids": outcome["candidate_ids"]},
        }
    )
    assert persisted["status"] == "matched"
    assert len(await database.finance_reconciliation_list()) == 1

    await database.finance_settlement_upsert(
        {
            "provider": "marketplace",
            "external_id": "settlement-1",
            "currency": "EUR",
            "gross_amount": Decimal("0.10"),
            "fees_amount": Decimal("0.20"),
            "net_amount": Decimal("-0.10"),
            "payload_json": {},
        }
    )
    await database.finance_settlement_upsert(
        {
            "provider": "marketplace",
            "external_id": "settlement-2",
            "currency": "EUR",
            "gross_amount": Decimal("0.20"),
            "fees_amount": Decimal("0.10"),
            "net_amount": Decimal("0.10"),
            "payload_json": {},
        }
    )
    overview = await database.finance_overview()
    assert overview["documents"][0]["currency"] == "EUR"
    assert Decimal(overview["settlements"][0]["gross_amount"]) == Decimal("0.30")
    assert Decimal(overview["settlements"][0]["fees_amount"]) == Decimal("0.30")
    assert Decimal(overview["settlements"][0]["net_amount"]) == Decimal("0")


def test_finance_api_import_and_reconcile(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "finance-api.sqlite")
    monkeypatch.setenv("INTERNAL_API_KEY", "finance-test-key")
    headers = {"X-Internal-API-Key": "finance-test-key"}

    with TestClient(app) as client:
        document = client.post(
            "/api/finance/documents",
            headers=headers,
            json={
                "provider": "shopware",
                "external_id": "invoice-api-1",
                "document_type": "invoice",
                "direction": "receivable",
                "order_reference": "ORDER-API-1",
                "currency": "eur",
                "net_amount": "100.00",
                "tax_amount": "19.00",
                "gross_amount": "119.00",
            },
        )
        assert document.status_code == 200
        assert document.json()["currency"] == "EUR"

        payment = client.post(
            "/api/finance/transactions",
            headers=headers,
            json={
                "provider": "paypal",
                "external_id": "payment-api-1",
                "transaction_type": "payment",
                "order_reference": "ORDER-API-1",
                "currency": "EUR",
                "amount": "119.00",
            },
        )
        assert payment.status_code == 200

        result = client.post(
            "/api/finance/reconcile",
            headers=headers,
            json={"transaction_id": payment.json()["id"]},
        )
        assert result.status_code == 200
        assert result.json()["status"] == "matched"
        assert client.get("/api/finance/overview", headers=headers).status_code == 200

        margin = client.post(
            "/api/finance/contribution-margin",
            headers=headers,
            json={
                "revenue": "100.00",
                "currency": "eur",
                "cost_of_goods": "40.00",
                "marketplace_fee": "8.00",
                "payment_fee": "3.00",
                "shipping": "5.00",
                "packaging": "2.00",
                "returns": "0.00",
            },
        )
        assert margin.status_code == 200
        assert margin.json()["status"] == "complete"
        assert margin.json()["contribution"] == "42.00"

        incomplete_margin = client.post(
            "/api/finance/contribution-margin",
            headers=headers,
            json={"revenue": "100.00", "currency": "EUR"},
        )
        assert incomplete_margin.status_code == 200
        assert incomplete_margin.json()["status"] == "incomplete"
        assert incomplete_margin.json()["contribution"] is None


def test_finance_api_requires_authentication(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "finance-auth.sqlite")
    monkeypatch.setenv("INTERNAL_API_KEY", "finance-test-key")
    with TestClient(app) as client:
        assert client.get("/api/finance/overview").status_code == 401


def test_finance_api_permissions_are_separate_from_general_read_access():
    permission = _required_permission("GET", "/api/finance/overview")
    assert permission == "finance.read"
    assert not _has_permission(
        {"authenticated": True, "permissions": ["products.read"]},
        permission,
    )
    assert _required_permission("POST", "/api/finance/contribution-margin") == "finance.read"
