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
# Defaults: site at http://development16.localhost:8000, user Administrator/admin.
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
| `payrexx_settings.spec.ts` | Creates a `Payrexx Settings` row in the desk and verifies the matching `Payment Gateway` row appears. Also exercises the `payrexx_pay_url` jinja helper through the REST API. | Nothing extra. |
| `pay_invoice_redirect.spec.ts` | Hits `/api/method/payrexx_integration.api.pay_invoice` with bad/missing tokens and unknown invoices. Verifies the 403/404 paths. | Nothing extra. |
| `booking_email.spec.ts` | Calls `Good Event Booking.create_sales_invoice`, then asserts the Email Queue contains a row whose body has the Payrexx `pay?si=…&token=…` URL. | `TEST_BOOKING_NAME` env var pointing at a seeded `Good Event Booking` with a customer + contact email. |

## What's *not* covered (yet)

- A real round-trip to `api.payrexx.com` (sandbox or live). Add a spec
  variant that talks to the sandbox once you have credentials — the existing
  endpoint test already validates the URL pattern, so the only delta is
  changing the redirect assertion to `expect(r.status()).toBe(302)` and
  `expect(r.headers().location).toMatch(/\.payrexx\.com\//)`.
- Webhook → Payment Entry creation. The Python integration test
  (`test_payrexx_settings.TestPayrexxSettings.test_callback_marks_integration_request_completed`)
  covers the controller-side logic; an E2E test would need to forge a
  webhook and assert a `Payment Entry` was created against the SI, which
  requires the full ERPNext payment flow to be set up on the site.

## Auth state

Login is performed once in `tests/helpers/global-setup.ts` and the session
cookie is cached in `auth.json` (gitignored). Delete `auth.json` to force a
fresh login on the next run.
