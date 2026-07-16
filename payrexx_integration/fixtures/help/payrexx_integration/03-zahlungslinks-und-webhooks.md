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
2. Der Integration Request wird als **Completed** gespeichert.
3. Der Payment Request wird **Paid** und erhält genau einen eingereichten Payment Entry.
4. Der offene Betrag der Rechnung wird über den Payment Entry aktualisiert.
5. Die Fach-App kann eine Bestätigung anzeigen oder weitere E-Mails verschicken.

## Abstimmung

Bei einer bestätigten Zahlung immer die gesamte Kette prüfen:

- Integration Request: richtiger Gateway, gespeicherte Payrexx-Transaktion, Status **Completed**
- Payment Request: Status **Paid**, offener Betrag `0`
- Payment Entry: genau ein eingereichter Eintrag mit Bezug auf den Payment Request
- Sales Invoice: offener Betrag entsprechend dem eingereichten Payment Entry

Bei einem Chargeback bleibt der eingereichte Payment Entry unverändert. Die App setzt den Integration Request auf **Failed** und erstellt einmalig ein dringendes ToDo für die manuelle buchhalterische Gegenbuchung.

## Nicht unterstützte Aktionen

Die App startet keine spätere Belastung für `authorized`, keinen Capture für `reserved`, keine Stornierung/Voids und keine Rückerstattung. Ein `refunded` Webhook wird gespeichert, aber nicht automatisch in ERPNext verbucht oder zurückgebucht. Solche Aktionen in Payrexx ausführen und die freigegebene Gegenbuchung in ERPNext manuell erfassen. Eingereichte Payment Entries nicht allein aufgrund eines Webhook-Status automatisch stornieren.

## Fehleranalyse

Prüfen Sie bei Unklarheiten:

- Payrexx Dashboard: Wurde die Zahlung bestätigt?
- Payrexx Settings: Stimmt die Webhook URL?
- Payment Gateway Account: Stimmen Gateway, Firma, Währung und Zahlungskonto?
- Integration Request: Kam die Rückmeldung in ERPNext an?
- Payment Request und Payment Entry: Wurde die Zahlung genau einmal verbucht?
- Email Queue: Wurde das E-Mail mit Zahlungslink verschickt?

## Sicherheit

Zahlungslinks sind signiert. Sie sollten nicht manuell nachgebaut oder in externen Dokumenten verändert werden.
