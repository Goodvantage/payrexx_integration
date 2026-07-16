# Payrexx Integration - Documentation

## Purpose

`payrexx_integration` adds Payrexx as a standalone payment gateway app without patching upstream `payments`. It provides a Payrexx Settings DocType, a REST client, webhook handling, and signed pay-by-email URLs for ERPNext Sales Invoices.

## Dependencies

```text
payments
  └── payrexx_integration
```

Do not modify `apps/payments` directly for Payrexx behavior.

The app metadata exposes `/assets/payrexx_integration/images/payrexx-integration-app-logo.svg`, following the shared Goodvantage navy tile pattern with a centered white credit-card line symbol and small bottom-right white Goodvantage `g` mark. Payrexx Integration does not add a Desk app tile by default; the logo is available for metadata or future app-surface use.

## Core DocTypes

| DocType | Purpose |
|---|---|
| `Payrexx Settings` | Per-environment Payrexx credentials and gateway settings. |
| `Payment Gateway` | Upstream registry row `Payrexx-<gateway_name>`, created by the settings controller. |
| `Payment Gateway Account` | Upstream ERPNext company/currency/payment-account bridge. Operators must create it after the gateway; it is not seeded by this app. |
| `Integration Request` | Upstream provider-request audit and state record. |

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
| `gateway_selection.py` | Generic, strict Payrexx Settings resolver for this app and downstream consumers. |
| `payrexx/payrexx_client.py` | Thin Payrexx REST client. |
| `payrexx/webhook_validator.py` | HMAC webhook signature validation. |
| `doctype/payrexx_settings/payrexx_settings.py` | Settings controller, gateway creation, callback endpoint. |
| `playwright/` | Browser tests for Payrexx plus an opt-in invoice-email check against an existing Good Event Booking. |

## URL Contracts

Pay-by-email endpoint:

```text
GET /api/method/payrexx_integration.api.pay_invoice?si=<Sales Invoice>&gateway_name=<Payrexx Settings name>&token=<hmac>
```

Pay-by-email links are generated only for submitted Sales Invoices. Draft
invoices return no payment URL, and `pay_invoice` rejects draft invoices before
creating a Payrexx gateway. Submitting the ERPNext Payment Request creates the
Payrexx Gateway and stores its URL; `pay_invoice` redirects to that stored URL
instead of requesting a second checkout. If a legacy Payment Request has no URL,
the app recovers the URL recorded in its active Integration Request. An active
request with no recoverable URL raises a clean error rather than creating a
potential duplicate checkout.

Gateway selection is centralized in
`payrexx_integration.gateway_selection.resolve_payrexx_settings()`. Resolution
uses an explicit `gateway_name`, then an optional caller-owned `site_config_key`,
then the only configured Payrexx Settings row. Zero or multiple rows fail
clearly; names such as `Live` and `Sandbox` are never silently preferred.
Current pay-by-email links include the resolved gateway in both the URL and its
HMAC. Legacy links without `gateway_name` keep their original token contract and
work when exactly one settings row exists, but intentionally fail when several
rows make the old link ambiguous.

The generated `Payment Gateway` is not sufficient for ERPNext posting. Before a
first invoice-link click can create its Payment Request, an operator must create
a `Payment Gateway Account` for that gateway and invoice company, with the
appropriate currency and Bank/Cash payment account. Deployments using several
companies or currencies need an unambiguous row for each combination.

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
as `Administrator`. The `pay_invoice` redirect endpoint uses the same
least-privilege resolution (`session_utils.as_automation_user`) for its lazy
Payment Request creation — no guest path runs as a hardcoded Administrator
when an automation user is configured. A confirmed Integration Request that
references an ERPNext Payment Request calls the standard `set_as_paid()` method
under a row lock. This creates and submits one Payment Entry and lets ERPNext
update the Payment Request status/outstanding amount and the source invoice
outstanding amount. Other reference types continue to receive their existing
`on_payment_authorized("Completed")` hook.

Transient `QueryDeadlockError` failures retry the entire locked completion unit:
the Integration Request is reloaded, transaction data and status are saved, and
the downstream settlement runs again in one transaction. Duplicate confirmed
callbacks return after observing the locked completed row, while separate
requests for the same Payment Request are serialized on that Payment Request.
Non-deadlock failures are logged and re-raised so Payrexx can retry the webhook;
the Integration Request and downstream payment side effects are committed
together by Frappe's request transaction instead of a mid-callback manual
commit.
The callback also binds the verifying key to the Integration Request itself:
`get_payment_url()` stores the originating Payrexx Settings name in the
Integration Request data (`payrexx_settings`), and a webhook verified with a
different settings row's key (for example a Sandbox-signed webhook referencing
a Live request) is logged and ignored. Requests created before this field
existed fall back to the `payment_gateway` value recorded at creation.
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
The credentials used for that confirmation come from the Integration Request's
own stored gateway (`payrexx_settings`, falling back to `payment_gateway`); the
caller-supplied `gateway_name` parameter is only honoured for legacy requests
that carry neither value.
If the Integration Request data contains a `redirect_to` value, the endpoint
redirects directly to that same-site return URL after reconciliation; otherwise
it falls back to the standard `/payment-success` page. If Payrexx does not yet
report a confirmed payment, the endpoint redirects to `/payment-failed` instead
of showing a success page prematurely. Payment creators can pass per-checkout
`failed_redirect_to` and `cancel_redirect_to` values to `get_payment_url()` when
they need failed or cancelled Payrexx returns to land back in their own UI
instead of the generic failed-payment page.

## Chargebacks

A signed Payrexx `chargeback` event changes the Integration Request to `Failed`
and stores the provider transaction. The integration deliberately does not
cancel or delete submitted Payment Entries or ledger records. It creates one
idempotent, high-priority open ToDo assigned to the payment automation user and
linked to the Integration Request for an accountant to review and post the
appropriate reversal. Repeated chargeback callbacks reuse that exception, and a
later duplicate confirmation cannot move the chargeback request back to
`Completed`.

## Supported Payment Operations

The client creates hosted Gateways and retrieves Gateway/Transaction state.
Webhook and success-return reconciliation settle only `confirmed` payments.
`authorized` and `reserved` callbacks record the Integration Request as
`Authorized`, but this app has no later-charge or capture operation.
`cancelled`, `declined`, `error`, and `expired` callbacks mark the request
failed; they do not call Payrexx to cancel or void anything. `chargeback` has
the accounting-exception workflow above. Refund initiation and ERPNext refund
reconciliation are not implemented: a `refunded` or otherwise unknown webhook
is stored while the Integration Request status remains unchanged. Provider-side
refund/capture/cancellation and the corresponding accounting reversal therefore
remain explicit manual procedures.

## Security Model

- Pay-by-email URLs are signed with an HMAC derived from the site's `encryption_key`.
- Payrexx webhooks are validated with `X-Webhook-Signature`.
- Webhook signing key and API secret are separate values.
- Webhook diagnostics avoid logging full payer/payment payloads.
- Guest endpoints are intentionally whitelisted and documented in `SEMGREP_OVERRIDES.md`.

## Cross-App Integration

Good Event's default invoice renderer imports `payrexx_pay_url`. Missing or
ambiguous Payrexx configuration degrades gracefully: invoice emails still send
without the online-pay button.

Downstream apps can import `resolve_payrexx_settings` without creating a reverse
dependency. A caller that owns a site setting can pass its key, for example
`resolve_payrexx_settings(site_config_key="my_app_payrexx_gateway")`. This app
does not import the downstream app or interpret its site-config keys.

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

The Playwright project covers current Payrexx Settings and pay-by-email endpoint
behavior. Its optional booking-email spec accepts an existing eligible Good
Event Booking through `TEST_BOOKING_NAME`; this app does not create cross-app
event fixtures.

## Related Docs

- `README.md` - installation notes.
- `HOW_TO.md` - operator runbook.
- `AGENTS.md` - detailed implementation notes.
- `PAYREXX_INTEGRATION.md` - integration design/reference.
