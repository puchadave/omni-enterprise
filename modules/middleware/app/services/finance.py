"""Canonical finance calculations that do not depend on an accounting provider."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a valid decimal amount") from exc
    if not amount.is_finite():
        raise ValueError(f"{field} must be finite")
    return amount


def reconcile_payment_to_documents(
    transaction: Mapping[str, Any],
    documents: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Match a payment conservatively; amount-only matches always require review."""
    currency = str(transaction.get("currency", "")).upper()
    if len(currency) != 3:
        raise ValueError("Transaction currency must be a three-letter ISO code")
    amount = _decimal(transaction.get("amount"), field="transaction amount")
    if amount < 0:
        raise ValueError("Transaction amount cannot be negative")

    order_reference = str(transaction.get("order_reference") or "").strip()
    same_currency = [
        document
        for document in documents
        if str(document.get("currency", "")).upper() == currency
        and str(document.get("direction", "receivable")) == "receivable"
    ]
    invoice_documents = [
        document
        for document in same_currency
        if str(document.get("document_type", "invoice")) == "invoice"
    ]
    reference_matches = [
        document
        for document in invoice_documents
        if order_reference
        and str(document.get("order_reference") or "").strip() == order_reference
    ]

    if reference_matches:
        exact_matches = [
            document
            for document in reference_matches
            if _decimal(document.get("gross_amount"), field="document gross amount") == amount
        ]
        if len(exact_matches) == 1:
            document = exact_matches[0]
            return {
                "status": "matched",
                "target_type": "document",
                "target_id": str(document["id"]),
                "currency": currency,
                "matched_amount": str(amount),
                "candidate_ids": [str(document["id"])],
            }
        if len(exact_matches) > 1:
            candidates = exact_matches
            status = "ambiguous"
        elif len(reference_matches) > 1:
            candidates = reference_matches
            status = "ambiguous"
        else:
            document = reference_matches[0]
            gross_amount = _decimal(document.get("gross_amount"), field="document gross amount")
            candidates = [document]
            status = "partial_match" if Decimal("0") < amount < gross_amount else "mismatch"
        target_id = str(candidates[0]["id"]) if len(candidates) == 1 else ""
        return {
            "status": status,
            "target_type": "document" if target_id else "",
            "target_id": target_id,
            "currency": currency,
            "matched_amount": str(min(amount, _decimal(candidates[0].get("gross_amount"), field="document gross amount"))) if status == "partial_match" else str(amount if status == "matched" else Decimal("0")),
            "candidate_ids": [str(document["id"]) for document in candidates],
        }

    amount_matches = [
        document
        for document in same_currency
        if _decimal(document.get("gross_amount"), field="document gross amount") == amount
    ]
    if amount_matches:
        return {
            "status": "review_required",
            "target_type": "",
            "target_id": "",
            "currency": currency,
            "matched_amount": "0",
            "candidate_ids": [str(document["id"]) for document in amount_matches],
        }

    return {
        "status": "missing_document",
        "target_type": "",
        "target_id": "",
        "currency": currency,
        "matched_amount": "0",
        "candidate_ids": [],
    }


def calculate_contribution_margin(
    *,
    revenue: Any,
    currency: str,
    cost_of_goods: Any | None,
    marketplace_fee: Any | None,
    payment_fee: Any | None,
    shipping: Any | None,
    packaging: Any | None,
    returns: Any | None,
) -> dict[str, Any]:
    """Calculate contribution only when every required cost input is supplied."""
    normalized_currency = currency.upper()
    if len(normalized_currency) != 3:
        raise ValueError("Currency must be a three-letter ISO code")

    inputs = {
        "cost_of_goods": cost_of_goods,
        "marketplace_fee": marketplace_fee,
        "payment_fee": payment_fee,
        "shipping": shipping,
        "packaging": packaging,
        "returns": returns,
    }
    missing = [name for name, value in inputs.items() if value is None]
    revenue_amount = _decimal(revenue, field="revenue")
    if revenue_amount < 0:
        raise ValueError("Revenue cannot be negative")
    if missing:
        return {
            "status": "incomplete",
            "currency": normalized_currency,
            "revenue": str(revenue_amount),
            "costs": None,
            "contribution": None,
            "margin_percent": None,
            "missing_inputs": missing,
        }

    parsed_costs = {name: _decimal(value, field=name) for name, value in inputs.items()}
    if any(value < 0 for value in parsed_costs.values()):
        raise ValueError("Cost inputs cannot be negative")
    total_costs = sum(parsed_costs.values(), Decimal("0"))
    contribution = revenue_amount - total_costs
    margin_percent = (contribution / revenue_amount * Decimal("100")) if revenue_amount else None
    return {
        "status": "complete",
        "currency": normalized_currency,
        "revenue": str(revenue_amount),
        "costs": str(total_costs),
        "contribution": str(contribution),
        "margin_percent": str(margin_percent) if margin_percent is not None else None,
        "missing_inputs": [],
    }
