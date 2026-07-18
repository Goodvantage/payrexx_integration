# Playwright tests — `payrexx_integration`

End-to-end tests that drive the desk UI and HTTP endpoints of the
`payrexx_integration` app on a running Frappe site.

## Prerequisites

- A running Frappe site with `payrexx_integration` (and its required app
  `payments`) installed and migrated.
- Node 18+.
- Admin credentials for the target site.

## Setup

```bash
cd apps/payrexx_integration/playwright
npm install
npx playwright install chromium
```

## Run

```bash
# Defaults: site at http://localhost:8000, user Administrator/admin.
npm test

# Override
PLAYWRIGHT_BASE_URL=https://my-site.local \
FRAPPE_USERNAME=Administrator \
FRAPPE_PASSWORD=secret \
  npm test

# Headed (browser visible)
npm run test:headed

# Inspector / time-travel UI
npm run test:ui

# View the HTML report from the last run
npm run report
```

## What's covered

| Spec | What it does | Needs |
|---|---|---|
| `payrexx_settings.spec.ts` | Opens an existing `Payrexx Settings` row in Desk, verifies its form values and single webhook message, then verifies the matching `Payment Gateway` row through the REST API. | A settings row named `Sandbox`, or set `TEST_PAYREXX_SETTINGS` to another existing row. |
| `pay_invoice_redirect.spec.ts` | Hits `/api/method/payrexx_integration.api.pay_invoice` with missing or invalid tokens, including unknown invoice names that do not pass token validation. Verifies the 403 paths. | Nothing extra. |
| `booking_email.spec.ts` | Calls `Good Event Booking.create_sales_invoice`, then asserts the Email Queue contains a gateway-bound Payrexx `pay_invoice?si=…&gateway_name=…&token=…` URL when the active invoice-email provider renders one. The spec skips that assertion when the provider omits the URL. | `TEST_BOOKING_NAME` pointing at an existing eligible Good Event Booking with customer/contact email, plus Good Event installed. |

## What's *not* covered (yet)

- A real round-trip to `api.payrexx.com` (sandbox or live). This needs a
  separately guarded opt-in spec with valid provider credentials, a correctly
  signed invoice/gateway token, a submitted outstanding invoice, payment
  account and automation-user setup, and explicit cleanup. Do not turn the
  current negative endpoint spec into a provider test by changing only its
  expected status or redirect URL.
- Webhook → Payment Entry creation in a real browser/provider round-trip. The
  Python integration suite covers callback settlement, idempotency, and exactly
  one submitted Payment Entry; a browser E2E requires a configured Payrexx
  sandbox and complete ERPNext payment-account setup.

## Auth state

Login is performed once in `tests/helpers/global-setup.ts` and the session
cookie is cached in `auth.json` (gitignored). Delete `auth.json` to force a
fresh login on the next run.
