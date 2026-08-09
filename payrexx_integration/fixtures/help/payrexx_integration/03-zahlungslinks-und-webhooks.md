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

Ein bestehender Checkout wird nur wiederverwendet, solange die Rechnung
vollständig offen und der eingereichte Payment Request weiterhin **Requested**
und vollständig offen ist. Betrag, Währung, Rechnung, Gateway und gespeicherte
Payrexx-Metadaten müssen exakt übereinstimmen. Nach einer Teilzahlung oder einer
anderen Änderung wird der alte Vollbetrags-Checkout vor jedem Kontakt mit
Payrexx blockiert. In diesem Fall Payment Entries und Integration Request prüfen
und den alten Link nicht erneut öffnen.

Vor einem neuen Gateway prüft die App unter einer aktuellen Sperre alle
eingereichten aktiven Payrexx Payment Requests derselben Rechnung, unabhängig
vom gewählten Payrexx-Settings-Eintrag. Existiert bereits ein anderer aktiver
Request, wird der neue Request unverändert erhalten und vor dem Provider-Kontakt
abgelehnt. Bezahlte, fehlgeschlagene, stornierte und abgebrochene historische
Requests bleiben erhalten und blockieren keinen legitimen neuen Checkout.

Ein POST direkt an `api.payrexx.com` wird weder wegen eines Rate Limits noch
nach einem Datenbank-Deadlock nach Provider-Kontakt wiederholt. Bei einem
konfigurierten eigenen API-Host besteht jedoch ein separater Fallback: Die
Gateway-Erstellung sendet denselben POST nach 401/403/404 einmal an
`api.payrexx.com`. Payrexx bietet dafür keinen Idempotency Key und dokumentiert
`referenceId` nicht als eindeutigen Deduplizierungsschlüssel.

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

Die lokalen Checkout-Datensätze werden gemeinsam gespeichert: Ein Fehler bei
Payrexx hinterlässt keinen vorzeitig bestätigten lokalen Payment Request oder
unvollständigen lokalen Integration Request. Ein extern bereits erzeugter
Gateway kann aber nicht Teil dieser Datenbanktransaktion sein. Nach einer
Gateway-Antwort enthält
`sites/<site>/logs/payrexx_integration.log` zuerst
`[Payrexx Gateway recovery] state=local_commit_pending`. Ein normaler Commit
ergänzt `state=local_commit_confirmed`, ein Rollback
`[Payrexx possible orphan Gateway] state=local_rollback_confirmed`. Bleibt ein
Pending-Eintrag ohne Ergebnis, ist der SQL-Commit-Ausgang unklar. Den lokalen
Integration Request und den Gateway über `referenceId`/Gateway-ID in Payrexx
prüfen. Payrexx unterstützt `DELETE /Gateway/{id}/`, die App stellt dafür aber
keinen Wrapper bereit. Gibt es keinen Gateway, ist nichts zu löschen. Nur mit
ausdrücklicher Provider-Berechtigung jeden exakten gefundenen Gateway einzeln
und nur dann löschen, wenn sein Abruf sowie die Transaktionssuche zeigen, dass
alle `invoices[].transactions[]` leer sind. Gateways mit Transaktion oder
unklarem Host/Ausgang nicht erneut bezahlen oder löschen, sondern die bestehende
Evidenz abstimmen.

## Nicht unterstützte Aktionen

Die App startet keine spätere Belastung für `authorized`, keinen Capture für `reserved`, keine Stornierung/Voids und keine Rückerstattung. Payrexx unterstützt das Löschen eines Gateways, die App stellt dafür jedoch keinen Gateway-DELETE-Wrapper bereit. Ein `refunded` Webhook wird gespeichert, aber nicht automatisch in ERPNext verbucht oder zurückgebucht. Solche Aktionen in Payrexx ausführen und die freigegebene Gegenbuchung in ERPNext manuell erfassen. Gateways nur mit exaktem Nachweis löschen, dass keine Transaktion vorhanden ist. Eingereichte Payment Entries nicht allein aufgrund eines Webhook-Status automatisch stornieren.

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
