# Payrexx Integration - How To

Payrexx Integration adds Payrexx hosted checkout support as a standalone app on top of upstream `payments`.

## 1. Create Payrexx Settings

1. Open **Payrexx Settings**.
2. Set `gateway_name`, environment, instance name, API secret, and webhook signing key.
3. Save.

On save, the controller verifies credentials unless running in tests/install and creates the corresponding `Payment Gateway` row.

## 2. Configure The Webhook In Payrexx

Copy the callback URL from the settings form or build it with this shape:

```text
https://<site>/api/method/payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.callback?gateway_name=<Payrexx Settings name>
```

Use the Payrexx dashboard's webhook signing key as the app's webhook signing key. This is separate from the API secret.

## 3. Use Pay-By-Email Links

Invoice emails can call the Jinja helper:

```jinja
{{ payrexx_pay_url(doc.name) }}
```

The helper returns a signed URL:

```text
/api/method/payrexx_integration.api.pay_invoice?si=<Sales Invoice>&token=<hmac>
```

When clicked, the endpoint verifies the token, lazy-creates a Payment Request through ERPNext, and redirects to Payrexx hosted checkout.

## 4. Success Redirect Fallback

Payrexx webhooks should still be configured, but every generated Gateway also
uses a success redirect back into:

```text
https://<site>/api/method/payrexx_integration.api.payment_success?ir=<Integration Request>&gateway_name=<Payrexx Settings name>
```

That endpoint asks Payrexx for the Gateway status server-side and only marks the
Integration Request complete when Payrexx reports a confirmed payment.
When a payment creator stored `redirect_to` in the Integration Request, the
endpoint sends the customer directly back to that same-site URL after
reconciliation instead of showing the generic `/payment-success` page. If the
Gateway is not confirmed, the customer is sent to `/payment-failed`.

## 5. Set The Production Host URL

The app uses the configured public `host_name` for externally shared URLs. Set
it in production so emails and Payrexx redirects contain the public URL without
the local bench port:

```bash
cd frappe-bench
bench --site <site> set-config host_name "https://kursverwaltung.example.ch"
bench --site <site> clear-cache
```

## 6. Troubleshoot A Failed Save

If saving Payrexx Settings fails:

1. Confirm the instance name matches Payrexx exactly.
2. Confirm the API secret is current.
3. Confirm outbound network access from the bench.
4. Try saving in Sandbox first.

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

## 8. Run Tests

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
