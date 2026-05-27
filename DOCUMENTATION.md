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
Normal Payrexx accounts use `api_base_domain = "payrexx.com"`, producing API
calls to `https://api.payrexx.com/...`. Payrexx Platform / partner accounts
store only the first subdomain in `instance_name` and the remaining platform
domain in `api_base_domain`; for example, `customer.pay.goodvantage.ch`
uses `instance_name = "customer"` and
`api_base_domain = "pay.goodvantage.ch"`, producing API calls to
`https://api.pay.goodvantage.ch/...`. If a custom API domain rejects the
instance credentials with 401/403/404, the client retries the same request
against the default `api.payrexx.com` host. This keeps instance API keys working
when a checkout/login custom domain exists but the REST API still authenticates
on Payrexx's default API domain.

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
GET /api/method/payrexx_integration.api.pay_invoice?si=<Sales Invoice>&token=<hmac>
```

Pay-by-email links are generated only for submitted Sales Invoices. Draft
invoices return no payment URL, and `pay_invoice` rejects draft invoices before
creating a Payrexx gateway.

Externally shared URLs are built from `host_name` exactly as configured. This
avoids leaking a local bench `webserver_port` such as `:8000`, or a temporary
tunnel origin, into Payrexx checkout links and webhook instructions when the
site is exposed through a reverse proxy or ngrok URL.

Webhook endpoint:

```text
POST /api/method/payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.callback?gateway_name=<Payrexx Settings name>
```

The Desk form asks the server for this callback URL as soon as `gateway_name`
is filled, including on unsaved rows. The helper uses the configured public
`host_name` when available and falls back to the current browser origin only if
the server call fails. Refreshing or saving the form replaces the existing
webhook URL hint instead of appending duplicate headline rows. The webhook
signing key can therefore stay blank until the webhook has been created in
Payrexx.
Payrexx sends JSON webhooks, so the callback reads `gateway_name` directly from
the request query string before resolving the signing key.
After signature verification, payment side effects run as the configured
`Non Profit Settings.creation_user` when that DocType is installed, otherwise
as `Administrator`. Transient `QueryDeadlockError` failures while running the
reference document's `on_payment_authorized` hook are retried before the webhook
is allowed to fail. Non-deadlock failures are logged and re-raised so Payrexx
can retry the webhook; the Integration Request and downstream payment side
effects are committed together by Frappe's request transaction instead of a
mid-callback manual commit.
If a webhook is missing `referenceId`, references an unknown Integration
Request, or references an Integration Request whose service is not `Payrexx`,
the callback logs only a compact transaction summary
(`reference_id`, status, transaction id/uuid, mode, instance, and payment
request id) instead of the full Payrexx payload, because the full payload can
contain payer contact data.

Success redirect endpoint:

```text
GET /api/method/payrexx_integration.api.payment_success?ir=<Integration Request>&gateway_name=<Payrexx Settings name>
```

Payrexx success redirects reconcile the Integration Request by fetching the
Gateway from Payrexx server-side. Webhooks remain the primary completion path,
but the success return is a safe fallback because payment side effects only run
after Payrexx reports the Gateway or one of its transactions as `confirmed`.
If the Integration Request data contains a `redirect_to` value, the endpoint
redirects directly to that same-site return URL after reconciliation; otherwise
it falls back to the standard `/payment-success` page. If Payrexx does not yet
report a confirmed payment, the endpoint redirects to `/payment-failed` instead
of showing a success page prematurely. Payment creators can pass per-checkout
`failed_redirect_to` and `cancel_redirect_to` values to `get_payment_url()` when
they need failed or cancelled Payrexx returns to land back in their own UI
instead of the generic failed-payment page.

## Security Model

- Pay-by-email URLs are signed with an HMAC derived from the site's `encryption_key`.
- Payrexx webhooks are validated with `X-Webhook-Signature`.
- Webhook signing key and API secret are separate values.
- Webhook diagnostics avoid logging full payer/payment payloads.
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

Legacy Buzz browser specs for `/anmelden` and `buzz.api.process_booking` are
skipped by default because the current native Event App no longer serves those
routes. Run them only on a Buzz-compatible site with `RUN_LEGACY_BUZZ_E2E=1`.

## Related Docs

- `README.md` - installation notes.
- `HOW_TO.md` - operator runbook.
- `AGENTS.md` - detailed implementation notes.
- `PAYREXX_INTEGRATION.md` - integration design/reference.
