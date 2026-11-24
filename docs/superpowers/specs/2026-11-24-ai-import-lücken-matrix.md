# Lücken- & Risikomatrix: AI-gestützte Produktimport-Pipeline

## 1. Kritische Fehlstellen (P0)

| ID | Fehlstelle | Betroffene Datei | Priorität | Technisches Risiko | Beeinflusste Funktionalität |
|----|------------|------------------|-----------|-------------------|---------------------------|
| P0.1 | Unvollständige Fehlerbehandlung in Matterhorn Importer | `modules/middleware/app/services/matterhorn_importer.py` (Zeile 213) | P0 | **Hoch** - Ungehandled Exceptions brechen Importer ab | Alle Importe (P0) |
| P0.2 | Placeholder-Code im Retail Stock-Handling | `modules/middleware/app/services/matterhorn_importer.py` (Zeile 204) | P0 | **Hoch** - Falsche Logik führt zu falschem Lagerbestand | Lagerbestand, Preise, Margenberechnung |
| P0.3 | Fehlende EAN/GTIN-Validierung | `modules/middleware/app/services/canonical_product_mapping.py` | P0 | **Mittel** - Ungültige EANs im Katalog | Produktidentifikation, Steuerberechnung, Reporting |
| P0.4 | Unzureichende SEO-Validierung | `modules/middleware/app/services/shopware_description_engine.py` | P0 | **Mittel** - Überlange Meta-Title/Description brechen Shopware | SEO, Produktfindbarkeit, Conversion |
| P0.5 | Keine Bild-Safety-Checks | `modules/middleware/app/services/matterhorn_importer.py` | P0 | **Hoch** - SSRF, Timeout, File-Type-Attacken | Produktbilder, Medien-Import, SSRF-Schutz |

## 2. Risikomatrix

| Risiko | Eintrittswahrscheinlichkeit | Auswirkung | Gesamtrisiko | Betroffene Module |
|--------|---------------------------|------------|--------------|-------------------|
| **SSRF-Schutz fehlt** | Hoch (Bilder aus externen URLs) | **Hoch** (Serverkompromittierung) | **Hoch** | Matterhorn Importer, Media-Handling |
| **Ungültige EAN/GTIN-Daten** | Mittel (Datenlieferant) | **Mittel** (Falsche Steuerberechnung, falsche Lagerbestände) | **Mittel** | Canonical Mapping, Shopware Import |
| **SEO-Überlänge** | Hoch (Längengrenzen nicht geprüft) | **Mittel** (Produkt nicht freigabelinklich) | **Mittel** | Beschreibung Engine, Shopware Export |
| **Image-Dateityp-Attacken** | Mittel (JPEG mit .png-Extension) | **Hoch** (Serverabsturz, Security-Breach) | **Hoch** | Media-Handling, Shopware Media API |
| **Fehlende Idempotenz** | Mittel (Re-Import-Requests) | **Mittel** (Duplikate, falsche Lagerbestände) | **Mittel** | Job-Item-Tabellen, Shopware Update-Logik |

## 3. Technische Lücken (P1/P2)

| ID | Lücke | Priorität | Betroffene Datei | Empfohlene Maßnahme |
|------|-------|-----------|------------------|---------------------|
| P1.1 | Keine SSRF-Schutzmechanismen | P1 | `matterhorn_importer.py`, `shopware_description_engine.py` | Implementieren Sie URL-Whitelist mit `validators.url` und `httpx` mit `timeout=10s` |
| P1.2 | Keine Image-Dateityp-Validierung | P1 | `matterhorn_importer.py` | Prüfen Sie MIME-Type mit `python-magic` oder `Pillow`, akzeptieren Sie nur `image/jpeg`, `image/png` |
| P1.3 | Keine Dateigrößenbegrenzung | P1 | `matterhorn_importer.py` | Implementieren Sie 10MB Upload-Limit mit `hio`-basiertem Streaming |
| P1.4 | Keine SEO-Längengrenzen | P1 | `shopware_description_engine.py` | Validieren Sie Titel (<60 Zeichen), Description (<160 Zeichen), Slug (<128 Zeichen) |
| P1.5 | Ungültige EAN/GTIN-Daten | P1 | `canonical_product_mapping.py` | Validieren Sie EAN mit `if len(ean) == 13 or len(ean) == 8` + Prüfziffer |
| P1.6 | Fehlende Fehler-Code-Spezifikation | P2 | `shopware_export_manager.py` | Definieren Sie standardisierte Fehler-Codes (z.B. `ERR_001` = "Invalid EAN") |
| P1.7 | Keine Alt-Text-Generierung | P2 | `shopware_description_engine.py` | Implementieren Sie KI-generierte Alt-Texte mit `content_ai_engine` |
| P1.8 | Fehlende Preis-/Steuer-Fallback-Logik | P2 | `shopware_mapping_engine.py` | Implementieren Sie fallback-Mechanismus für fehlende Preise (Standardsteuersatz) |
| P1.9 | Ungenügende Beobachterbarkeit | P2 | `matterhorn_importer.py` | Fügen Sie strukturiertes Logging mit `job_id`, `status`, `error_code` hinzu |
| P1.10 | Fehlende Kategorie-Auto-Create-Logik | P2 | `shopware_mapping_engine.py` | Implementieren Sie Whitelist-basierte Kategorie-Erstellung mit Shopware-API |

## 4. Risikobewertung nach BSI IT-Grundschutz

| Risiko | ISMS-Klasse | Begründung |
|--------|-------------|------------|
| SSRF-Schutz | **Kritisch** | Unbehandelte externe URL-Nutzung kann zu Serverkompromittierung führen (§7.14 ISMS) |
| Ungültige EAN-Daten | **Hoch** | Falsche Steuerberechnung und Lagerbestände verletzen §5.11 (Datenintegrität) |
| Image-Datei-Attacken | **Kritisch** | Dateityp-Manipulation kann zu Code-Ausführung führen (§7.8) |
| Fehlende Idempotenz | **Mittel** | Duplikate verletzen §6.5 (Vermeidung von Datenanomalien) |

## 5. Empfohlene Priorisierungsreihenfolge

1. **SSRF-Schutz & Image-Validierung** (P0.5, P1.2, P1.3) - Schutz vor kritischen Sicherheitslücken
2. **SEO-Validierung & Längengrenzen** (P0.4, P1.4) - Einhaltung Shopware-Freisabelogungsanforderungen
3. **EAN/GTIN-Validierung** (P0.3, P1.5) - Datenintegrität für Steuer- und Reporting-Funktionen
4. **Idempotenz-Implementierung** (P1.7) - Vermeidung von Duplikaten im Katalog
5. **Fehler-Code-Spezifikation** (P1.6) - Einheitliche Fehlermeldungen für Support und Monitoring

## 6. Abhängigkeiten zu Sicherheitsregeln

- **§7.14 ISMS (Sicherheitsarchitektur):** SSRF-Schutz, Image-Validierung
- **§5.11 ISMS (Datenintegrität):** EAN/GTIN-Validierung, Lagerbestand-Berechnung
- **§6.5 ISMS (Datenanonymisierung):** Idempotenz, Duplikatvermeidung
- **§8.1 ISMS (Zugriffskontrolle):** API-Sicherheit, Authentifizierung der Importer-API
- **§9.1 ISMS (Verschlüsselung):** HTTPS-Pflicht für alle externen API-Calls

## 7. Verifikation der Lücken

| Lücke | Verifikation | Status |
|-------|--------------|--------|
| SSRF-Schutz | Pen-Test mit externen URLs (z.B. `http://169.254.1.1`) | **Offen** |
| Image-Validierung | Test mit JPEG-Datei (.png-Extension) | **Offen** |
| SEO-Längengrenzen | Test mit 200-Zeichen-Title | **Offen** |
| EAN-Validierung | Test mit 12-stelligen EAN | **Offen** |
| Idempotenz | Test mit gleichem Job-Id + Re-Import | **Offen** |

## 8. Offene Fragen für Rückfragen

1. **Kategorie-Auto-Create:** Soll die Whitelist für Kategorie-Erstellung Shopware-Systemkategorien oder benutzerdefinierte Kategorien erlauben?
2. **Preis-/Steuer-Fallback:** Welcher Standard-Steuersatz ist bei fehlenden Preisdaten zu verwenden? (19% MwSt?)
3. **SSRF-Whitelist:** Welche Domains/IP-Ranges sind als vertrauenswürdig zu definieren? (z.B. interne Netzwerke, Shopware-Hosts)
4. **Alt-Text-Generierung:** Welcher KI-Provider ist für die Beschreibung verfügbar? (Ollama, OpenAI, Gemini?)
5. **EAN-Prüfziffer:** Soll die Validierung die Prüfziffer (Checksum) der EAN-13 validieren?
6. **Retry-Logik:** Welche maximale Anzahl von Retry-Versuchen ist pro Item erlaubt? (z.B. 3, 5, 10?)

This document represents the current state of gap analysis based on the repository content. The P0 issues identified require immediate attention as they represent critical security and data integrity risks.

Let me now check if there are any existing test files that document the expected behavior for these critical paths.
<tool_call>
<function=search_files>
<parameter=pattern>
test.*ai.*import|import.*test|ai.*job.*test