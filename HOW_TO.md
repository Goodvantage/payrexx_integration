# Payrexx Integration - How To

Payrexx Integration adds Payrexx hosted checkout support as a standalone app on top of upstream `payments`.

## 1. Create Payrexx Settings

1. Open **Payrexx Settings**.
2. Set **Gateway Name**, **Instance Name**, **API Base Domain**, **API Version**, and **API Secret**. There is no separate Environment field; use distinct Gateway Names such as `Sandbox` and `Live`.
3. Review **Supported Currencies**, the optional **PSP Whitelist**, **Gateway Validity**, and redirect overrides.
4. As soon as **Gateway Name** is filled, copy the callback URL shown on the unsaved form and create that webhook in Payrexx.
5. Paste Payrexx's per-webhook key into the required **Webhook Signing Key** field, then save.

The form shows one callback URL as soon as `gateway_name` is filled, even before
the row can be saved. The URL uses the site's configured public `host_name` when
available, so admins do not accidentally copy a temporary tunnel URL. Refreshing
or saving the form replaces that hint in place instead of appending duplicate
lines. Use that URL to create the Payrexx webhook, then paste the generated
signing key back into **Webhook Signing Key**.

On save, the controller verifies credentials unless running in tests/install and creates the corresponding `Payment Gateway` row named `Payrexx-<Gateway Name>`. Saving does not create the ERPNext Payment Gateway Account required for invoice payments.

For normal Payrexx accounts, keep `api_base_domain` as `payrexx.com`. For
Payrexx Platform / partner accounts, split the login domain into instance and
base domain. Example: `customer.pay.goodvantage.ch` becomes:

```text
Instance Name: customer
API Base Domain: pay.goodvantage.ch
```

If that custom API domain rejects an otherwise valid instance key, the client
automatically retries on `api.payrexx.com`. This is useful when the checkout
uses a partner/custom domain but Payrexx still authenticates API calls on the
default API host.

## 2. Create The Payment Gateway Account

Create the accounting bridge after saving Payrexx Settings:

1. Open **Payment Gateway Account** and create a new row.
2. Select the generated **Payment Gateway**, for example `Payrexx-Live`.
3. Select the ERPNext **Payment Account** where Payrexx receipts are posted. Its
   account currency must match the invoices; ERPNext derives the gateway
   account's **Currency** from this account.
4. Set the matching **Company**.
5. Enable **Is Default** when this should be the default gateway account for that company/currency, then save.

Create a separate Payment Gateway Account for every company/currency combination that will use Payrexx. A signed invoice link can be generated without this row, but the first click cannot create its Payment Request and returns `No Payment Gateway Account configured for Payrexx-<name>`.

## 3. Configure The Webhook In Payrexx

Copy the callback URL from the settings form or build it with this shape:

```text
POST https://<site>/api/method/payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.callback?gateway_name=<Payrexx Settings name>
```

Use the Payrexx dashboard's webhook signing key as the app's webhook signing key. This is separate from the API secret.

## 4. Use Pay-By-Email Links

Invoice emails can call the Jinja helper:

```jinja
{{ payrexx_pay_url(doc.name) }}
```

This form requires exactly one Payrexx Settings row. If the site has multiple
rows, choose the gateway explicitly so it is included in the signed link:

```jinja
{{ payrexx_pay_url(doc.name, "Live") }}
```

The helper returns a signed GET URL:

```text
GET /api/method/payrexx_integration.api.pay_invoice?si=<Sales Invoice>&gateway_name=<Payrexx Settings name>&token=<hmac>
```

When clicked, the endpoint verifies the token, lazy-creates and submits a Payment
Request through ERPNext, and redirects to the checkout URL created during that
submission. Repeated clicks reuse the same Payment Request and Payrexx checkout.
The GET endpoint commits its lazy-created local records only after Payrexx has
returned a valid checkout URL; a failed click rolls back the current attempt.
Links generated before gateway binding was introduced did not include
`gateway_name`. They continue to work when exactly one settings row exists, but
are rejected as ambiguous when multiple rows exist; resend the invoice email to
issue a gateway-bound link.

If an older active Integration Request has no recoverable checkout URL, the app
shows an error instead of creating a second potentially chargeable checkout;
review that Integration Request in Desk.

## 5. Success Redirect Fallback

Payrexx webhooks should still be configured. Unless an explicit success redirect
override is set, every generated Gateway returns through:

```text
GET https://<site>/api/method/payrexx_integration.api.payment_success?ir=<Integration Request>&gateway_name=<Payrexx Settings name>
```

That endpoint asks Payrexx for the Gateway status server-side and only marks the
Integration Request complete when Payrexx reports a confirmed payment.
Because this return is an HTTP GET, terminal server-verified reconciliation
requests Frappe's end-of-request commit before redirecting; waiting results do
not. A success page with unchanged accounting records indicates an outdated
deployment that still rolled back the GET transaction.
When a payment creator stored `redirect_to` in the Integration Request, the
endpoint sends the customer directly back to that same-site URL after
reconciliation instead of showing the generic `/payment-success` page. If the
Gateway is not confirmed, the customer is sent to `/payment-failed`. Apps that
need a branded failed-payment state can pass `failed_redirect_to` and
`cancel_redirect_to` to `get_payment_url()` for that individual checkout.

## 6. Set The Production Host URL

The app uses the configured public `host_name` for externally shared URLs,
including pay links, redirects, and the webhook URL shown in Desk. Set it in
production so Payrexx never receives a local bench port or temporary tunnel URL:

```bash
cd frappe-bench
bench --site <site> set-config host_name "https://kursverwaltung.example.ch"
bench --site <site> clear-cache
```

If Payrexx logs show an ngrok HTML response such as `ERR_NGROK_3200`, the
webhook was configured with an offline tunnel URL. Update the webhook in the
Payrexx dashboard to the current public `host_name` URL from **Payrexx
Settings**.

## 7. Troubleshoot A Failed Save

If saving Payrexx Settings fails:

1. Confirm the instance name matches the first subdomain of the checkout/login domain.
2. Confirm the API base domain is correct (`payrexx.com` for normal accounts, e.g. `pay.goodvantage.ch` for GoodVantage partner accounts). A 401/403/404 from a custom API domain is retried once on `api.payrexx.com`.
3. Confirm the API secret is current.
4. Confirm outbound network access from the bench.
5. Try saving in Sandbox first.

The app pings `GET /Gateway/0/`; a Payrexx JSON response with `status: error` can still mean credentials are accepted if the error is "gateway not found".

## 8. Troubleshoot A Payment Link

If a pay link returns 403:

1. Check that the Sales Invoice exists.
2. Check that the token was generated for the same invoice name.
3. Confirm the site's `encryption_key` was not rotated after the email was sent.
4. Confirm URL parameters were not stripped by an email client.

If the first valid click reports that no Payment Gateway Account is configured, create or correct the ERPNext row described in Section 2 for the generated gateway, invoice company, and invoice currency.

If the link reports that a draft Payment Request already exists, open the
invoice's draft Payment Request in Desk and either complete, cancel or delete it
after review. The pay-link endpoint preserves all pre-existing drafts because
ERPNext would otherwise reuse one even when it belongs to another gateway.

If the link opens but payment does not update, check **Integration Request**
rows, Payrexx webhook delivery logs, and whether Payrexx can reach the success
redirect URL on the public `host_name`.
The webhook only updates Integration Requests whose service is `Payrexx`; if a
Payrexx reference ID points at a row owned by another gateway, the callback logs
the mismatch and ignores it.
If Payrexx reports a transient `tabSeries` / `QueryDeadlockError`, retry the
webhook after the latest app code is loaded. The callback retries those
deadlocks from the locked Integration Request update through downstream
settlement before returning an error to Payrexx.
Other downstream payment-hook failures are logged and returned as webhook
errors so Payrexx can retry; the app no longer marks the Integration Request
complete in a separate manual commit before the referenced document accepts the
payment.

For a confirmed Payment Request, verify that its status is **Paid**, its
outstanding amount is zero, and exactly one submitted Payment Entry references
it. ERPNext updates the linked Sales Invoice outstanding amount through the
normal Payment Entry submission path.

## 9. Reconcile Payments And Handle Chargebacks

For a confirmed invoice payment, verify this chain rather than relying only on the browser success page:

1. The Payrexx `Integration Request` is **Completed** and stores the confirmed transaction.
2. The linked ERPNext `Payment Request` is **Paid** with zero outstanding.
3. Exactly one submitted `Payment Entry` references that Payment Request.
4. The Sales Invoice outstanding amount reflects the submitted Payment Entry.

When Payrexx reports a chargeback:

1. Open the high-priority ToDo linked to the failed Integration Request.
2. Review its stored Payrexx transaction and the submitted Payment Entry.
3. Post the appropriate accounting reversal under your normal approval process.
4. Close the ToDo after the reversal and customer/source-document follow-up are complete.

The callback preserves submitted ledger records and never cancels them
automatically. Repeated chargeback callbacks do not create additional ToDos.

### Unsupported Provider Operations

This app does not initiate captures of `reserved` transactions, later charges of `authorized` transactions, checkout cancellation/voids, or refunds. It also does not reconcile a `refunded` webhook into ERPNext accounting; an unknown/refunded status is stored on the Integration Request without completing or reversing it. Perform the provider action in Payrexx and post the approved ERPNext reversal manually. Do not infer a refund from a failed/chargeback Integration Request or cancel submitted Payment Entries automatically.

## 10. Run Hosted Sandbox Settlement Acceptance

Use only a dedicated developer QA site and a small, fully unpaid sandbox
invoice. The runner never enters card data or invokes settlement itself.

Enable and bind the read-only evidence surface to one exact target:

```bash
bench --site <qa-site> set-config payrexx_hosted_qa_enabled 1
bench --site <qa-site> set-config payrexx_hosted_qa_gateway <Payrexx-Settings-name>
bench --site <qa-site> set-config payrexx_hosted_qa_invoice <Sales-Invoice-name>
bench --site <qa-site> clear-cache
```

Load credentials from the protected secret source, then export these non-secret
target controls. Never put credentials, signed payment URLs, or card data in
arguments, state files, screenshots, traces, or reports:

```bash
export PAYREXX_HOSTED_QA_BASE_URL=https://<qa-host>
export PAYREXX_HOSTED_QA_ALLOWED_HOSTS=<qa-host>
export PAYREXX_HOSTED_QA_USER=<protected-system-and-accounts-manager>
export PAYREXX_HOSTED_QA_PASSWORD=<protected-password>
export PAYREXX_HOSTED_QA_RUN_ID=PRX-SBX-E2E-YYYYMMDD-<8-hex>

cd frappe-bench
env/bin/python -m payrexx_integration.tests.hosted_settlement_qa --mode preflight
```

The first preflight returns `ready_for_checkout` when no checkout exists. Open
the invoice's normal signed payment link and stop after the Payrexx sandbox page
loads. Run preflight again; it must return `awaiting_payment` with exactly one
Payment Request and Integration Request. Complete the payment manually only
after the provider page visibly identifies the transaction as TEST. Abort on an
unexpected provider, CAPTCHA, 3-D Secure, live-mode label, or amount.

After Payrexx returns and its callback or normal success reconciliation has run,
use the persisted exact record names for read-only proof:

```bash
env/bin/python -m payrexx_integration.tests.hosted_settlement_qa --mode settlement
```

Acceptance requires the confirmed TEST transaction, Completed Integration
Request carrying the exact settlement-created Payment Entry name, Paid Payment
Request and Sales Invoice, zero outstanding balances, and one exact submitted
Payment Entry. Do not call reconciliation merely to make the test pass; a
pending result is evidence that the normal external path did not complete.

Disable the gate immediately afterward:

```bash
bench --site <qa-site> set-config payrexx_hosted_qa_enabled 0
bench --site <qa-site> clear-cache
```

Payrexx test transactions cannot be deleted. Keep their run marker and exact
ERPNext records as acceptance evidence; never add destructive provider cleanup.

## 11. Run Tests

```bash
cd frappe-bench
bench --site development16.localhost run-tests --app payrexx_integration \
  --module payrexx_integration.payrexx_integration.doctype.payrexx_settings.test_payrexx_settings

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_hosted_qa
```

Browser tests live in the Playwright project:

```bash
cd frappe-bench/apps/payrexx_integration/playwright
npm install
npx playwright test
```

The core Playwright specs cover Payrexx Settings and pay-by-email endpoint errors. The optional `booking_email.spec.ts` uses `TEST_BOOKING_NAME` for an existing eligible Good Event Booking; Payrexx Integration no longer seeds cross-app event records.
