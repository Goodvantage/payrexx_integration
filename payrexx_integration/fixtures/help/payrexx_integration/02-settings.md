---
title: Payrexx Settings einrichten
slug: payrexx-integration-settings
category: Payrexx Integration
level: Intermediate
---

# Payrexx Settings einrichten

Der **Payrexx Settings** Datensatz enthält die Zugangsdaten und das Verhalten für eine Payrexx Umgebung.

![Payrexx Settings Formular mit Webhook URL, Zugangsdaten und Verhalten](/assets/payrexx_integration/images/help/02-payrexx-settings-form.png)

## Felder

- **Gateway Name**: eindeutiger Name wie `Sandbox` oder `Live`; ein separates Environment-Feld gibt es nicht.
- **Instance Name**: erster Teil der Payrexx Instanz, zum Beispiel `customer`.
- **API Base Domain**: `payrexx.com` oder die Plattform-Domain, zum Beispiel `pay.goodvantage.ch`.
- **API Secret**: Schlüssel aus Payrexx für die API.
- **Webhook Signing Key**: separater Schlüssel zur Prüfung eingehender Webhooks.
- **Automation User**: aktiver System User für Checkout, Verbuchung und
  Abstimmung dieses Gateways.
- **Supported Currencies**: kommagetrennte Währungen, die dieser Gateway akzeptiert.
- **PSP Whitelist**: optionale kommagetrennte Payrexx-PSP-IDs.
- **Gateway Validity**: optionale Gültigkeit des Checkout-Links in Minuten.
- **Allow TEST Transactions**: nur auf einem reinen Sandbox-Gateway aktivieren.
- **Enable Managed Subscriptions**: standardmässig aus. Erst nach einem
  signierten Sandbox-Test aktivieren, bei dem Lifecycle-Webhook,
  Transaction-Recovery und ein Folgeeinzug nachgewiesen wurden. Das Abschalten
  blockiert neue Mandate, aber nicht die Abstimmung bestehender Abonnemente.
- **Transaction Reconciliation Cursor (UTC)**: schreibgeschützter Zeitstempel
  des letzten vollständig erfolgreichen Transaction-Fensters.
- **Redirect Overrides**: optionale globale Ziel-URLs für Erfolg, Fehler und Abbruch.

Die API-Version ist in der App fest auf `v1.16` gesetzt und kann nicht pro
Gateway geändert werden. Ein Versionswechsel erfolgt als getestetes App-Release.

Payrexx-eigene API-Hosts unter `payrexx.com` sind standardmässig erlaubt. Eine
Plattform-Domain wie `pay.goodvantage.ch` muss zusätzlich als exakter finaler
API-Host in der Site-Konfiguration freigegeben werden:

```bash
bench --site <site> set-config --parse payrexx_allowed_api_hosts '["api.pay.goodvantage.ch"]'
```

Die Freigabe ist eine JSON-Liste ohne Schema, Pfad oder Wildcards. IP-Adressen,
Benutzerinformationen, Query/Fragment und andere Ports als HTTPS 443 werden
abgelehnt, bevor das API Secret gelesen wird. Nach einer Änderung die
langlaufenden Web- und Worker-Prozesse neu starten.

Bei einem eigenen API-Host wiederholt die App eine abgelehnte Anfrage nach
401/403 einmal auf `api.payrexx.com`; bei der Gateway-Erstellung gilt dies auch
für 404. Eine Gateway-Erstellung kann deshalb denselben POST einmal pro Host
senden. Das ist kein Rate-Limit- oder Datenbank-Deadlock-Retry. Für den POST gibt
es keinen Idempotency Key, und Payrexx dokumentiert `referenceId` nicht als
eindeutig. Beim offiziellen PHP SDK v2.0.15 ruft der Communicator pro API-Aufruf
den konfigurierten Adapter einmal auf und besitzt keinen eingebauten Host-
Fallback oder Idempotency Key; ein eigener Adapter kann trotzdem mehrere
Netzwerkanfragen senden. Der Code verwendet standardmässig API `v1.15`, während
die README desselben Tags widersprüchlich API `v1.11` und SDK v2.0.0 als aktuell
nennt. Die App bleibt bewusst auf API `v1.16`; ihr Form-POST ist weiterhin ein
offiziell dokumentiertes Payrexx-Format.

Sobald **Gateway Name** ausgefüllt ist, zeigt bereits das ungespeicherte Formular die Webhook URL an. Diese URL zuerst in Payrexx anlegen, den dort erzeugten Signing Key in **Webhook Signing Key** eintragen und erst danach speichern. Beim Speichern werden die API-Zugangsdaten geprüft und der Payment Gateway `Payrexx-<Gateway Name>` erzeugt.

## Payment Gateway Account

Der erzeugte Payment Gateway allein reicht für Zahlungen nicht aus:

1. **Payment Gateway Account** öffnen und einen neuen Datensatz erstellen.
2. Den erzeugten Gateway, zum Beispiel `Payrexx-Live`, auswählen.
3. Zahlungskonto und Firma festlegen. ERPNext übernimmt die **Währung** aus dem
   Zahlungskonto; dessen Kontowährung muss zu den Rechnungen passen.
4. **Is Default** aktivieren, wenn diese Kombination als Standard dienen soll.
5. Für jede verwendete Firma/Währung einen eindeutigen Datensatz anlegen.

Fehlt dieser Datensatz, kann der erste Klick auf einen gültigen Rechnungslink keinen Payment Request erstellen.

## Vor Live-Schaltung prüfen

1. Sandbox-Zahlung erfolgreich testen.
2. Webhook in Payrexx mit der angezeigten URL hinterlegen.
3. Payment Gateway Account für Testfirma und Testwährung anlegen.
4. Prüfen, ob Integration Request, Payment Request, Payment Entry und Rechnung korrekt aktualisiert werden.
5. Danach erst Live-Zugangsdaten und Live-Payment-Gateway-Account hinterlegen.

Der separate Live-Nachweis des Custom-Host-Fallbacks ist aufgeschoben, bis ein
eigener kontrollierbarer Sandbox-API-Host und eine bestätigte Berechtigung zum
Löschen leerer Gateways vorhanden sind. Er muss den Client einmal direkt ohne
Speichern der Settings und ohne Payment Request/Integration Request aufrufen,
die Settings vor der Fehlerinjektion prüfen, null/einen/mehrere externe Gateways
exakt behandeln und Settings sowie Allowlist in `finally` immer
wiederherstellen. Diesen Test nie auf einem gemeinsam genutzten oder produktiven
Host durchführen.

## Häufige Fragen

**Speichern schlägt fehl.**
Prüfen Sie Instanzname und API Secret. Bei ungültigen Zugangsdaten lehnt Payrexx die Verbindung ab.

**Webhook kommt nicht an.**
Prüfen Sie die Webhook URL im Formular und den Signing Key in Payrexx.
