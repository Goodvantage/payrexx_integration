# Payrexx Integration - Documentation

## Purpose

`payrexx_integration` adds Payrexx as a standalone payment gateway app without patching upstream `payments`. It provides a Payrexx Settings DocType, a REST client, webhook handling, and signed pay-by-email URLs for ERPNext Sales Invoices.

## Dependencies

```text
payments
  └── payrexx_integration
```

Do not modify `apps/payments` directly for Payrexx behavior.
The CI environment installs upstream `payments` from `version-16`, matching the
supported Frappe major instead of testing against the moving development branch.

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
`https://api.pay.goodvantage.ch/...`. Canonical Payrexx-owned API hosts under
`payrexx.com` are trusted by default. Every custom final host must be listed
exactly in the site-config JSON list `payrexx_allowed_api_hosts`, for example
`["api.pay.goodvantage.ch"]`; entries are hostnames, not base-domain values or
URLs. The strict parser rejects userinfo, schemes, paths, queries, fragments,
control characters, IP literals, malformed DNS names, wildcard entries, and
ports other than explicit HTTPS 443. `PayrexxSettings._client()` validates this
destination before reading the Password field, and the client validates it
again before constructing request headers. If an allowed custom API domain
rejects the instance credentials with 401/403/404, the client retries the same
request against trusted `api.payrexx.com`. This keeps instance API keys working
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
| `hosted_qa.py` | Read-only, exact-target evidence endpoints for explicitly enabled sandbox acceptance. |
| `tests/hosted_settlement_qa.py` | Protected external CLI for preflight and post-payment evidence. |
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
instead of requesting a second checkout only after current locking reads prove
the Sales Invoice remains wholly unpaid and the Payment Request remains an
inward, submitted, `Requested`, fully outstanding request with the exact same
full amount, currency, company, source, and gateway. It then locks the active
Integration Request and compares both references, the original amount, currency,
gateway, owning settings row, canonical provider amount/currency, provider
id/hash, and checkout URL. A partial payment, changed request, ambiguous active
request, stale URL, or incomplete metadata fails before any provider contact.
If a matching legacy Payment Request has no URL, the app recovers the URL only
from this complete exact Integration Request. A fully matching manual request
with neither URL nor active checkout may create one while both source and
Payment Request locks are held.

Existing-checkout reuse uses the same lock direction as settlement:
Integration Request, all submitted active Payrexx Payment Request rows for the
invoice, then Sales Invoice. New checkout creation has no externally visible
Integration Request yet, so it serializes first on the Sales Invoice and takes a
current locking read of every submitted active Payrexx Payment Request before
provider contact. Any other active Payrexx request, including one for another
settings row, blocks creation. Drafts and terminal/cancelled history remain
untouched. This also covers two staff-created full-value drafts submitted at the
same time: one may create the Gateway, while the other is preserved and rejected
before its provider call.

The complete pay-link checkout boundary retries a changed lock-order discovery
or `QueryDeadlockError` at most three times, rolling back before each replay.
Retries are allowed only while the current attempt has not started the Payrexx
Gateway POST. Once provider contact begins, any later deadlock is rolled back
and surfaced; external Gateway creation is never blindly replayed.

Payrexx checkout and automatic settlement support Payment Requests whose source
is a Sales Invoice. The controller checks the Payment Request source before
creating an Integration Request or calling Payrexx. Direct references are
rejected by default; an installed app may explicitly own one through
`payrexx_settlement_source_providers`, which must validate it at checkout and
again under its own row lock during settlement. Sales Orders and other unowned
source doctypes remain rejected because this app does not implement their
advance-payment payable and idempotency semantics. If an unsupported checkout
predates this guard, confirmation records a terminal settlement conflict and
does not authorize the source.

The pay-by-email endpoint is necessarily an HTTP GET. Frappe normally rolls
back GET transactions, so `pay_invoice` sets the framework end-of-request commit
flag only after Payment Request creation, the app-owned Integration Request, and
complete checkout metadata all succeed. The app intentionally does not call
core `create_request_log()`, because that helper unconditionally commits. Its
own `_create_integration_request()` inserts the Queued row without transaction
control; provider id/hash/link and canonical amount/currency are saved in that
same caller transaction. Provider failures therefore roll back Payment Request
and Integration Request state together rather than persisting an incomplete
checkout.

A provider Gateway cannot participate in the local database transaction. As
soon as Payrexx returns one, the app writes compact non-secret
`[Payrexx Gateway recovery] state=local_commit_pending` evidence to the app file
log, then registers outcome callbacks. A successful SQL transaction produces
`state=local_commit_confirmed`; an ordinary rollback produces
`[Payrexx possible orphan Gateway] state=local_rollback_confirmed`. Ambiguous
provider failures log the Integration Request `referenceId` immediately. These
records deliberately omit API keys, hashes, checkout URLs, and payer data.

There is an exact residual framework gap: Frappe clears rollback callbacks
before issuing SQL `COMMIT`. If that SQL call raises, neither outcome callback
can prove the result. The already-written, unpaired `local_commit_pending` line
therefore remains conservative durable recovery evidence. The app cannot safely
add an internal commit or automatically delete the Gateway, because the commit
outcome may be unknown and a provider transaction may already exist. Operators
must compare the local Integration Request and Payrexx reference/id, delete only
an unused Gateway with no transaction, and retry only after that review;
incomplete local state is never committed as recovery.

ERPNext `make_payment_request` re-uses any existing draft Payment Request for
the same invoice without first applying the requested gateway. The pay-link
flow therefore never deletes drafts. It reuses a pending request for the
resolved gateway only after the exact current-state validation above; if a draft
exists it preserves that draft, logs
the conflict and fails closed with an instruction to contact the accounts
team. A failed current endpoint attempt is rolled back with the request
transaction rather than cleaned up by deleting persisted records.

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
references a supported ERPNext Payment Request calls the standard
`set_as_paid()` method under a row lock. This creates and submits one Payment
Entry and lets ERPNext update the Payment Request status/outstanding amount and
the Sales Invoice outstanding amount. An explicitly registered direct-source
provider can instead revalidate and authorize only the source it owns; Good NPO
uses this for submitted, unpaid Donations. Generic references and other Payment
Request source doctypes cannot reach the authorization side effect.

Before that side effect, confirmation fails closed unless the Integration
Request references an existing inward Payment Request that is submitted and
still `Requested`, and that request references an existing submitted source
document. The locked Payment Request must remain fully unpaid and unchanged,
and the source must still have enough outstanding value for the checkout.

New checkout requests persist `payrexx_gateway_amount` as the exact integer sent
to Payrexx and `payrexx_gateway_currency` as its normalized currency. Settlement
compares provider evidence directly with those canonical values. Legacy
in-flight requests without those keys are accepted only when their original
amount converts exactly to the same two-decimal integer. Gateway creation
rejects sub-cent values and Currency masters whose `fraction_units` is not 100;
it never rounds an unsupported value into a different charge.

Settlement also requires the checkout, Payment Request, Sales Invoice, party
account, and payment account currencies to agree. This supports the unambiguous
same-currency path and conservatively rejects foreign-currency accounting paths
whose ERPNext outstanding and gateway amounts use different units. A
bank/manual partial or full payment therefore cannot be followed by a second
automatic Payrexx ledger entry.

The locked Integration Request becomes `Failed` through a direct transactional
field update, keeps the confirmed provider transaction, and stores a versioned
`payrexx_settlement_conflict` object with a terminal flag, stable reason code,
timestamp, and non-PII evidence snapshot. The direct update deliberately avoids
ERPNext's authorization validation: if another Payment Entry already paid the
request, that validation must not prevent recording the terminal conflict. The
request also receives one high-priority settlement-conflict ToDo. Later
authentic webhook and success-return replays preserve the first marker/evidence
and cannot settle or reopen the request. No automated conflict-resolution
endpoint exists; the supported path is accounting review followed by an
approved refund or allocation and ToDo closure. Any future automated reopen
flow requires a new explicit, tested contract.

When settlement creates a Payment Entry, its exact name is stored in the
Integration Request data as `payrexx_payment_entry` in the same transaction.
Hosted acceptance uses that provenance to distinguish provider settlement from
an unrelated manual Payment Entry that ERPNext may automatically match to an
open Payment Request.

Transient `QueryDeadlockError` failures, including MariaDB error 1020 after a
stale snapshot, are handled only by the complete callback, reconciliation,
chargeback, settlement, or pay-link checkout boundary. Locked one-attempt
helpers propagate the error. The boundary rolls back the failed transaction,
waits with bounded linear backoff, and replays the whole atomic unit from a fresh
snapshot, for at most three attempts; the final failure is also rolled back
before it is re-raised. Checkout is stricter: it retries only before provider
contact and never repeats a Gateway POST. This prevents code from continuing
inside an invalid transaction and ensures a partial Integration Request or
Payment Entry attempt is never retained.
Duplicate confirmed callbacks return after observing the locked completed row,
while separate requests for the same Payment Request are serialized on that
Payment Request.
Every mutable payment row used to authorize a state change is hydrated by the
same current `FOR UPDATE` read that acquires its lock. The code never performs a
scalar locking query followed by an ordinary `get_doc()` reload: under MariaDB
`REPEATABLE READ`, that second query can return the transaction's older snapshot
even though the first query locked a newer row. Standard settlement and
existing-checkout reuse keep the lock order Integration Request, Payment
Request, then Sales Invoice. Two-connection regressions establish stale
snapshots explicitly and verify that a concurrent completion, Payment Entry,
chargeback, settlement conflict, or competing manual checkout remains
authoritative.
Once an Integration Request is Completed, delayed or replayed webhook statuses
such as `authorized`, `reserved`, `waiting`, provider failures, or `refunded`
are ignored and cannot replace its confirmed transaction evidence. A verified
`chargeback` is the only webhook status allowed to move a Completed request to
Failed so the accounting-exception workflow still runs. Callback mapping is
serialized on the Integration Request row. Once chargeback evidence exists,
all later non-chargeback statuses, including `confirmed`, are ignored and the
Failed status, chargeback error, and first chargeback transaction remain
unchanged. Duplicate chargebacks only repair/reuse the same review ToDo.
Browser-return reconciliation applies the same terminal guard before calling
Payrexx.
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
but the success return is a safe fallback because payment side effects run only
when `invoices[].transactions[]` contains an actual `confirmed` transaction
whose invoice, transaction, or Gateway `referenceId` exactly matches the
expected Integration Request. A Gateway-level `confirmed` status, a missing
transaction reference, or a confirmed transaction belonging to another request
is not settlement evidence and follows the failed-payment route. If several
confirmed transactions are present, mismatched ones are skipped and only an
exactly bound transaction can be selected. An already-Completed Integration
Request likewise returns success only when its stored `payrexx_transaction` is
confirmed.
The return endpoint is an HTTP GET, so it requests Frappe's end-of-request
commit only after server verification reaches a Completed or Failed terminal
state. Waiting/non-terminal provider results remain non-committing. Without this
flag, the browser could receive `/payment-success` while the Integration Request,
Payment Request, Payment Entry, and invoice settlement all rolled back.
If provider confirmation is genuine but the pre-settlement checks record an
amount/currency or second-channel conflict, reconciliation returns false and the
browser follows the failed-payment route rather than displaying a false success.
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

The client creates hosted Gateways and retrieves Gateway/Transaction state for
Sales-Invoice-backed Payment Requests and explicitly owned extension sources.
Webhook and success-return reconciliation settle only actual `confirmed`
transactions; Gateway status alone cannot settle.
`authorized` and `reserved` callbacks record the Integration Request as
`Authorized`, but this app has no later-charge or capture operation.
`cancelled`, `declined`, `error`, and `expired` callbacks mark the request
failed; they do not call Payrexx to cancel or void anything. `chargeback` has
the accounting-exception workflow above. Refund initiation and ERPNext refund
reconciliation are not implemented: a `refunded` or otherwise unknown webhook
is stored while the Integration Request status remains unchanged. Provider-side
refund/capture/cancellation and the corresponding accounting reversal therefore
remain explicit manual procedures.
These ordinary mappings apply only before completion. A Completed request keeps
its confirmed state and evidence when any non-chargeback webhook is delayed or
replayed. After a chargeback, every non-chargeback replay keeps the request
Failed and preserves the first chargeback evidence.

## Security Model

- Pay-by-email URLs are signed with an HMAC derived from the site's `encryption_key`.
- Payrexx webhooks are validated with `X-Webhook-Signature`.
- Webhook signing key and API secret are separate values.
- API secrets are read and sent only after strict final-host validation; custom API hosts require an exact `payrexx_allowed_api_hosts` site-config entry.
- Checkout reuse requires current locked receivable state plus exact persisted provider metadata; a stored URL alone is never trusted.
- A new Gateway is rejected while any other submitted active Payrexx Payment Request exists for the invoice; terminal and cancelled history is preserved.
- Webhook diagnostics avoid logging full payer/payment payloads.
- Commit/rollback recovery logs contain provider/reference identifiers but no secret, checkout URL, hash, or payer data; an unpaired `local_commit_pending` record is an explicit manual-recovery condition.
- Guest endpoints are intentionally whitelisted and documented in `SEMGREP_OVERRIDES.md`.

## Hosted Sandbox Acceptance

The hosted acceptance surface is disabled by default. It exposes only two
authenticated POST methods in `payrexx_integration.hosted_qa`: `preflight` and
`inspect_settlement`. Both require a user with System Manager and Accounts
Manager on a developer site, a strict current-date
`PRX-SBX-E2E-YYYYMMDD-<8 hex>` run marker, the explicit
`payrexx_hosted_qa_enabled` gate, and exact gateway/invoice names from site
config. The invoice amount may not exceed 500 currency units.

Preflight performs the live Payrexx credential ping and validates the exact
submitted, fully unpaid invoice, supported currency, webhook URL, secrets, and
company/currency Payment Gateway Account. It accepts either no checkout or one
submitted pending Payment Request with one complete Integration Request whose
stored canonical gateway amount/currency exactly match that request. It
returns only document names, statuses, amount, and callback host/path; signed
payment and provider checkout URLs are never returned.

Settlement inspection is read-only. It does not call `payment_success`,
`reconcile_integration_request`, the callback, or provider mutations. It requires a
provider `confirmed` transaction in `TEST` mode whose amount/currency match the
Integration Request's persisted canonical gateway values and Payment Request,
plus the exact Integration Request reference; a Completed request carrying the
exact settlement-created Payment Entry name; Paid and zero-outstanding Payment
Request and Sales Invoice; and exactly one submitted Payment Entry allocating
its full account-currency paid amount to the invoice. A locally settled record
with missing provider evidence or `LIVE` mode fails acceptance.
Callback payloads store currency inside the transaction's invoice object. The
success fallback receives the same transaction separately from its parent
Gateway invoice; when stored currency is absent, inspection performs one
read-only Gateway retrieval and matches the exact transaction ID/UUID before
using that parent invoice currency. It never persists the retrieved payload.

The external CLI validates an exact allowlisted HTTPS origin before sending
credentials, accepts credentials only through environment variables, and writes
owner-readable redacted state. A human must complete the provider sandbox page
through the normal invoice payment link; the runner does not automate cards,
CAPTCHA, 3-D Secure, callback replay, or reconciliation.

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
bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_settlement_validation

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_checkout_security

bench --site development16.localhost run-tests \
  --module payrexx_integration.payrexx_integration.doctype.payrexx_settings.test_payrexx_settings

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_hosted_qa
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
