---
title: Payrexx Integration
slug: payrexx-integration-uebersicht
category: Payrexx Integration
level: Beginner
---

# Payrexx Integration

Payrexx Integration verbindet ERPNext-Rechnungen mit dem Payrexx Hosted Checkout. Die App stellt pro Umgebung einen **Payrexx Settings** Datensatz bereit und erzeugt daraus den passenden Payment Gateway.

![Payrexx Settings Liste im Desk](/assets/payrexx_integration/images/help/01-payrexx-settings-list.png)

## Wofür wird es genutzt?

- Zahlungslinks in E-Mails
- Online-Zahlungen für Rechnungen
- Payrexx Webhook-Rückmeldungen
- automatische Zuordnung zur ERPNext Zahlung

## Grundprinzip

1. Eine Rechnung wird erstellt.
2. Das E-Mail enthält einen sicheren Zahlungslink.
3. Beim Klick öffnet sich der Payrexx Checkout.
4. Nach erfolgreicher Zahlung meldet Payrexx den Status zurück.
5. ERPNext aktualisiert die Zahlungsinformationen.

## Häufige Fragen

**Braucht jede Umgebung eigene Settings?**
Ja. Sandbox und Live sollten getrennte Payrexx Settings haben.

**Kann ich mehrere Gateways betreiben?**
Ja. Jeder Payrexx Settings Datensatz erzeugt den dazugehörigen Gateway.
