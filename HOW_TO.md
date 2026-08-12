# Payrexx Integration - How To

Payrexx Integration adds Payrexx hosted checkout support as a standalone app on top of upstream `payments`.

## 1. Create Payrexx Settings

1. Open **Payrexx Settings**.
2. Set **Gateway Name**, **Instance Name**, **API Base Domain**, and **API Secret**. There is no separate Environment field; use distinct Gateway Names such as `Sandbox` and `Live`. The API version is pinned by the app at `v1.16` and is not editable per gateway.
3. Select an **Automation User** that is an enabled System User with only the ERPNext permissions needed to create/settle Payment Requests and receive accounting-review ToDos. Configure this independently for every gateway row.
4. Review **Supported Currencies**, the optional **PSP Whitelist**, **Gateway Validity**, and redirect overrides.
5. Leave **Allow TEST Transactions** off on every gateway that handles real money. Turn it on only for a sandbox gateway: it lets Payrexx's simulated payments settle documents, which is required for hosted sandbox acceptance and dangerous anywhere else.
6. As soon as **Gateway Name** is filled, copy the callback URL shown on the unsaved form and create that webhook in Payrexx.
7. Paste Payrexx's per-webhook key into the required **Webhook Signing Key** field, then save.

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
the client repeats the request once on `api.payrexx.com`. A 404 repeats only for
Gateway creation, where the custom API host may not be provisioned. The
credential probe rejects every 404. Therefore Gateway creation on a custom host can send the same POST
twice, once per host, after 401/403/404. A 404 for a specific existing-checkout
Gateway lookup does not fall back; verify the configured API domain and Gateway
in Payrexx instead. This host fallback is separate from rate-limit and database
deadlock retry. Payrexx exposes no idempotency-key field and does not document
`referenceId` as unique, so do not assume the provider de-duplicates the two
requests.

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

Set the webhook's **content type to JSON**. Payrexx also offers "Normal (PHP-Post)" form encoding, which this app does not decode; a delivery sent that way is rejected with an error naming this setting, so if you see that message the fix is here rather than in the app.

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

Python invoice/dunning renderers should use the owned fail-soft boundary instead
of adding another broad `try/except` around the Jinja helper:

```python
from payrexx_integration.api import safe_pay_url

payment_url = safe_pay_url(sales_invoice.name, gateway_name="Live")
```

`safe_pay_url` returns an empty string after an ordinary configuration or URL
failure and writes exactly one fixed, sanitized, empty-metadata Error Log entry.
Consumer apps must not wrap it with another error log. It deliberately re-raises
`QueryDeadlockError` and `QueryTimeoutError` without logging so the caller can
retry its complete transaction. It also returns an empty string before gateway
resolution when the Sales Invoice is not submitted, is a return, is already
paid, or its positive outstanding amount no longer equals the original rounded
payable total. Consumer apps keep additional product opt-ins, but must not
weaken this canonical invoice-state gate.

The helper returns a signed GET URL:

```text
GET /api/method/payrexx_integration.api.pay_invoice?si=<Sales Invoice>&gateway_name=<Payrexx Settings name>&token=<hmac>
```

New tokens use an app-purpose key derived as
`SHA256(encryption_key || ":payrexx_integration:pay-link")`. Verification also
accepts pre-derivation signatures made with the raw site key so already-sent
links keep working; gateway-unbound legacy links additionally retain their
invoice-only payload while gateway resolution remains unambiguous.

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
retried at most three times only before a Payrexx Gateway POST. After contact
starts, the checkout transaction is not retried for a database deadlock, and
canonical-host POSTs are not retried for rate limiting. A configured custom API
host remains the exception: its Gateway-create POST currently falls back once to
`api.payrexx.com` after 401/403/404.
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
3. For a custom domain, confirm its exact final host is present in the `payrexx_allowed_api_hosts` JSON list, e.g. `api.pay.goodvantage.ch`. A 401/403 repeats the request once on `api.payrexx.com`; a 404 repeats only Gateway creation, not credential probing or concrete retrieval. For Gateway creation this is a second POST, not a rate-limit retry.
4. Confirm the Automation User is present, enabled, and a System User.
5. Confirm the API secret is current.
6. Confirm outbound network access from the bench.
7. Try saving in Sandbox first.

The app pings `GET /Gateway/0/`; only HTTP 200 with the exact Payrexx JSON object
`{"status":"error","message":"No Gateway found with id 0"}` means credentials
are accepted. Partner-host 404 responses, prefixed/substring messages, extra
keys, and every other successful envelope are rejected.

Every final unexpected provider call writes exactly one direct Error Log entry.
`Payrexx request failed` identifies transport failures and `Payrexx response
failed` identifies HTTP-200 error envelopes or incomplete provider metadata;
both contain only operation, exception class, bounded HTTP status
(`http_status=None` without a response), and a fixed summary. The URL-rendering
boundary uses `Payrexx pay URL unavailable`. All rows have empty metadata and
contain no exception text, URLs, invoice/payer/request data, credentials, tokens,
response bodies, or traceback frames. The boundary does not send the exception
or frames to Sentry. Intermediate retries and tolerated statuses produce no row,
and checkout/QR controllers do not add an outer entry. Read credentials from
`Payrexx Settings` in Desk when you need to compare them, never from a log.

All provider calls use a 5-second connect and 30-second read timeout. A timeout
is surfaced through the same failure contract, makes exactly one transport
request even for a custom API host, and never makes a Gateway-create POST safe
to replay. Database-level transaction retry behavior is unchanged.

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
Other downstream payment-hook failures return HTTP 503 so Payrexx can retry and
write one context-free `Payrexx webhook failed` Error Log entry. The entry has
empty metadata and omits the exception, transaction/subscription body, request,
and traceback. Compact ignored/unclaimed observations are written only to
`sites/<site>/logs/payrexx_integration.log`. The app no longer marks the
Integration Request complete in a separate manual commit before the referenced
document accepts the payment.

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
3. If one or more unused Gateways exist and no complete local request owns them, retrieve every exact Gateway id and search the exact `referenceId` in Payrexx. With explicit Gateway DELETE permission, conditionally delete each Gateway only when its `invoices[].transactions[]` collections and the provider transaction search are empty; if no Gateway exists, there is nothing to delete.
4. If any Gateway has a transaction or its commit/host outcome remains ambiguous, do not delete that Gateway or pay again; reconcile that existing evidence through the normal webhook/success path and accounting review.
5. Never manually commit/recreate incomplete local state or automate provider deletion to close an unpaired pending record. Payrexx documents `DELETE /Gateway/{id}/`, but this app deliberately exposes no wrapper for it.

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

This app does not initiate captures of `reserved` transactions, later charges of
`authorized` transactions, checkout cancellation/voids, or refunds. Payrexx also
documents Gateway DELETE, but the app has no Gateway-delete wrapper or Desk
action. It records provider refund/dispute webhooks as evidence and
accounting-review work, but posts no ERPNext reversal. Perform an authorized
provider action in Payrexx and post the approved ERPNext reversal manually. If
Gateways are discovered, conditionally delete each exact one only after proving
it transaction-free; do not infer a refund from a failed or chargeback
Integration Request, and never cancel submitted Payment Entries automatically.

## 10. Handle A Refund Issued In Payrexx

Refunds are made in the Payrexx dashboard. ERPNext does not initiate them and
does not post the reversal — it shows you that one happened.

When Payrexx sends the refund webhook, the app:

1. Records the refund on the Integration Request (amount, currency, and the
   transaction it reverses), leaving the original settlement evidence intact.
2. Opens a High-priority ToDo naming the refunded amount.
3. Comments on the paid document — the Sales Invoice, or the Donation for
   Good NPO donations.

The Integration Request stays **Completed**: the payment really did settle, and
the refund is a later event, not a retraction of that fact.

**You still have to post the accounting reversal manually.** Nothing in the
ledger changes automatically: no Payment Entry is cancelled or created, the
invoice keeps its paid status, and a Donation keeps `paid`. Work the ToDo,
post the reversal your accounting policy requires, then close it.

Partial refunds appear as separate entries and separate ToDos, one per refund.
A `refund_pending` status is recorded but raises no ToDo — nothing has moved
yet; the ToDo appears when the refund lands. A `disputed` status raises its own
ToDo and may be followed by a chargeback, which has its own workflow (§9).

## 11. Set Up A Subscription Checkout

Subscriptions are created by sending the payer through a normal hosted checkout
that carries subscription parameters — not by calling `POST /Subscription/`,
which needs a Payrexx contact id that does not exist until the payer has paid
once.

Pass these to `get_payment_url` alongside the usual arguments:

| Argument | Required | Example | Notes |
|---|---|---|---|
| `subscription_state` | yes | `True` | Turns the checkout into a signup |
| `subscription_interval` | yes | `P1M` | Monthly `P1M`, quarterly `P3M`, yearly `P1Y` |
| `subscription_period` | no | `P1Y` | Total duration; omitted if not passed |
| `subscription_cancellation_interval` | no | `P1M` | Notice period; omitted if not passed |

Only months and years are accepted. **An invalid interval fails the checkout** —
it is not dropped the way an invalid QR parameter is, because a subscription
created on the wrong cadence bills the payer wrongly for as long as it runs.

No default period is sent. If your account needs one, pass it explicitly; the
values Payrexx accepts for an open-ended subscription are not well documented,
so confirm against your own account rather than assuming.

Prerequisites in the Payrexx dashboard:

1. The account must be on **Payrexx Pay** for TWINT subscriptions. An
   external-PSP TWINT connection cannot do recurring payments.
2. Subscription webhooks must point at the same callback URL as transaction
   webhooks (§3), with content type JSON.

The matching **Payrexx Settings** row also has **Enable Managed
Subscriptions**, which defaults off. Before switching it on:

1. Use that row's sandbox account to complete one managed subscription signup.
2. Capture and verify the signed lifecycle delivery against
   `payrexx/webhook_payload.py`.
3. Confirm that the lifecycle-triggered transaction recovery creates/settles the
   exact first Donation or invoice from a real `GET /Transaction/` result.
4. Confirm a later sandbox charge creates exactly one installment.

Do not enable the switch from a manufactured callback. Turning it off later is
safe for incident containment: it removes monthly from new public signups and
the provider rejects direct new subscription checkouts, while existing mandates
continue to reconcile and can still be changed or cancelled.

After signup, Payrexx owns the schedule. It decides when to charge and retries
failures per the account's dunning settings. Changing an amount takes effect
from the next interval; cancelling over the API is immediate.

Every recurring transaction is tracked by a deterministic **Payrexx Subscription
Event** before the owning-app hook runs. Its status advances monotonically, so a
`waiting` or `authorized` delivery cannot consume a later `confirmed` delivery.
A claimed same-status replay is ignored; an **Unclaimed** event retries when the
same webhook is replayed after the provider hook is repaired. Unclaimed financial
events return an error to Payrexx rather than a false success. If the owning hook
raises, its partial writes are rolled back and the event retains only non-personal
financial evidence needed for replay. Refunds, disputes, and chargebacks use the
normal accounting-review reversal path.

All lifecycle, installment, reversal, and reconciliation hooks run as that
gateway's configured Automation User. A lifecycle webhook queues scoped
transaction recovery only after the callback commits, so provider I/O does not
consume Payrexx's webhook timeout. The daily scheduler queues one deduplicated
worker per Settings row and commits/rolls back each financial event and
subscription separately, so one bad row or Sandbox gateway cannot starve Live
reconciliation. It runs even when new managed subscriptions are disabled.

The worker redrives all Unclaimed events, then retrieves real subscription
transactions through a bounded UTC cursor window with a six-hour overlap, then
runs the status-only subscription list sweep. The cursor advances only when the
transaction window has no failed rows. After repairing a provider hook, you may
run that gateway immediately:

```bash
bench --site <site> execute \
  payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.reconcile_subscriptions \
  --kwargs '{"gateway_name":"Live"}'
```

Before production, capture signed lifecycle and recurring-transaction deliveries
from your own Payrexx sandbox and compare them with `webhook_payload.py`. The app
supports Payrexx's documented bare lifecycle object, an older `subscription`
envelope, and transaction envelopes with nested subscriptions, but this repository
has **not** completed that account-specific verification.
This matters because Payrexx's managed-subscription guide says it sends a
subscription webhook instead of a transaction webhook, while its transaction
webhook reference still documents nested subscription data. The app recovers
charge evidence through `GET /Transaction/`, but keep **Enable Managed
Subscriptions** off until your sandbox demonstrates that complete path.

## 12. Run Hosted Sandbox Settlement Acceptance

Use only a dedicated developer QA site and a small, fully unpaid sandbox
invoice. The runner never enters card data or invokes settlement itself.

Enable and bind the read-only evidence surface to one exact target:

```bash
bench --site <qa-site> set-config payrexx_hosted_qa_enabled 1
bench --site <qa-site> set-config payrexx_hosted_qa_gateway <Payrexx-Settings-name>
bench --site <qa-site> set-config payrexx_hosted_qa_invoice <Sales-Invoice-name>
bench --site <qa-site> clear-cache
```

The bound gateway must have **Allow TEST Transactions** enabled: the whole run
proves a TEST-mode payment settles end to end, and settlement refuses simulated
payments otherwise. `preflight` checks this and refuses with that message rather
than letting the run fail later as an opaque settlement conflict. Never enable it
on a gateway that also handles real money.

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
ERPNext records as acceptance evidence; never add destructive provider cleanup
to settlement acceptance. The separate run below may conditionally delete each
of zero or more unvisited, exactly identified Gateways only after proving that
it has no transaction.

### Verify custom-host fallback safely (deferred)

This is a separate transport test, not part of ordinary settlement acceptance.
It is **deferred** because there is currently no dedicated controllable custom
API target and no confirmed operator permission to call provider Gateway DELETE.
Do not run it against a shared custom host, a Live instance, or production
credentials. A custom-host Gateway create can produce zero, one, or multiple
provider objects, and neither an idempotency key nor documented `referenceId`
uniqueness protects it.

When both prerequisites exist, use a dedicated direct one-shot harness. It must
read credentials from the disposable sandbox Settings row, construct a
`PayrexxClient` for the dedicated target without saving or mutating that row,
and call `create_gateway()` once. It must **not** call `pay_invoice`,
`PayrexxSettings.get_payment_url()`, ERPNext `make_payment_request`, or any path
that creates a local Payment Request or Integration Request.

1. Before changing configuration, snapshot the exact sandbox Settings values
   and the exact `payrexx_allowed_api_hosts` representation, including whether
   the site-config key is absent, present with an empty list, or populated.
   Snapshot the relevant Payment Request, Integration Request, Error Log, and
   Payrexx recovery-log baselines. Start an outer `try/finally` immediately
   after this snapshot; every following step, including validation and cleanup,
   belongs inside `try`.
2. Add only the exact dedicated target to the temporary allowlist and pass its
   base domain directly to the harness client; do not save the Settings row.
   Keep the injector disarmed. Clear/reload site configuration, then validate
   the source row and harness client before fault injection: expected sandbox
   instance, unchanged Settings values, app API `v1.16`, exact final-host
   allowlist, accepted small test currency/amount, working credentials, and one
   healthy non-creating `GET /Gateway/0/` through the custom target. Confirm the
   target recorded one probe and the client did not enter canonical fallback.
   Confirm provider permission to retrieve and DELETE sandbox Gateways. Before
   arming the fault, prove that cleanup path with a separate unique
   `PRX-CLEANUP-YYYYMMDD-<random>` canary: create it once through the healthy
   dedicated target with the same direct harness, retrieve its exact Gateway id,
   prove every `invoices[].transactions[]` collection empty, DELETE that exact id
   on its source host, and confirm by retrieval plus dashboard search that it is
   gone. Also confirm the canary created no local Payment Request, Integration
   Request, or recovery-journal row. Abort before the fallback POST if canary
   creation is ambiguous, any transaction exists, deletion fails, or absence
   cannot be proven.
3. Choose one status (401, 403, or 404) and a unique
   `PRX-FALLBACK-YYYYMMDD-<random>` `referenceId`. Confirm provider/dashboard
   searches return no existing Gateway or transaction for that exact reference.
   Arm the target for exactly the next matching form-encoded
   `POST /v1.16/Gateway/`: record it, return the selected status, do not forward
   it, then disarm automatically. Use a new reference and fresh one-shot arm for
   every status; never combine statuses in one run.
4. Invoke the direct harness exactly once with only the small positive
   amount/currency, unique `referenceId`, non-sensitive purpose, and safe
   sandbox redirects. Capture its returned Gateway id or exception plus the
   custom-target request count and canonical response metadata. Never open the
   checkout URL, enter payer data, or submit payment.
5. Assert local payment state exactly: no new Payment Request or Integration
   Request and no Payrexx recovery-journal entry. The injected first-host HTTP
   failure may create the client's normal Error Log; retain its redacted name as
   expected transport evidence rather than deleting it or inventing local
   payment ownership.
6. Resolve external state without assuming `referenceId` uniqueness. Combine
   custom-target logs, harness response/error, canonical provider search, and
   dashboard search; retrieve zero, one, or every discovered Gateway id on its
   exact host. Record each id, host, status, invoices, and nested transactions.
7. If no Gateway exists, record that no deletion was required. If one or more
   exist, evaluate each separately. With the pre-confirmed permission,
   conditionally delete only an exact Gateway whose retrieval and provider
   transaction search both prove all `invoices[].transactions[]` empty. Confirm
   each attempted deletion by retrieval/search. Leave every paid, ambiguous, or
   unprovable Gateway untouched for reconciliation and incident review; cleanup
   failure must not trigger another create.
8. In the outer `finally`, unconditionally disarm the injector first, restore
   the exact prior Settings values and exact prior allowlist representation,
   clear/reload configuration, and verify restoration from a fresh process.
   This restoration is attempted even when validation, create, inspection, or
   conditional deletion raises. If restoration cannot be proven, keep the
   incident open and do not run another status.
9. Retain the redacted validation result, request/adapter counts, status,
   zero/one/multiple Gateway findings, local-state assertion, conditional
   deletion results, and restoration proof. Do not retain API secrets, hashes,
   checkout URLs, or payer data.

The official PHP SDK v2.0.15 is not a substitute for this run. Its communicator
invokes the configured adapter once and has no built-in custom-host fallback or
idempotency layer; an injected adapter may implement its own retries or multiple
wire requests. Tagged code defaults to API `v1.15`, while the same tag's README
inconsistently says API `v1.11` and calls SDK v2.0.0 current stable. This app pins
API `v1.16`. Its form-encoded POST remains supported by Payrexx's official
Gateway OpenAPI.

## 13. Static QR Codes (TWINT)

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

## 14. Review Captured Payout Evidence

Payout evidence arrives passively through the same signed JSON webhook URL in
Section 3. Do not trigger a payout merely to populate local records.

1. Open **Payrexx Payout Evidence** and filter by the exact Payrexx Settings row,
   mode, payout UUID, currency, or status.
2. Compare the exact provider-minor-unit payout amount with the transfer rows.
   Every transfer also has item rows linked by **Transfer Index**.
3. Signed evidence remains review-only. Do not treat transaction UUID/reference
   or a `sent` status as authority to post accounting.
4. Treat **Settlement Ready** as provider evidence only. It is true solely for
   Payrexx status `sent`; it is not proof that ERPNext bank accounting was
   posted.
5. Investigate a rejected replay in Payrexx rather than editing the evidence.
   Only `processing` may advance, and only to `sent` or `failed`; composition is
   immutable.
6. If Payrexx records HTTP 503, wait for/retry the delivery and inspect **Error
   Log** for `Payrexx payout webhook failed` or, when processing failed before
   body classification, `Payrexx webhook failed`. That row intentionally
   contains only operation/class/status and empty metadata. It never contains
   the payout body. Deadlock/timeout retries may have no Error Log row.

The full IBAN, account holder, merchant, owner, and raw webhook are intentionally
unavailable. Operators see only destination type and IBAN last four; the hidden
site-keyed hash supports exact destination matching without retaining the IBAN.
Authenticated `initiated`, `pending`, and `under-review` evidence remains
non-settlement-ready even though Payrexx says it normally sends no webhook for
those states.

### Create synthetic acceptance evidence

This is a developer/test acceptance workflow, not a production payout workflow.
There is intentionally no LIVE enable setting.

1. On **Payrexx Settings**, enable **Allow TEST Transactions** and **Enable
   Synthetic Payout Acceptance** only on a developer/test site.
2. Configure the payout clearing Account, destination Bank Account, fee expense
   Account, and fee Cost Center. All accounts must use the company's
   two-decimal default currency; the Bank Account must carry its real IBAN and
   ledger account. Foreign-currency accounting is intentionally unsupported.
3. Confirm each source is a Completed TEST Integration Request for a submitted,
   fully paid Sales Invoice Payment Request under this exact gateway. The
   Payment Request payment account must be the configured clearing account, and
   its recorded submitted receipt Payment Entry must allocate only to that exact
   Payment Request/Sales Invoice. Do not edit or replay signed provider data.
4. From a controlled bench console, call the non-whitelisted function with
   Integration Request names and the exact confirmation text:

```python
from payrexx_integration.payout_reconciliation import create_synthetic_acceptance_evidence

create_synthetic_acceptance_evidence(
	"Sandbox",
	["<integration-request-1>", "<integration-request-2>"],
	"CREATE SYNTHETIC PAYREXX TEST PAYOUT",
)
```

5. Put the returned exact `SYNTHETIC-PAYOUT-*` reference in the synthetic EBICS
   `TxDtls.Refs.AcctSvcrRef`. The booked credit must also match net amount,
   currency, payout date, and configured destination Bank Account.
6. Verify the resulting Internal Transfer debits the bank by net, debits the fee
   expense by total fees, and credits clearing by gross. Disable the synthetic
   gate after acceptance.

Any signed/LIVE evidence, amount-only credit, blank/different reference,
ambiguous candidate, or failed secondary check stays Review.

## 15. Run Tests

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

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_rate_limit

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_refunds

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_subscriptions

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_payout_webhooks

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_payout_reconciliation
```

`test_rate_limit` directly covers GET backoff and a canonical-host POST 405 that
is not rate-limit retried. PUT and DELETE use the same shared helper in runtime
code but are not exercised by that module. `test_checkout_security` separately
covers the current custom-host fallback with mocked responses; neither suite is
live sandbox evidence.

Browser tests live in the Playwright project:

```bash
cd frappe-bench/apps/payrexx_integration/playwright
npm install
npx playwright test
```

The core Playwright specs cover Payrexx Settings and pay-by-email endpoint errors. CI seeds only a dummy non-live `Sandbox` Payrexx Settings/Payment Gateway pair, starts and waits for the test site, and runs those core specs with failure artifacts. The optional `booking_email.spec.ts` uses `TEST_BOOKING_NAME` for an existing eligible Good Event Booking; CI does not install or seed Good Event merely for that optional check.
