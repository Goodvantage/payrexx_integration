# AGENTS.md — payrexx_integration

Guidance for coding agents working in `payrexx_integration`. Pairs with the
bench-root `AGENTS.md` (general Frappe rules) and the upstream
`apps/payments` (which this app extends without forking).

---

## What payrexx_integration is

A standalone Frappe app that adds **Payrexx** as a payment gateway on top of
the upstream `frappe/payments` app. Same plug-in pattern as the existing
gateways inside `payments` (Stripe, Paymob, PayPal, Razorpay, …) but kept
in its own repo so we don't fork upstream `payments`.

`required_apps = ["payments"]`. Module name `Payrexx Integration`.
Innermost package dir: `payrexx_integration/payrexx_integration/payrexx_integration/`
(the triple `payrexx_integration` is the standard Frappe scrub layout).

> **Why a separate app, not a patch into `apps/payments`?** Upstream
> `payments` is on `frappe/payments`. Patching it directly turns every
> upstream pull into a merge conflict. The `payrexx_integration` app
> contributes the same shape (a `<X> Settings` doctype + an `on_update`
> that calls `payments.utils.create_payment_gateway`) without touching
> upstream code.

## Documentation Requirements

- Keep root-level `HOW_TO.md` and `DOCUMENTATION.md` present and current.
- Update `HOW_TO.md` when operator/admin procedures change.
- Update `DOCUMENTATION.md` when doctypes, hooks, APIs, webhook contracts,
  security assumptions, setup/migration behavior, or test commands change.
- Keep `README.md` short; use these docs for runbooks and technical reference.

---

## Architecture

| File | Purpose |
|---|---|
| `payrexx_integration/payrexx_integration/doctype/payrexx_settings/` | The settings DocType. One row per environment (`Sandbox` / `Live`). `on_update` auto-creates the matching `Payment Gateway` row (`Payrexx-<gateway_name>`). |
| `payrexx_integration/payrexx_integration/payrexx/payrexx_client.py` | Thin REST client. **Auth: `x-api-key: <api_secret>` header** — current Payrexx scheme (per the official PHP SDK). The legacy `ApiSignature` body field is no longer used. Supports Payrexx Platform domains through `Payrexx Settings.api_base_domain`. |
| `payrexx_integration/payrexx_integration/payrexx/webhook_validator.py` | HMAC-SHA256 verification of `X-Webhook-Signature`. Tries base64 first, falls back to hex. The signing key is **separate** from the API secret (configured per webhook in the Payrexx dashboard). |
| `api.py::payrexx_pay_url(sales_invoice)` | Jinja helper (registered via `hooks.py.jinja`). Returns an HMAC-signed redirect URL keyed off the site's `encryption_key`. |
| `api.py::pay_invoice(si, token)` | Whitelisted redirect endpoint. Verifies the HMAC token, looks up the Sales Invoice, lazy-creates a Payment Request via ERPNext's `make_payment_request`, and 302s to the Payrexx hosted checkout. **Both args are optional kwargs** so missing-param requests return clean 403, not 500. |
| `dev_e2e.py::run_event_to_invoice_email(email)` | Bench-execute helper that creates an event → ticket → booking → invoice → triggers the email queue. Used from the conversation runbook for one-shot smoke tests against the live sandbox. |
| `playwright/` | Self-contained Playwright project (npm). 12-spec suite covering Payrexx Settings desk flow, pay_invoice endpoint auth, and the Event Booking → email queue flow. Reads `TEST_BOOKING_NAME` env var. |

---

## URL patterns (operationally important)

### Pay-by-email URL (embedded in invoice emails)

```
{{ host_name }}/api/method/payrexx_integration.api.pay_invoice
  ?si=<Sales Invoice name>
  &token=<32 hex chars — first half of HMAC-SHA256(encryption_key, si_name)>
```

The token is deterministic per SI so resends produce the same URL — clicks
from old and new emails both work. To invalidate, rotate the site's
`encryption_key` (rare).

### Webhook URL (configured in Payrexx dashboard)

```
{{ host_name }}/api/method/payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.callback
  ?gateway_name=<Payrexx Settings name>
```

The `gateway_name` query param is required when more than one Payrexx
Settings row exists. The desk form's dashboard surfaces this URL so admins
can paste it into the Payrexx webhook settings.

### Success redirect URL (generated per Gateway)

```
{{ host_name }}/api/method/payrexx_integration.api.payment_success
  ?ir=<Integration Request name>
  &gateway_name=<Payrexx Settings name>
```

This endpoint is a fallback reconciliation path. It retrieves the Payrexx
Gateway server-side and only completes the Integration Request when Payrexx
reports a confirmed Gateway/transaction, then redirects directly to the
Integration Request's same-site `redirect_to` when present, otherwise to the
standard `/payment-success` page.

### Host URL — IMPORTANT for production

Payrexx-facing URLs use `payrexx_integration.url_utils.get_public_url()`,
which takes the site's `host_name` exactly as configured and avoids appending
the local bench `webserver_port` behind reverse proxies or ngrok. **No URL is
hard-coded.** In production, set `host_name` on the site (`bench --site <site>
set-config host_name "https://kursverwaltung.example.ch"`) and the embedded
URLs resolve correctly.

---

## Payrexx API quick reference

| | |
|---|---|
| Base URL | `https://api.<api_base_domain>/v1.14/`; default `https://api.payrexx.com/v1.14/` |
| Auth | `x-api-key: <api_secret>` header |
| Required query param | `?instance=<instance_name>` on every call |
| POST body format | `application/x-www-form-urlencoded` |
| Response envelope | `{"status":"success", "data":[ … ]}` |
| Amount unit | Smallest currency unit (CHF 2.00 → `200`) |
| Webhook signature header | `X-Webhook-Signature` — HMAC-SHA256 of raw body, signed with the per-webhook signing key (NOT the API secret) |

A "no gateway found" GET against `/Gateway/0/` is the cheap auth-check used
by `_ping()` — HTTP 200 with `status: error` means creds are valid.

For Payrexx Platform / partner accounts, split the checkout/login domain:
`kibesuisse.pay.goodvantage.ch` means `instance_name = "kibesuisse"` and
`api_base_domain = "pay.goodvantage.ch"`. Do not put the full login domain in
`instance_name`.

### Status mapping (webhook)

| Payrexx `transaction.status` | `Integration Request.status` | Side effect |
|---|---|---|
| `confirmed` | `Completed` | Runs `on_payment_authorized` on the reference doc |
| `authorized` | `Authorized` | Tokenisation — charge later via `/Transaction/{id}/` |
| `reserved` | `Authorized` | Pre-auth hold — capture later |
| `waiting` | unchanged | In-progress; wait for next webhook |
| `cancelled` / `declined` / `error` / `expired` / `chargeback` | `Failed` | Records error string |

---

## Testing

```bash
# Python integration tests
bench --site <site> run-tests --app payrexx_integration \
  --module payrexx_integration.payrexx_integration.doctype.payrexx_settings.test_payrexx_settings

# Playwright e2e (covers both this app + event_app correspondence flows)
cd playwright
npm install && npx playwright install chromium
TEST_BOOKING_NAME=<booking> npx playwright test
```

`dev_e2e.run_event_to_invoice_email("benediktmathis@gmail.com")` is the
canonical end-to-end smoke test — creates an event, books it, generates
the SI, queues the invoice email. Returns a summary dict with the booking
name (use it for `TEST_BOOKING_NAME` afterwards).

---

## Gotchas

- **Settings save calls Payrexx live.** `validate()._ping()` hits
  `GET /Gateway/0/` to verify credentials. With bogus creds the save
  fails with "Payrexx rejected the API Secret". The ping is skipped when
  `frappe.flags.in_test` or `frappe.flags.in_install` is set.
- **The DocType uses `autoname: field:gateway_name`** — the doc name = the
  `gateway_name` field. Don't add an `autoname` patch that breaks this.
- **Pay URL contains `&amp;` after rendering.** Tests asserting on the
  decoded body need to handle the HTML-entity encoding (the Playwright
  helper `decodeMime` does this).
- **Settings row is single per environment** but the doctype is NOT a
  Single — multiple rows are supported (e.g. `Sandbox` + `Live`). Webhooks
  must include `?gateway_name=...` once you have more than one row.
- **No URL hard-coding** — externally shared URLs go through
  `payrexx_integration.url_utils.get_public_url()`, which respects
  `host_name` without leaking the local bench port. The Playwright config is
  the only place `localhost:8000` appears (test runner, not embedded).

---

## Cross-app integration

- `event_app` imports `from payrexx_integration.api import payrexx_pay_url`
  in `services/booking_confirmation.py` and `services/workflow.py`
  (`combined_bundle` flow). Both wrap the import in try/except so missing
  Payrexx config gracefully degrades — invoice email still ships, just
  without the online-pay button.

---

## Recent commit

- `f6d0499 feat: Payrexx payment gateway with email integration + tests` —
  initial complete implementation.
