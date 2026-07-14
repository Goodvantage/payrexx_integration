# Payrexx Integration - How To

Payrexx Integration adds Payrexx hosted checkout support as a standalone app on top of upstream `payments`.

## 1. Create Payrexx Settings

1. Open **Payrexx Settings**.
2. Set `gateway_name`, environment, instance name, API base domain, API secret, and webhook signing key.
3. Save.

The form shows one callback URL as soon as `gateway_name` is filled, even before
the row can be saved. The URL uses the site's configured public `host_name` when
available, so admins do not accidentally copy a temporary tunnel URL. Refreshing
or saving the form replaces that hint in place instead of appending duplicate
lines. Use that URL to create the Payrexx webhook, then paste the generated
signing key back into **Webhook Signing Key**.

On save, the controller verifies credentials unless running in tests/install and creates the corresponding `Payment Gateway` row.

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

## 2. Configure The Webhook In Payrexx

Copy the callback URL from the settings form or build it with this shape:

```text
POST https://<site>/api/method/payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.callback?gateway_name=<Payrexx Settings name>
```

Use the Payrexx dashboard's webhook signing key as the app's webhook signing key. This is separate from the API secret.

## 3. Use Pay-By-Email Links

Invoice emails can call the Jinja helper:

```jinja
{{ payrexx_pay_url(doc.name) }}
```

The helper returns a signed GET URL:

```text
GET /api/method/payrexx_integration.api.pay_invoice?si=<Sales Invoice>&token=<hmac>
```

When clicked, the endpoint verifies the token, lazy-creates and submits a Payment
Request through ERPNext, and redirects to the checkout URL created during that
submission. Repeated clicks reuse the same Payment Request and Payrexx checkout.
If an older active Integration Request has no recoverable checkout URL, the app
shows an error instead of creating a second potentially chargeable checkout;
review that Integration Request in Desk.

## 4. Success Redirect Fallback

Payrexx webhooks should still be configured, but every generated Gateway also
uses a success redirect back into:

```text
GET https://<site>/api/method/payrexx_integration.api.payment_success?ir=<Integration Request>&gateway_name=<Payrexx Settings name>
```

That endpoint asks Payrexx for the Gateway status server-side and only marks the
Integration Request complete when Payrexx reports a confirmed payment.
When a payment creator stored `redirect_to` in the Integration Request, the
endpoint sends the customer directly back to that same-site URL after
reconciliation instead of showing the generic `/payment-success` page. If the
Gateway is not confirmed, the customer is sent to `/payment-failed`. Apps that
need a branded failed-payment state can pass `failed_redirect_to` and
`cancel_redirect_to` to `get_payment_url()` for that individual checkout.

## 5. Set The Production Host URL

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

## 6. Troubleshoot A Failed Save

If saving Payrexx Settings fails:

1. Confirm the instance name matches the first subdomain of the checkout/login domain.
2. Confirm the API base domain is correct (`payrexx.com` for normal accounts, e.g. `pay.goodvantage.ch` for GoodVantage partner accounts). A 401/403/404 from a custom API domain is retried once on `api.payrexx.com`.
3. Confirm the API secret is current.
4. Confirm outbound network access from the bench.
5. Try saving in Sandbox first.

The app pings `GET /Gateway/0/`; a Payrexx JSON response with `status: error` can still mean credentials are accepted if the error is "gateway not found".

## 7. Troubleshoot A Payment Link

If a pay link returns 403:

1. Check that the Sales Invoice exists.
2. Check that the token was generated for the same invoice name.
3. Confirm the site's `encryption_key` was not rotated after the email was sent.
4. Confirm URL parameters were not stripped by an email client.

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

## 8. Handle A Chargeback

When Payrexx reports a chargeback:

1. Open the high-priority ToDo linked to the failed Integration Request.
2. Review its stored Payrexx transaction and the submitted Payment Entry.
3. Post the appropriate accounting reversal under your normal approval process.
4. Close the ToDo after the reversal and customer/source-document follow-up are complete.

The callback preserves submitted ledger records and never cancels them
automatically. Repeated chargeback callbacks do not create additional ToDos.

## 9. Run Tests

```bash
cd frappe-bench
bench --site development16.localhost run-tests --app payrexx_integration \
  --module payrexx_integration.payrexx_integration.doctype.payrexx_settings.test_payrexx_settings
```

Browser tests live in the Playwright project:

```bash
cd frappe-bench/apps/payrexx_integration/playwright
npm install
npx playwright test
```

The Playwright specs cover the current Payrexx Settings, pay-by-email, and Good
Event correspondence flows.
