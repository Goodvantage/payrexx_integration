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
- **API Version**: normalerweise `v1.14`.
- **API Secret**: Schlüssel aus Payrexx für die API.
- **Webhook Signing Key**: separater Schlüssel zur Prüfung eingehender Webhooks.
- **Supported Currencies**: kommagetrennte Währungen, die dieser Gateway akzeptiert.
- **PSP Whitelist**: optionale kommagetrennte Payrexx-PSP-IDs.
- **Gateway Validity**: optionale Gültigkeit des Checkout-Links in Minuten.
- **Redirect Overrides**: optionale globale Ziel-URLs für Erfolg, Fehler und Abbruch.

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

## Häufige Fragen

**Speichern schlägt fehl.**
Prüfen Sie Instanzname und API Secret. Bei ungültigen Zugangsdaten lehnt Payrexx die Verbindung ab.

**Webhook kommt nicht an.**
Prüfen Sie die Webhook URL im Formular und den Signing Key in Payrexx.
