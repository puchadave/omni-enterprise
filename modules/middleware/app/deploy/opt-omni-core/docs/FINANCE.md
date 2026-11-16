# Finance Integration Foundation

Omni-Core now provides a provider-neutral persistence and integration layer for finance records. It is intended to connect commerce and accounting systems, not to replace a tax adviser or certified bookkeeping product.

## Available records and routes

Every route requires the existing API authentication. Finance reads and the non-mutating margin calculator require `finance.read`; ingestion and reconciliation require `finance.write`. The `ACCOUNTANT` role has both permissions; administrators retain them as well.

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/api/finance/documents` | Idempotently import an invoice, credit note, supplier invoice, or expense |
| `GET` | `/api/finance/documents` | List stored documents |
| `POST` | `/api/finance/transactions` | Idempotently import a payment, refund, fee, payout, or adjustment |
| `GET` | `/api/finance/transactions` | List stored transactions |
| `POST` | `/api/finance/settlements` | Idempotently import a settlement |
| `GET` | `/api/finance/settlements` | List stored settlements |
| `POST` | `/api/finance/reconcile` | Reconcile one payment transaction against receivable invoices |
| `GET` | `/api/finance/reconciliations` | List persisted reconciliation outcomes |
| `GET` | `/api/finance/overview` | Source totals grouped by currency and transaction type |
| `POST` | `/api/finance/contribution-margin` | Calculate contribution from caller-supplied cost inputs |

List endpoints accept bounded `limit` (1–500) and non-negative `offset` query parameters.

The margin endpoint is a scenario calculation, not actualized product/channel profitability. It returns `incomplete` when any required cost input is absent and does not infer a value.

## Example import flow

Import a sales invoice:

```json
{
  "provider": "shopware",
  "external_id": "invoice-123",
  "document_type": "invoice",
  "direction": "receivable",
  "order_reference": "ORDER-123",
  "currency": "EUR",
  "net_amount": "100.00",
  "tax_amount": "19.00",
  "gross_amount": "119.00",
  "status": "issued"
}
```

Import its payment and then reconcile it:

```json
{
  "provider": "paypal",
  "external_id": "transaction-456",
  "transaction_type": "payment",
  "order_reference": "ORDER-123",
  "currency": "EUR",
  "amount": "119.00"
}
```

Pass the returned transaction `id` to `POST /api/finance/reconcile` as `{"transaction_id":"..."}`. Provider plus external ID is the idempotency key for each imported record. A repeated import updates that record rather than creating a duplicate.

## Reconciliation safety

Reconciliation outcomes are `matched`, `partial_match`, `mismatch`, `missing_document`, `ambiguous`, or `review_required`. Only a single exact amount match with a matching order reference is auto-marked `matched`. Amount-only matches, credit notes, and ambiguous candidates require review; the service does not post accounting entries or issue refunds. Reconciliation details retain candidate IDs for review.

## Amount and BI semantics

Amounts are stored to six decimal places, with an explicit ISO-style three-letter currency on every record. PostgreSQL uses `NUMERIC`; SQLite stores canonical decimal text to avoid binary floating-point drift. Totals are never combined across currencies. Invoice totals and settlements are reported separately because they are different accounting facts. The overview is not recognized revenue, cashflow, or realized profit.

Contribution margin is available as a deterministic calculation helper. It returns `incomplete` when any required input (cost of goods, marketplace/payment fee, shipping, packaging, or returns) is absent; it does not fill gaps with pricing rules or assumed markup.

## Data handling and limitations

- Keep `payload_json` to bounded provider metadata; do not send credentials or full customer/document content in it.
- Original invoice files, XRechnung/ZUGFeRD validation, DATEV posting/export, bank feeds, tax-rule versioning, and provider credentials are not implemented by this foundation.
- Before enabling an accounting adapter, verify the provider's current API contract and required audit/document semantics.
- Database initialization creates the new tables for both PostgreSQL and SQLite. Imported finance records remain separate from the existing order `payment_status` until an explicit, audited linking workflow is implemented.
