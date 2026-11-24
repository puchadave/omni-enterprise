# Spec: AI-gestützte Produktimport-Pipeline (Ansatz 2 – Modulare Enrichment-Pipeline)

**Status:** Entwurf – zur Freigabe
**Datum:** 2026-11-24
**Autor:** Senior E-Commerce & Shopware-6-Integrationsarchitekt (HERMES ONE)
**Version:** 1.0

## 1. Ziel

Die AI-gestützte Produktimport-Pipeline der Omni-Core-Middleware, des OmniCoreConnector und der Shopware-6-Integration analysieren, vervollständigen und optimieren.

Ziel ist ein durchgängiger Import von Matterhorn-Supplier-Daten über AI-Selektion, Enrichment, Validierung, Media-Handling und Shopware-Import inklusive:

- Produktbeschreibungen
- SEO-Metadaten
- Produktbilder
- Katalog- und Shopware-Pflichtfelder
- Idempotenz
- Teilerfolge/Fehler pro Item
- Retry-Logik
- Readiness-Gates und Observability

Die vorliegende Design-Phase erfolgt ohne Implementierungsänderungen.

## 2. Architektur-Prinzip (Ansatz 2)

Ansatz 2 wird als **modulare Enrichment-Pipeline** umgesetzt.

Der bestehende `sales_release_orchestrator.py::import_to_shopware` bleibt der Koordinationspunkt, delegiert aber an klar getrennte Services.

Neue Services unter `modules/middleware/app/services/`:

1. `CatalogValidationService`
2. `DescriptionEnrichmentService`
3. `MediaIngestionService`
4. `VariantGenerationService`
5. `IdempotencyManager`
6. `ObservabilityMixin` / `error_codes.py`

Bestehende Services werden ergänzt oder um genutzt:

- `shopware_description_engine.py`
- `shopware_mapping_engine.py`
- `shopware_connection_manager.py`
- `image_manager.py`
- `gtin_intelligence.py`
- `export_gate.py`
- `database.py`

## 3. Daten- und Enrichment-Vertrag

Zentraler Konzeptvertrag vor Ort: `EnrichmentContext` / Ergebnisdataclasses je Service.

### 3.1 ProductSource

```python
@dataclass
class ProductSource:
    sku: str
    canonical: CanonicalProduct
    raw: dict[str, Any]
    locale: str = "de-DE"
```

### 3.2 CatalogValidationResult

```python
@dataclass
class CatalogValidationResult:
    sku: str
    blocking_errors: list[ErrorCode]
    warnings: list[ErrorCode]
    normalized: dict[str, Any]
    price_fallback_used: bool
    tax_fallback_used: bool
```

### 3.3 DescriptionSeoResult

```python
@dataclass
class DescriptionSeoResult:
    sku: str
    description_html: str
    short_description: str
    seo_title: str
    meta_description: str
    url_slug: str
    keywords: list[str]
    alt_texts: dict[int, str]
    facts_hash: str
    hallucination_blocked: bool
```

### 3.4 MediaIngestionResult

```python
@dataclass
class MediaIngestionResult:
    sku: str
    cover_url: str | None
    gallery: list[MediaAsset]
    failed_images: list[MediaFailure]
```

### 3.5 VariantResult

```python
@dataclass
class VariantResult:
    sku: str
    is_variant_parent: bool
    parent_number: str | None
    options: list[dict]
    children: list[dict]
    visibilities: list[dict]
```

### 3.6 ShopwareImportDecision

```python
@dataclass
class ShopwareImportDecision:
    sku: str
    payload: dict[str, Any]
    active: bool
    idempotency_key: str
    lifecycle_status: str
    error_code: ErrorCode | None
```

## 4. Standardisierte Error-Codes

| Code | Bedeutung | Blockierend |
|---|---|---|
| `ERR_MISSING_SKU` | SKU fehlt | Ja |
| `ERR_MISSING_TITLE` | Titel fehlt | Ja |
| `ERR_INVALID_EAN` | EAN/GTIN ungültig | Ja |
| `ERR_MISSING_MANUFACTURER` | Hersteller fehlt | Ja |
| `ERR_MISSING_TAX` | Steuer fehlt | Ja |
| `ERR_MISSING_PRICE` | Preis fehlt | Ja |
| `ERR_MISSING_CURRENCY` | Währung fehlt | Ja |
| `ERR_MISSING_UNIT` | Einheit fehlt | Ja |
| `ERR_MISSING_CATEGORY` | Kategorie fehlt | Ja |
| `ERR_SHOPWARE_NOT_CONFIGURED` | Shopware nicht konfiguriert | Ja |
| `ERR_SHOPWARE_READBACK_FAILED` | Readback fehlgeschlagen | Ja |
| `ERR_IDEMPOTENCY_CONFLICT` | Idempotenzkonflikt | Ja |

| Code | Bedeutung | Blockierend |
|---|---|---|
| `WARN_PRICE_FALLBACK_USED` | Fallback-Preis verwendet | Nein |
| `WARN_TAX_FALLBACK_USED` | Fallback-Steuer verwendet | Nein |
| `WARN_INVALID_MIME` | Bild-MIME ungültig | Nein |
| `WARN_IMAGE_TOO_LARGE` | Bild zu groß | Nein |
| `WARN_IMAGE_DOWNLOAD_FAILED` | Bild-Download fehlgeschlagen | Nein |
| `WARN_HALLUCINATION_BLOCKED` | KI-Faktentreue verletzt | Nein |
| `WARN_DB_LOCK_RETRY` | DB-Lock-Retry | Nein |

Konvention: `ERR_` = blockierend, `WARN_` = nicht blockierend.

## 5. Ausführungsreihenfolge pro SKU

1. `CatalogValidationService.validate(ProductSource) -> CatalogValidationResult`
2. `DescriptionEnrichmentService.enrich(ProductSource, locale) -> DescriptionSeoResult`
3. `MediaIngestionService.ingest(ProductSource) -> MediaIngestionResult`
4. `VariantGenerationService.build(ProductSource) -> VariantResult`
5. `IdempotencyManager.upsert(...) -> ...`
6. `sales_release_orchestrator`: Shopware-Create/Update, Readback, Lifecycle-Status
7. `ObservabilityMixin.log(...)`

Fehlerisolierung pro SKU; blockierende Fehler stoppen den Import für diese SKU.

## 6. Go-Live-Gate und Sicherheitsregeln

### 6.1 Active-Entscheidung

`active = False`, wenn mindestens eine Bedingung vorliegt:

- blocking errors vorhanden
- `price_fallback_used == True`
- `cover_url is None` nach erfolgreicher Media-Ingestion
- `export_gate != APPROVED`
- `hallucination_blocked == True`

`active = True` nur wenn alle Bedingungen nicht vorliegen und Readback erfolgreich.

### 6.2 Fallback-Preis / Go-Live

Ein Produkt mit Fallback-Preis darf importiert werden, erhält Warnung `WARN_PRICE_FALLBACK_USED`, bleibt aber zwingend `active = False`.

Ein Produkt ohne gültiges Produktbild (`cover_url is None`) darf nicht live gehen.

### 6.3 Preflight / Start-Sales

`run_preflight` und `start_sales` bleiben Freigabe-Gate.

## 7. SQLite-WAL und Concurrency

- Kurze Transaktionen.
- `BEGIN IMMEDIATE` für Schreibabschnitte.
- `busy_timeout = 5000 ms`.
- Retry-on-Lock: max 3 Versuche, exponentieller Backoff, dann Item `FAILED`.
- Netzwerk-Calls getrennt von DB-Transaktionen.
- Atomare Upserts.

## 8. Unit-Test-Plan pro Service

Siehe Sektion 9.

## 9. Abnahmekriterien pro Arbeitspaket

AP1 bis AP7.

## 10. User-Freigabe und vier Präzisierungen

### Präzisierung 1 – Medien & Alt-Text-Positionen

Bei isoliertem Bildfehler: Positionen neu indizieren, `alt_texts` neu auf Basis gültiger Assets; erstes gültiges Bild wird Cover (`position = 0`) mit passendem Alt-Text.

### Präzisierung 2 – Go-Live-Gate

`cover_url is None` zwingt `active = False`.

### Präzisierung 3 – SSRF-Hardening

`MediaIngestionService` verwendet `follow_redirects=False`; Scheme nur `http`/`https`; optional Redirect-Hop erneut prüfen.

### Präzisierung 4 – Idempotency-Key

Trennung:

- Job-Item-Deduplizierung: `job_id|sku`
- Payload-Hash-Erkennung: `sku|content_hash`
- Persistierung: `ON CONFLICT(sku) DO UPDATE`

Bestehende `shopware_product_id` wiederverwenden; unveränderte Payloads als No-Op.

## 11. Offene Punkte

- Spec-Review durch User
- Implementierung AP1–AP7

---

Dieses Dokument wird vor Implementierung reviewed und bei Änderungen aktualisiert.
"""

print("written")
from hermes_tools import read_file, write_file
# verify write
r = read_file(path="docs/superpowers/specs/2026-11-24-ai-import-pipeline-design.md")
print("READBACK OK, chars:", len(r.get("content") or ""))
