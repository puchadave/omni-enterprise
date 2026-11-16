# Global Control Dashboard Documentation

## Scope

This document describes the Global Control Center implemented for the Omni-Core middleware. It consolidates executive, system, business, AI, marketplace, competitor, and security views through a single control-plane API.

## Architecture

- FastAPI exposes control-center routes under `/api/control-center`.
- `GlobalControlCenter` aggregates module state and metrics.
- Event bus supplies communication and queue health.
- Business intelligence supplies product, inventory, and order metrics.
- AI operations supply provider, model, and routing health.
- Marketplace operations supply Shopware and Google Merchant health.
- Security operations supply audit and error summaries.

## Endpoints

- `GET /api/control-center/overview`
- `GET /api/control-center/dashboard`
- `GET /api/control-center/system`

## Dashboard Views

1. Executive Overview: system health, business metrics, recent activity.
2. System Health: event bus, AI providers, Shopware, Google Merchant.
3. Business Intelligence: product status, inventory, orders, errors.
4. AI Operations: providers, models, routing policies.
5. Marketplace Performance: Shopware and Google Merchant health.
6. Competitor Intelligence: competitor engine status.
7. Security and Audit: error logs and error type distribution.

## KPI Coverage

- Product count and active product count.
- Inventory stock, out-of-stock, and low-stock metrics.
- Pending orders and error counts.
- AI provider health, model usage, and routing.
- Marketplace connection health.
- Event bus queue, publish, process, failure, dead-letter metrics.

## Implementation Status

- Control-center API and aggregation layer implemented.
- Dashboard documentation added.
- Full navigation parity with all mission-defined dashboard items is still pending.

## Recommendation

Extend the full dashboard payload with the remaining mission sections and wire the server-rendered dashboard to consume the control-center endpoints.

