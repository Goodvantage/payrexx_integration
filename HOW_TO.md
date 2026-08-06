# Payrexx Integration - How To

Payrexx Integration adds Payrexx hosted checkout support as a standalone app on top of upstream `payments`.

## 1. Create Payrexx Settings

1. Open **Payrexx Settings**.
2. Set **Gateway Name**, **Instance Name**, **API Base Domain**, **API Version**, and **API Secret**. There is no separate Environment field; use distinct Gateway Names such as `Sandbox` and `Live`.
3. Select an **Automation User** that is an enabled System User with only the ERPNext permissions needed to create/settle Payment Requests and receive accounting-review ToDos. Configure this independently for every gateway row.
4. Review **Supported Currencies**, the optional **PSP Whitelist**, **Gateway Validity**, and redirect overrides.
5. As soon as **Gateway Name** is filled, copy the callback URL shown on the unsaved form and create that webhook in Payrexx.
6. Paste Payrexx's per-webhook key into the required **Webhook Signing Key** field, then save.

The form shows one callback URL as soon as `gateway_name` is filled, even before
the row can be saved. The URL uses the site's configured public `host_name` when
available, so admins do not accidentally copy a temporary tunnel URL. Refreshing
or saving the form replaces that hint in place instead of appending duplicate
lines. Use that URL to create the Payrexx webhook, then paste the generated
signing key back into **Webhook Signing Key**.

On save, the controller verifies credentials unless running in tests/install and creates the corresponding `Payment Gateway` row named `Payrexx-<Gateway Name>`. Saving does not create the ERPNext Payment Gateway Account required for invoice payments.
Checkout, settlement, chargeback, and accounting-review ToDo work fails closed
if that row's Automation User is missing, disabled, or a Website User. The app
does not fall back to Administrator or `Non Profit Settings`.
During upgrade, the migration copies a valid enabled System User from legacy
`Non Profit Settings.creation_user` into empty existing rows. It does not copy
an invalid value or invent Administrator; configure any row that remains empty
before resuming payment processing.

For normal Payrexx accounts, keep `api_base_domain` as `payrexx.com`. For
Payrexx Platform / partner accounts, split the login domain into instance and
base domain. Example: `customer.pay.goodvantage.ch` becomes:

```text
Instance Name: customer
API Base Domain: pay.goodvantage.ch
```

Canonical Payrexx-owned API hosts under `payrexx.com` are trusted by default.
Before saving a custom platform domain, explicitly allow the exact final API
host in site config. The value is a JSON list of hostnames, without schemes,
paths, wildcards, or non-HTTPS ports:

```bash
cd frappe-bench
bench --site <site> set-config --parse payrexx_allowed_api_hosts '["api.pay.goodvantage.ch"]'
bench --site <site> clear-cache
```

Restart long-lived web and worker processes after changing site config. The
settings field still contains the base domain (`pay.goodvantage.ch`); the
allowlist contains the final host the client contacts
(`api.pay.goodvantage.ch`). IP addresses and URL-like values such as
`https://...`, credentials, paths, queries, and fragments are never accepted.

If that custom API domain rejects an otherwise valid instance key with 401/403,
the client retries on `api.payrexx.com`. A 404 retries only for the credential
probe or Gateway creation, where the custom API host may not be provisioned. A
404 for a specific existing-checkout Gateway lookup does not fall back; verify
the configured API domain and Gateway in Payrexx instead.

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

The `gateway_name` query value must be the exact **Payrexx Settings document
name**, not a generic environment label. For example, a settings row named
`spendedirekt` requires `?gateway_name=spendedirekt`; a stale
`?gateway_name=Sandbox` callback is rejected with HTTP 417 before any accounting
mutation. After correcting an existing webhook URI, retry its failed delivery
when Payrexx provides that action. Do not create or pay a second checkout merely
because the first webhook failed.

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
submission. Repeated clicks reuse the same Payment Request and Payrexx checkout
only while current locking reads prove that the invoice remains wholly unpaid,
the Payment Request remains submitted, `Requested`, and fully outstanding, and
its amount, currency, source, gateway, and stored provider metadata all still
match exactly. A partial/manual payment or another change stops before any
Payrexx request; accounts staff must review the receivable rather than sending
the customer to the original full-value checkout.
Before a new Gateway is created, the app also checks every submitted active
Payrexx Payment Request for the invoice, across all Payrexx settings rows. If
another one exists, the new/manual submission is preserved but rejected before
provider contact. Paid, failed, cancelled, and docstatus-cancelled history does
not block a legitimate new checkout. Concurrent first clicks and lock waits are
retried at most three times only before a Payrexx Gateway POST; provider creation
is never replayed after contact has started.
The GET endpoint commits its lazy-created local records only after Payrexx has
returned a valid checkout URL and complete provider metadata. Integration
Request creation itself does not commit. A failed provider call therefore rolls
back the Payment Request and Integration Request together.
Links generated before gateway binding was introduced did not include
`gateway_name`. They continue to work when exactly one settings row exists, but
are rejected as ambiguous when multiple rows exist; resend the invoice email to
issue a gateway-bound link.

Payrexx supports Payment Requests sourced from Sales Invoices. Do not select a
Payrexx gateway on a Sales Order or another source doctype: the app rejects it
before creating a provider checkout because Sales Order advance
payable/idempotency behavior is not implemented. Installed apps may register an
explicit direct-source provider; Good NPO uses that extension for submitted,
unpaid Donations and revalidates their amount and company currency at settlement.

If an older active Integration Request has no recoverable checkout URL, the app
shows an error instead of creating a second potentially chargeable checkout;
review that Integration Request in Desk.

## 5. Success Redirect Fallback

Payrexx webhooks should still be configured. Unless an explicit success redirect
override is set, every generated Gateway returns through:

```text
GET https://<site>/api/method/payrexx_integration.api.payment_success?ir=<Integration Request>&gateway_name=<Payrexx Settings name>&token=<hmac>
```

New checkout return URLs are signed for the exact Integration Request, gateway,
and `payment_success` purpose. Do not strip or edit any parameter. Only
Integration Requests created before the success-token marker was introduced can
use their already-issued unsigned legacy URL. The marker key must be absent for
legacy compatibility; a present blank, zero, malformed, or unknown version is a
configuration/data error and fails closed.

That endpoint retrieves the Payrexx Gateway server-side and only marks the
Integration Request complete when its invoices contain an actual confirmed
transaction whose provider `referenceId` belongs to that Integration Request. A
Gateway-level `confirmed` status, missing reference, or transaction belonging to
another request does not settle or return success.
Because this return is an HTTP GET, terminal server-verified reconciliation
requests Frappe's end-of-request commit before redirecting; waiting results do
not. A success page with unchanged accounting records indicates an outdated
deployment that still rolled back the GET transaction.
If Payrexx already reports the transaction as confirmed but the original webhook
cannot be retried, use the exact signed success-return URL stored on that Payrexx
Gateway instead of paying again:

```text
https://<site>/api/method/payrexx_integration.api.payment_success?ir=<Integration-Request>&gateway_name=<Payrexx-Settings-name>&token=<original-token>
```

Do not reconstruct a new marked request's URL without its original token. An
unsigned manually built URL remains valid only for an unmarked legacy
Integration Request.

This is not a second payment. It retrieves the existing Gateway server-side and
settles only after provider confirmation. Reopening the payment URL or creating
a new checkout can produce a duplicate provider transaction and is not a
recovery procedure.
When a payment creator stored `redirect_to` in the Integration Request, the
endpoint sends the customer directly back to that same-site URL after
reconciliation instead of showing the generic `/payment-success` page. If the
Gateway contains no confirmed transaction, the customer is sent to
`/payment-failed`. Apps that
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

Downstream apps may deliberately expose the same site through an additional
public origin and store it in an app-owned site-config key ending in
`_public_base_url`. For example:

```bash
cd frappe-bench
bench --site <site> set-config good_npo_public_base_url "https://donate.example.ch"
bench --site <site> clear-cache
```

Every such key authorizes caller-supplied payment return URLs for that exact
normalized origin: scheme, hostname, and effective port must match. An HTTPS
value does not authorize HTTP, and a non-default port must be present in both
the configuration and return URL. Values with credentials/userinfo, malformed
ports, missing HTTP(S) schemes, or scheme-relative forms are ignored and cannot
authorize a return. Use plain HTTP only for an intentional local-development
origin. Restart long-lived web and worker processes after changing site config.

## 7. Troubleshoot A Failed Save

If saving Payrexx Settings fails:

1. Confirm the instance name matches the first subdomain of the checkout/login domain.
2. Confirm the API base domain is correct (`payrexx.com` for normal accounts, e.g. `pay.goodvantage.ch` for GoodVantage partner accounts).
3. For a custom domain, confirm its exact final host is present in the `payrexx_allowed_api_hosts` JSON list, e.g. `api.pay.goodvantage.ch`. A 401/403 retries once on `api.payrexx.com`; a 404 retries only for credential probing and Gateway creation, not a concrete Gateway retrieval.
4. Confirm the Automation User is present, enabled, and a System User.
5. Confirm the API secret is current.
6. Confirm outbound network access from the bench.
7. Try saving in Sandbox first.

The app pings `GET /Gateway/0/`; a Payrexx JSON response with `status: error` can still mean credentials are accepted if the error is "gateway not found".

Every failed provider call also writes an `Error Log` entry. Those entries
deliberately contain no API secret and no payer payload, so they can be shared
with support or forwarded to error telemetry as they are. Read the credentials
from `Payrexx Settings` in Desk when you need to compare them, never from a log.

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

If the link reports that the invoice, Payment Request, or checkout no longer
matches, do not reopen the provider URL. Review manual/partial Payment Entries,
the invoice outstanding amount, and the linked Integration Request. The old
full-value checkout is deliberately blocked before provider contact.

If submission reports that another active Payrexx Payment Request exists, list
all submitted Payment Requests for the Sales Invoice and all `Payrexx-*`
gateways. Keep the one with the active matching Integration Request; review and
cancel only genuinely duplicate requests under the normal accounting workflow.
Do not delete historical Paid/Failed/Cancelled rows and do not submit another
full-value draft to bypass the guard.

If the link opens but payment does not update, check **Integration Request**
rows, Payrexx webhook delivery logs, and whether Payrexx can reach the success
redirect URL on the public `host_name`.
If Payrexx shows a confirmed transaction while the Integration Request remains
Queued and unmodified, compare the failed webhook's `gateway_name` query value
with the exact Payrexx Settings document name. Correct the webhook first; retry
the delivery or use the existing transaction's success fallback. Never retry the
payment itself.
The webhook only updates Integration Requests whose service is `Payrexx`; if a
Payrexx reference ID points at a row owned by another gateway, the callback logs
the mismatch and ignores it.
Transient `tabSeries` / `QueryDeadlockError` failures, including MariaDB error
1020, are retried automatically up to three times. Each retry rolls back the
failed transaction and replays the complete callback, reconciliation,
chargeback, or settlement unit from fresh state. Retry webhook delivery manually
only if all bounded attempts fail and Payrexx receives an error response; never
retry the payment itself.
Other downstream payment-hook failures are logged and returned as webhook
errors so Payrexx can retry; the app no longer marks the Integration Request
complete in a separate manual commit before the referenced document accepts the
payment.

If checkout creation fails around a provider timeout, local rollback, or SQL
commit, inspect `sites/<site>/logs/payrexx_integration.log` for
`[Payrexx Gateway recovery]` and `[Payrexx possible orphan Gateway]` before
retrying. A provider response first writes `state=local_commit_pending`; normal
commit adds `state=local_commit_confirmed`, and ordinary rollback adds
`state=local_rollback_confirmed`. Pair records by Integration Request reference
and Gateway id. An unpaired `local_commit_pending` record is intentionally
conservative: Frappe clears rollback callbacks before SQL commit, so a commit
failure in that interval has no reliable callback outcome. Entries contain only
the Integration Request reference, settings name, linked document type/name,
and Gateway id; they never contain the API key, checkout hash, checkout URL, or
payer data. Search both local state and Payrexx by `referenceId` and Gateway id:

1. If `local_commit_confirmed` exists and the complete local Integration Request exists, use that checkout; do not create another.
2. If no Gateway exists, retry the original signed invoice link after the local issue is fixed.
3. If an unused Gateway exists and has no transaction and no complete local request owns it, delete that Gateway in the Payrexx dashboard, then retry.
4. If a transaction exists or commit outcome remains ambiguous, do not delete or pay again; reconcile that existing transaction through the normal webhook/success path and accounting review.
5. Never manually commit/recreate incomplete local state or automate provider deletion to close an unpaired pending record.

For a confirmed Payment Request, verify that its status is **Paid**, its
outstanding amount is zero, and exactly one submitted Payment Entry references
it. ERPNext updates the linked Sales Invoice outstanding amount through the
normal Payment Entry submission path.
Concurrent callbacks and manual Payment Entries are rechecked from current
row-locking reads. A delayed provider status cannot overwrite an already
Completed request, and a Payment Entry that wins the race prevents a second
automatic settlement; follow the settlement-conflict procedure below rather
than replaying the payment.

## 9. Reconcile Payments And Handle Chargebacks

For a confirmed invoice payment, verify this chain rather than relying only on the browser success page:

1. The Payrexx `Integration Request` is **Completed** and stores the confirmed transaction.
2. The linked ERPNext `Payment Request` is **Paid** with zero outstanding.
3. Exactly one submitted `Payment Entry` references that Payment Request.
4. The Sales Invoice outstanding amount reflects the submitted Payment Entry.

A Completed Integration Request is terminal for normal webhook delivery. If
Payrexx later replays `authorized`, `reserved`, `waiting`, a failure status, or
`refunded`, the request remains Completed and keeps its original confirmed
transaction evidence. `chargeback` is the intentional exception and starts the
manual accounting-reversal procedure below.

After that chargeback is recorded, the Integration Request remains **Failed**
with its original chargeback transaction and error. Later webhook replays,
including `confirmed`, and browser-return reconciliation cannot replace that
evidence. A duplicate chargeback only reuses the existing accounting-review
task.

If the Integration Request is **Failed** with a high-priority **Payrexx
settlement conflict** ToDo, Payrexx confirmed funds after the Payment Request or
invoice had already changed through another channel, or the provider amount /
currency evidence did not match. Do not retry the checkout or manually mark the
request Paid. Compare the confirmed Payrexx transaction, Payment Request,
invoice outstanding amount, and any bank/manual Payment Entries; then refund or
allocate the provider funds under the normal accounting approval process and
close the ToDo. The conflict is terminal: webhook retries, the browser success
URL, and manual edits to the Integration Request status do not constitute a
resolution and must not create a Payment Entry. There is currently no automated
reopen action. If an automated resolution path is added later, use it only after
its separate accounting approval and idempotency contract is documented and
deployed.

Payrexx checkout creation supports only Currency masters with 100 fraction
units and amounts exactly representable to two decimal places. It also settles
only same-currency accounting paths where the Payment Request, Sales Invoice,
party account, and payment account currencies agree. For a rejected
foreign-currency or non-two-decimal case, use an approved manual payment method
or configure a same-currency bank/payment account; never round the invoice or
edit stored provider evidence to force reconciliation.

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

`clear-cache` does not reliably reload `site_config.json` in long-lived web
workers. On Docker deployments, restart the backend container after changing
these keys before trusting preflight:

```bash
docker restart <project>-backend-1
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

Restart the backend workers after disabling the gate, then call preflight once
and require HTTP 403 before closing the acceptance run.

Payrexx test transactions cannot be deleted. Keep their run marker and exact
ERPNext records as acceptance evidence; never add destructive provider cleanup.

## 11. Static QR Codes (TWINT)

Downstream apps (e.g. Good NPO donation campaigns) can mint permanent static
QR codes through this app: `PayrexxSettings.create_static_qr(<landing URL>)`
returns the QR as ready PNG/SVG images. There is no Desk UI in this app; the
generating app owns the workflow and permissions.

Prerequisites for the TWINT-app scan path (in the Payrexx dashboard):

1. The Payrexx account must be **verified** and Swiss — TWINT scanning of
   static QR codes is enabled by default only then.
2. TWINT must be activated as a payment method: *Zahlungsanbieter → Payrexx
   Pay → Konfigurieren → TWINT*.

A plain phone-camera scan always works and opens the landing page directly.
Deleting a QR code in the Payrexx dashboard does not break local cleanup —
the app treats a provider 404 as already deleted, and that tolerated outcome no
longer writes an Error Log row.

The landing URL must be on an origin this site publishes: the site's
`host_name`, or an app-owned site-config key ending in `_public_base_url` (§6).
QR codes are permanent and printed, so an unconfigured, stale, or mistyped
origin fails with "Invalid QR code target URL" **before** Payrexx is contacted
instead of minting a code that points nowhere. If a downstream app's generation
fails with that message, check that its public base is set and matches the
landing URL exactly — scheme, hostname, and port:

```bash
cd frappe-bench
bench --site <site> get-config host_name
bench --site <site> get-config good_npo_public_base_url
```

An HTTPS base does not authorize an HTTP landing URL (and vice versa), and a
non-default port must appear in both.

Like checkout creation, both calls run as the settings row's **Automation
User**, so that field must name an enabled System User (§2) or QR creation and
deletion fail with "Payrexx Settings … requires an Automation User." before
Payrexx is contacted.

## 12. Run Tests

```bash
cd frappe-bench
bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_static_qr

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_settlement_validation

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_checkout_security

bench --site development16.localhost run-tests \
  --module payrexx_integration.payrexx_integration.doctype.payrexx_settings.test_payrexx_settings

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_hosted_qa

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_url_utils
```

Browser tests live in the Playwright project:

```bash
cd frappe-bench/apps/payrexx_integration/playwright
npm install
npx playwright test
```

The core Playwright specs cover Payrexx Settings and pay-by-email endpoint errors. CI seeds only a dummy non-live `Sandbox` Payrexx Settings/Payment Gateway pair, starts and waits for the test site, and runs those core specs with failure artifacts. The optional `booking_email.spec.ts` uses `TEST_BOOKING_NAME` for an existing eligible Good Event Booking; CI does not install or seed Good Event merely for that optional check.
