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

- **Instance Name**: Payrexx Instanzname.
- **API Secret**: Schlüssel aus Payrexx für die API.
- **API Version**: verwendete Payrexx API-Version.
- **Webhook Signing Key**: Schlüssel zur Prüfung eingehender Webhooks.
- **Supported Currencies**: Währungen, die dieser Gateway akzeptiert.
- **Gateway Validity**: Gültigkeit des Checkout-Links, falls begrenzt.

Nach dem Speichern zeigt das Formular die Webhook URL an, die in Payrexx hinterlegt werden muss.

## Vor Live-Schaltung prüfen

1. Sandbox-Zahlung erfolgreich testen.
2. Webhook in Payrexx mit der angezeigten URL hinterlegen.
3. Prüfen, ob die Zahlung in ERPNext ankommt.
4. Danach erst Live-Zugangsdaten hinterlegen.

## Häufige Fragen

**Speichern schlägt fehl.**
Prüfen Sie Instanzname und API Secret. Bei ungültigen Zugangsdaten lehnt Payrexx die Verbindung ab.

**Webhook kommt nicht an.**
Prüfen Sie die Webhook URL im Formular und den Signing Key in Payrexx.
