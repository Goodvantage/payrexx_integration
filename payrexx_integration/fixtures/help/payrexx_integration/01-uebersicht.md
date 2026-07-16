---
title: Payrexx Integration
slug: payrexx-integration-uebersicht
category: Payrexx Integration
level: Beginner
---

# Payrexx Integration

Payrexx Integration verbindet ERPNext-Rechnungen mit dem Payrexx Hosted Checkout. Die App stellt pro Umgebung einen **Payrexx Settings** Datensatz bereit und erzeugt daraus den passenden Payment Gateway. Für die Buchhaltung muss danach zusätzlich ein **Payment Gateway Account** pro Firma/Währung eingerichtet werden.

![Payrexx Settings Liste im Desk](/assets/payrexx_integration/images/help/01-payrexx-settings-list.png)

## Wofür wird es genutzt?

- Zahlungslinks in E-Mails
- Online-Zahlungen für Rechnungen
- Payrexx Webhook-Rückmeldungen
- automatische Zuordnung zur ERPNext Zahlung

## Grundprinzip

1. Payrexx Settings, Webhook und Payment Gateway Account werden eingerichtet.
2. Eine Rechnung wird erstellt.
3. Das E-Mail enthält einen sicheren Zahlungslink.
4. Beim ersten Klick erstellt ERPNext den Payment Request und öffnet den Payrexx Checkout.
5. Nach erfolgreicher Zahlung meldet Payrexx den Status zurück.
6. ERPNext erstellt genau einen Payment Entry und aktualisiert Rechnung und Payment Request.

## Häufige Fragen

**Braucht jede Umgebung eigene Settings?**
Ja. Sandbox und Live sollten getrennte Payrexx Settings haben.

**Kann ich mehrere Gateways betreiben?**
Ja. Jeder Payrexx Settings Datensatz erzeugt den dazugehörigen Gateway.
