# Payrexx Integration - Documentation

## Purpose

`payrexx_integration` adds Payrexx as a standalone payment gateway app without patching upstream `payments`. It provides a Payrexx Settings DocType, a REST client, webhook handling, and signed pay-by-email URLs for ERPNext Sales Invoices.

## Dependencies

```text
payments
  └── payrexx_integration
```

Do not modify `apps/payments` directly for Payrexx behavior.

## Core DocTypes

| DocType | Purpose |
|---|---|
| `Payrexx Settings` | Per-environment Payrexx credentials and gateway settings. |

Saving Payrexx Settings creates/updates the matching `Payment Gateway` row through the standard payments utility.

## Important Modules

| Module | Purpose |
|---|---|
| `api.py` | Signed pay-by-email URL generation and `pay_invoice` redirect endpoint. |
| `payrexx/payrexx_client.py` | Thin Payrexx REST client. |
| `payrexx/webhook_validator.py` | HMAC webhook signature validation. |
| `doctype/payrexx_settings/payrexx_settings.py` | Settings controller, gateway creation, callback endpoint. |
| `dev_e2e.py` | Local smoke helper for event-to-invoice-email flows. |
| `playwright/` | Browser tests for Payrexx and cross-app invoice email flows. |

## URL Contracts

Pay-by-email endpoint:

```text
/api/method/payrexx_integration.api.pay_invoice?si=<Sales Invoice>&token=<hmac>
```

Pay-by-email links are generated only for submitted Sales Invoices. Draft
invoices return no payment URL, and `pay_invoice` rejects draft invoices before
creating a Payrexx gateway.

Webhook endpoint:

```text
/api/method/payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.callback?gateway_name=<Payrexx Settings name>
```

## Security Model

- Pay-by-email URLs are signed with an HMAC derived from the site's `encryption_key`.
- Payrexx webhooks are validated with `X-Webhook-Signature`.
- Webhook signing key and API secret are separate values.
- Guest endpoints are intentionally whitelisted and documented in `SEMGREP_OVERRIDES.md`.

## Cross-App Integration

`event_app` imports `payrexx_pay_url` for invoice and combined-bundle emails. Missing Payrexx configuration should degrade gracefully: invoice emails still send without the online-pay button.

## Testing

```bash
cd frappe-bench
bench --site development16.localhost run-tests --app payrexx_integration \
  --module payrexx_integration.payrexx_integration.doctype.payrexx_settings.test_payrexx_settings
```

```bash
cd frappe-bench/apps/payrexx_integration/playwright
npx playwright test
```

## Related Docs

- `README.md` - installation notes.
- `HOW_TO.md` - operator runbook.
- `AGENTS.md` - detailed implementation notes.
- `PAYREXX_INTEGRATION.md` - integration design/reference.
