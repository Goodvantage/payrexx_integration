---
title: Zahlungslinks und Webhooks
slug: payrexx-integration-zahlungslinks-webhooks
category: Payrexx Integration
level: Intermediate
---

# Zahlungslinks und Webhooks

Zahlungslinks werden aus ERPNext-Rechnungen erzeugt und führen in den Payrexx Hosted Checkout. Webhooks melden anschliessend zurück, ob die Zahlung erfolgreich war.

## Zahlungslink im E-Mail

Der Zahlungslink sollte in Rechnungs- oder Bestätigungs-E-Mails als gut sichtbarer Button erscheinen. Empfängerinnen und Empfänger müssen nicht im Desk angemeldet sein.

## Erfolgreiche Zahlung

Nach erfolgreicher Zahlung passiert im Normalfall Folgendes:

1. Payrexx bestätigt die Zahlung.
2. Der zugehörige Zahlungsstatus wird verarbeitet.
3. ERPNext zeigt die Zahlung bzw. den Integration Request als abgeschlossen.
4. Die Fach-App kann eine Bestätigung anzeigen oder weitere E-Mails verschicken.

## Fehleranalyse

Prüfen Sie bei Unklarheiten:

- Payrexx Dashboard: Wurde die Zahlung bestätigt?
- Payrexx Settings: Stimmt die Webhook URL?
- Integration Request: Kam die Rückmeldung in ERPNext an?
- Email Queue: Wurde das E-Mail mit Zahlungslink verschickt?

## Sicherheit

Zahlungslinks sind signiert. Sie sollten nicht manuell nachgebaut oder in externen Dokumenten verändert werden.
