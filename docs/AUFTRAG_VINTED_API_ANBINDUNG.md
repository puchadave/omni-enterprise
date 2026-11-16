# Programmierauftrag (final): Vinted/Vintage-API-Anbindung – Kauf, Bestellung, Zahlungsabwicklung

**Priorität:** P0 (90-Minuten-Serveranbindung zuerst), danach inkrementelle Connector-Implementierung.  
**Scope-Guard:** Produktivcontainer `310b4d4eb7c1` (:8002) und DB `1c51e239de30` **nicht** stoppen/rebuild/löschen. Änderungen erfolgen in Repo/Bundle (`middleware-dockercompose/app`, `/mnt/social.puchalla.pro/docker-swarm/`) und werden über regulären Deploy-Pfad ausgerollt.

## 0. Annahmen & Freigaben (blockierende Klärung)
- **Vinted hat keine offen dokumentierte öffentliche Merchant-API.** Umsetzung als capability-gesteuerter Connector gegen den tatsächlich verfügbaren Zugang (Vinted Pro/Partner/zugelassene Endpunkte). Bis Auth/Endpoints bestätigt sind, läuft der Client im `config_required`/Mock-fähigen Modus (kein ToS-Bruch, kein Scraping ohne Freigabe).
- **„Kauf“ (Einkauf/Checkout auf Vinted):** nur als explizite, auditierte Aktion implementieren, wenn ein legitimer API-Pfad + Freigabe vorliegt; andernfalls als `NOT_SUPPORTED` melden. Fokus: **Verkauf/Order-Sync**.
- **„Zahlungsabwicklung“:** Vinted-captured Payment wird **synchronisiert/verrechnet/freigegeben** (kein eigenes Capture außerhalb Vinted, außer bestätigter Flow). Versand/Dropship **nur** bei bestätigtem `payment_status=paid` & nicht gelockt (bestehender Guard bleibt unverändert verbindlich).
- **Benötigt vom Auftraggeber:** Kontotyp/Markt, API-/Partnerzugang, Auth-Methode (OAuth/Token/Session), Basis-URL/Endpoint-Doku, Webhook-Verfügbarkeit/Secret, Testkonto & Testartikel/Bestellung.

## 1. Architektur (Adapter-Pattern, an bestehendes SDK)
- **SDK/Registry:** `services/connector_sdk.py`, `services/connector_registry.py`, `services/connector_service.py`, `connector_accounts.credentials_json`.
- **Neu:**
  - `app/services/connectors/vinted_client.py` – konfigurierbarer HTTP-Client (Basis-URL aus Credentials, Auth-Header/Token-Refresh/Session), Retry/Backoff/Rate-Limit, Timeout, strukturierte Fehler, **keine Secrets/PII in Logs**.
  - `app/services/connectors/vinted.py` – `PuchaVintedConnector(SalesChannelConnector)` (`connector_id="pucha_vinted"`, `platform_name="Vinted"`).
  - `app/services/vinted_sync.py` – Order/Listing-Sync, Idempotenz, Status-Mapping, Payment→Finance.
- **Capabilities (capability-gesteuert):** `PRODUCT_CREATE/UPDATE/DELETE`, `INVENTORY_SYNC`, `PRICE_SYNC`, `ORDER_IMPORT`, `FULFILLMENT_UPDATE`, `LISTING_STATUS` (aktiv nur bei bestätigter API-Funktion).

## 2. Datenmodell & Migrationen (idempotent, `database.py`-Muster)
- **Connector:** Konto in `connector_accounts` (`connector_id='pucha_vinted'`), Mapping in `connector_mappings`, Export/Sync-Status in `connector_exports`/Logs.
- **Orders (kanonisch, kompatibel):** Nutzung/Erweiterung `orders` um kanalbezogene Felder falls fehlend (idempotent `ALTER/ADD COLUMN IF NOT EXISTS`): `marketplace/channel='vinted'`, `external_order_id`, `channel_account_id`, `external_status`, `raw_payload` (JSON, minimiert), `buyer_ref`, `currency`, `total_amount`, `shipping_*`, `tracking_*`. **Dedupe-Constraint/Index:** unique `(marketplace, external_order_id)` (bzw. channel+external) für idempotenten Import.
- **Payment/Finance:** Zuordnung Payment-Events → `orders.payment_status` + `finance_transactions` (Referenz `order_reference`/`external_order_id`, Betrag/Währung, Gebühren/Netto als Metadaten soweit geliefert) für `reconcile_payment_to_documents`.
- **Sync-Cursor/Jobs:** Tabelle `connector_sync_state` (account_id, stream=`orders|listings`, cursor, last_ok_at, last_error) oder Settings-Key analog bestehenden Sync-Jobs.

## 3. Bestellabwicklung (Order-Sync)
- **Ingestion:** Polling-Job (Scheduler, inkrementell via Cursor/updated_since) + optional Webhook-Handler `/api/webhooks/vinted/{account_id}` (Signaturprüfung falls Secret vorhanden).
- **Normalisierung → `orders`:** Positionen/SKU-Mapping (über `connector_mappings`/SKU), Mengen, Preise, Käufer/Adresse (nur erlaubte Felder), Status, Timestamps.
- **Idempotenz:** Upsert per `external_order_id`; wiederholte Webhooks/Polls erzeugen keine Duplikate (Event/Import-Log dedupliziert).
- **Writeback:** Versandlabel/Tracking aus Middleware → Vinted (`FULFILLMENT_UPDATE`) nur wenn Capability+Status erlaubt.

## 4. Zahlungsabwicklung (Status, Guard, Verbuchung)
- **Status-Mapping (konservativ):** `pending/awaiting_payment`→`pending`, `paid/authorized/captured/released`→`paid`, `failed/cancelled/expired`→`failed`, `refunded/partially_refunded`→`refunded` (partiell als Metadatum + Review).
- **Freigabe/Guard:** Bei Übergang auf `paid` Event `order.paid`/Fulfillment freigeben **nur** über bestehende Logik (`payment_status=='paid'` && `!fulfillment_locked`) – **kein** Versand bei `unpaid` (unverändert).
- **Finance:** `finance_transactions` erfassen + reconcile (`reconcile_payment_to_documents`), Betrag/Währung/Referenz konsistent; Gebühren nur wenn geliefert, sonst kennzeichnen.
- **Audit:** jede Statusänderung/Writeback als Event/Log (wer/was/wann, Quelle Vinted).

## 5. „Kauf“-Funktion (abgegrenzt)
- **Nicht Standard:** Kauf/Checkout auf Vinted im Namen des Nutzers wird **nicht** automatisiert ohne bestätigten, ToS-konformen API-Pfad.
- **Falls freigegeben:** eigener geschützter Endpoint/Action (RBAC, 2-step confirm), Audit-Log, idempotent, nur deklarierte Capability – sonst Response `501/unsupported` mit Begründung.

## 6. API & WebGUI
- **API (`main.py`/Router, RBAC, konsistent zu `/api/connectors/*`):**
  - `POST /api/connectors/vinted/accounts` (anlegen), `POST .../test`, `GET .../accounts`, `PATCH .../accounts/{id}`
  - `POST /api/connectors/vinted/{account_id}/sync/orders` & `/sync/listings` (manuell)
  - `GET /api/connectors/vinted/{account_id}/orders|listings|status`
  - `POST /api/webhooks/vinted/{account_id}` (optional, signaturgeprüft)
- **UI:** *Marktplätze & Kanäle* – Vinted-Kachel (Connect/Test, Sync, Status/Fehler, Mappings), Orderdetail um Kanal/Payment/Tracking erweitern (Dark-UI).

## 7. Jobs, Betrieb & Sicherheit
- Scheduler-Jobs: `vinted.order.sync`, `vinted.listing.sync` (Intervall konfigurierbar, Backoff bei Fehlern).
- Secrets nur in `credentials_json`/Secret-Store, maskierte Ausgabe; Rate-Limit beachten; PII-Minimierung.
- Observability: strukturierte Logs (ohne Secrets), Sync-State/Fehlerzähler, Health-Capability.

## 8. Tests & Abnahme
- **Unit:** Status-Mapping, Idempotenz/Dedupe, Payment→`paid`/Guard-Interaktion (kein Fulfillment bei unpaid), Finance-Reconcile, Client-Retry/Fehler (gemockt).
- **Integration (gemockt):** Import→Order→Payment paid→Freigabe; Duplikat-Webhook; Tracking-Writeback; `config_required` ohne Credentials.
- **Abnahme:** (1) Konto verbinden/testen, (2) Listings/Bestand sync, (3) Testbestellung importiert & korrekt gemappt, (4) Zahlung `paid` verbucht/freigegeben, (5) Tracking zurückgemeldet, (6) keine Regression Payment-Guard.

## 9. 90-Minuten-Deliverables (Server/Anbindung)
- `platform/middleware` & `platform/omniroute` lauffähig + verbunden (OmniRoute erreichbar, Middleware konfiguriert auf `OMNIROUTE_*`), Healthchecks grün.
- Swarm-Bundle unter `/mnt/social.puchalla.pro/docker-swarm/` mit `scripts/deploy-cluster.sh`, Placement (pve01–pve04), kombinierten/Swarm-Stacks, `opencode.json`/Coding-Swarm-Configs, Secret-Templates.
- Vinted-Connector-Skeleton + Auftrag (dieses Dokument) eingecheckt; produktive Live-Instanz unverändert.
