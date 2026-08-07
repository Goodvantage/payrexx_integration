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
| `Payrexx Settings` | Per-environment Payrexx credentials, gateway settings, and required automation-user ownership. |
| `Payrexx Subscription Event` | Durable non-PII claim for one recurring installment, keyed deterministically by gateway and provider transaction identity. Service-written; System Manager and Accounts Manager can inspect it. |
| `Payment Gateway` | Upstream registry row `Payrexx-<gateway_name>`, created by the settings controller. |
| `Payment Gateway Account` | Upstream ERPNext company/currency/payment-account bridge. Operators must create it after the gateway; it is not seeded by this app. |
| `Integration Request` | Upstream provider-request audit and state record. |

Saving Payrexx Settings creates/updates the matching `Payment Gateway` row through the standard payments utility.
Normal Payrexx accounts use `api_base_domain = "payrexx.com"`, producing API
calls to `https://api.payrexx.com/v1.16/...`. The API version is deliberately
pinned in code as `DEFAULT_API_VERSION`; there is no per-row `api_version`
field. Payrexx Platform / partner accounts
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
rejects a supported operation with 401/403, the client retries the same request
against trusted `api.payrexx.com`. A 404 retries only for the credential probe
or Gateway collection/create operation, where it can mean the custom API host
is not provisioned. A 404 retrieving a concrete Gateway is authoritative and
never falls back, preventing a missing custom-domain resource from being read
from another API domain.

Payrexx enforces 600 requests per 5 minutes at the CDN edge. It answers `405`
first and `403` once the limit is well exceeded, so neither status reads like a
rate limit. Idempotent requests (GET, PUT, DELETE) retry with bounded exponential
backoff on `405`/`429` and tolerate those statuses on intermediate attempts, so a
call that recovers does not leave one Error Log row per attempt while a
persistent limit still surfaces on the final attempt. `403` keeps its existing
meaning (rejected API secret, and the custom-domain fallback trigger) and is not
treated as a rate limit. A POST is never replayed: an edge rejection cannot be
distinguished with certainty from a Gateway that was in fact created.
The API version is pinned once as `DEFAULT_API_VERSION` (currently `v1.16`).

## Important Modules

| Module | Purpose |
|---|---|
| `api.py` | Signed pay-by-email URL generation and `pay_invoice` redirect endpoint. |
| `gateway_selection.py` | Generic, strict Payrexx Settings resolver for this app and downstream consumers. |
| `session_utils.py` | Validates each owning settings row's enabled System User and restores the caller session after scoped privilege switching. |
| `payrexx/payrexx_client.py` | Thin Payrexx REST client (`create_gateway`, `retrieve_gateway`, `ping_gateway`, `create_qr_code`, `delete_qr_code`); host trust and credential-safe request execution. |
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
control. Before provider contact it stores the owning `payrexx_settings` and
`payrexx_success_token_version` marker in the existing Integration Request data;
provider id/hash/link and canonical amount/currency are then saved in that same
caller transaction. Provider failures therefore roll back Payment Request and
Integration Request state together rather than persisting an incomplete
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

Caller-supplied absolute return URLs are accepted only when their origin is the
canonical `host_name` origin or an operator-configured `*_public_base_url`
origin (`safe_return_url` in `url_utils.py`). Apps such as `good_npo` that
serve the site under an additional public URL (for example a Tailscale tunnel
on a development bench) advertise it through their own `<app>_public_base_url`
site-config key. Origins are normalized as scheme, canonical hostname, and
effective port (`https` = 443, `http` = 80). HTTPS configuration never trusts
the corresponding HTTP origin; userinfo, malformed ports/origins, and
scheme-relative values are rejected. Site config is operator-owned, so the
allowlist expands only through an explicit deployment setting, never guest
input.

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
The signature is HMAC-SHA256 over the raw body. Payrexx documents lowercase
hexadecimal; the verifier decodes it and compares the digest bytes. Base64 is
retained only as a compatibility form for previously observed/account-configured
deliveries.
The webhook's content type must be JSON in the Payrexx merchant account. The
alternative "Normal (PHP-Post)" form encoding is authentic but not JSON; a
delivery carrying a non-JSON content type is rejected — after the signature
is verified — with an error naming the setting that produced it, rather than
surfacing as a parse failure against a correctly signed request.
After signature verification, payment side effects resolve the owning
`Payrexx Settings.automation_user`. The configured user must exist, be enabled,
and be a System User when the operation runs. Checkout uses the explicitly
selected settings row; settlement and accounting-review ToDos use the
Integration Request's `payrexx_settings`, with its existing `payment_gateway`
metadata as the legacy ownership fallback. Missing or invalid configuration
fails closed, and no path guesses `Administrator` or reads another app's
settings. `session_utils.as_automation_user` always restores the original user,
session id, and session data. The settings controller owns this context around
source-extension validation, Integration Request writes, and provider setup, so
direct ERPNext and downstream callers behave like `pay_invoice`; nested entry by
`pay_invoice` does not restore the outer session early. A confirmed Integration Request that
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
such as `authorized`, `reserved`, `waiting`, and provider failures are ignored
and cannot replace its confirmed transaction evidence. Post-settlement refund
and dispute statuses append reversal evidence while preserving Completed; a
verified `chargeback` is the only webhook status allowed to move a Completed
request to Failed. Callback mapping is serialized on the Integration Request
row. Once chargeback evidence exists,
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
If a legacy request has neither binding on a multi-gateway site, only confirmed
settlement is refused as ambiguous. Authenticated non-confirmed evidence,
including chargebacks and provider failures, is still recorded using the
settings row that verified the webhook.
If a webhook is missing `referenceId`, references an unknown Integration
Request, or references an Integration Request whose service is not `Payrexx`,
the callback logs only a compact transaction summary
(`reference_id`, status, transaction id/uuid, mode, instance, and payment
request id) instead of the full Payrexx payload, because the full payload can
contain payer contact data.

Success redirect endpoint:

```text
GET /api/method/payrexx_integration.api.payment_success?ir=<Integration Request>&gateway_name=<Payrexx Settings name>&token=<hmac>
```

Every newly created checkout marks its Integration Request with
`payrexx_success_token_version = 1` and signs the return URL over
`<ir>|<gateway_name>|payment_success` using the site's encryption key. The
endpoint verifies supplied tokens before looking up the request, and a marked
request requires the exact integer marker version, a valid token, and the exact
owning gateway. Marker-key absence alone identifies legacy data; falsey,
boolean, string, and unknown present values fail closed. Unknown,
unsigned-marked, and otherwise invalid references return the same permission
failure. Only unmarked Integration Requests keep compatibility with already
issued unsigned legacy return URLs; newly marked requests have no broad
unsigned fallback.

After authentication, Payrexx success redirects reconcile the Integration Request by fetching the
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

## Static QR Codes (TWINT-capable)

`PayrexxSettings.create_static_qr(webshop_url)` creates a permanent Payrexx
static QR code (`POST /QrCode/`) whose scan target is `webshop_url`, and
returns the provider payload: `uuid`, `webshopUrl`, and ready-made `png` /
`svg` images as base64 data URIs. `delete_static_qr(uuid)` removes the code
(`DELETE /QrCode/{uuid}/`); a provider-side 404 counts as deleted. Neither
method is whitelisted — downstream callers (e.g. Good NPO campaign QR
generation) own permission checks. The target URL must be an absolute
http(s) URL without userinfo; the UUID must match the provider's shape.

The target must also live on an origin this site publishes: the canonical
`host_name` origin or an operator-configured `*_public_base_url` origin, the
same allowlist `safe_return_url` uses, shared through
`url_utils.is_allowed_public_origin`. A static QR is permanent and printed, and
the calling app resolves its own public base (Good NPO reads
`good_npo_public_base_url`, then `good_demo_public_base_url`, then
`host_name`), so a stale or mistyped value would otherwise mint permanent codes
pointing at a foreign origin without either layer noticing. A campaign URL
built from a configured `good_npo_public_base_url` passes; an unconfigured
origin, an HTTP downgrade, or a different effective port fails closed before
Payrexx is contacted with "Invalid QR code target URL".

Because the controller treats a deletion 404 as "already deleted", it declares
that status to the client as expected (`delete_qr_code(...,
expected_statuses=(404,))`). The client re-raises it as before but writes no
Error Log row, so a normal cleanup of an already-removed code produces no
staff-visible error. The declaration is per call: every other request, and
every undeclared status, is logged exactly as before.

Like every other settings-controller provider path, both methods run inside
`as_automation_user(self)`: a settings row without a valid, enabled System
User as its Automation User fails closed before Payrexx is contacted, and the
caller's session is restored afterwards. The QR delete is a concrete-resource
call, so a 404 from a custom Platform API host is authoritative and does not
retry `api.payrexx.com` (only 401/403 does).

Scan behavior: a plain camera scan opens `webshop_url` unchanged; a TWINT-app
scan (enabled by default for verified Swiss Payrexx accounts) opens it with
`qr_code_session_id` plus `returnAppScheme` (iOS) or `returnAppPackage`
(Android) appended as query parameters. The landing page forwards those into
checkout creation as the `get_payment_url` kwargs `qr_code_session_id` and
`return_app`. When present and valid, the Gateway payload carries
`qrCodeSessionId` / `returnApp`, and `get_payment_url` returns the Gateway
response's `appLink` (the deep link back into the TWINT app) instead of the
hosted checkout `link`. The Integration Request data still records the
canonical hosted URL in `payrexx_checkout_url` and adds
`payrexx_gateway_app_link`; webhook settlement is unchanged.

Both values originate from a guest query string, so they are sanitized against
a strict character allowlist and **dropped silently** when invalid — a
checkout without them simply behaves as a plain hosted checkout. `returnApp`
is never sent without a session id.

## Supported Payment Operations

The payment client surface is `create_gateway`, `retrieve_gateway`, and
`ping_gateway`; the Subscription set `create_subscription`,
`retrieve_subscription`, `list_subscriptions`, `update_subscription` and
`cancel_subscription`; paged, time-bounded `list_transactions`; plus the
non-payment static QR helpers `create_qr_code` / `delete_qr_code` documented
above. There is no Gateway deletion.

`create_subscription` is rarely usable: Payrexx requires a `userId` that only
exists once the payer has transacted, so subscriptions are normally created by
sending the payer through a Gateway built with `subscriptionState`.
`list_subscriptions` sends `offset` and `limit` as query parameters, matching
the official PHP SDK's GET transport. `list_transactions` uses the same query
transport with the SDK's UTC greater-than/less-than, own-transactions, order,
offset, and limit controls. Neither sends a GET JSON body.
`update_subscription` changes the amount from the **next** interval, not the
charge already taken. `cancel_subscription` is immediate and is the only
cancellation the API offers — end-of-period notice exists solely in the
merchant admin. A declared provider 404 is idempotent cancellation success; a
HTTP-200 error envelope is still an error and never changes local schedule state.
Webhook and success-return reconciliation settle only actual `confirmed`
transactions; Gateway status alone cannot settle.
A confirmed transaction Payrexx marks as simulated (`mode` other than `LIVE`, or
`invoice.test` set when `mode` is absent) is refused with the terminal
`test_transaction` settlement conflict unless the owning Payrexx Settings row
enables **Allow TEST Transactions**. A test payment carries a valid signature and
matches every amount, currency, and company check, so mode is the only evidence
that separates it from real money. Enable the flag on sandbox gateways only;
hosted sandbox acceptance requires it and `preflight` refuses to run without it.
Browser-return reconciliation copies the parent Gateway invoice's reference,
currency, and TEST marker into the selected transaction before applying this
same gate; a transaction that omits its own `mode` cannot lose the parent marker.
`authorized` and `reserved` callbacks record the Integration Request as
`Authorized`, but this app has no later-charge or capture operation.
`cancelled`, `declined`, `error`, and `expired` callbacks mark the request
failed; they do not call Payrexx to cancel or void anything. `chargeback` has
the accounting-exception workflow above.

Refunds are issued in the Payrexx dashboard; this app never initiates one and
never posts the reversal. What it does is make the refund visible. A
`refunded`, `partially-refunded`, `refund_pending`, or `disputed` webhook is
appended to the Integration Request's `payrexx_reversals` list with its amount,
currency, and originating transaction. It does not replace `payrexx_transaction`
— that stays the record of what was collected — and it does not change the
request status, because a refunded payment did settle and rewriting that to
Failed would misstate the history reconciliation reads.

Entries are keyed on the reversal transaction's own uuid/id, so Payrexx's
up-to-ten retries record once while two genuine partial refunds record twice.
Every reversal except `refund_pending` (no money has moved yet) raises a
High-priority accounting-review ToDo carrying the amount, and leaves a comment
on the paid document: the Payment Request's Sales Invoice directly, or whatever
a `payrexx_refund_notice_providers` hook claims first — good_npo registers one
for Donation. Chargeback and settlement-conflict terminality still win: a
reversal arriving after either is ignored.

Capture, cancellation, and the accounting reversal itself remain explicit
manual procedures.
These ordinary mappings apply only before completion. A Completed request keeps
its confirmed state and evidence when any non-chargeback webhook is delayed or
replayed. After a chargeback, every non-chargeback replay keeps the request
Failed and preserves the first chargeback evidence.

## Subscriptions

This app owns no recurring domain concept. It knows how to create a subscription
signup, how to read one back, and how to recognise the events one produces; what
a recurring donation or membership *is* belongs to the app that registers
`payrexx_subscription_event_providers`.

**Routing.** Every assumption about webhook payload shape lives in
`payrexx/webhook_payload.py`, and no other module reads a webhook dict by key.
Payrexx's transaction webhook page documents a `transaction` envelope; its
subscription webhook page shows a bare lifecycle object. Older observed /
SDK-derived examples wrap that object under `subscription`, so both lifecycle
forms are accepted. Strictly unmatched authenticated JSON is rejected rather
than acknowledged. Lifecycle events settle nothing; transaction envelopes take
the Integration Request path.

This reconstructed shape remains a release-verification requirement, not a
fixture-backed fact: before enabling subscription processing in production,
capture signed lifecycle and recurring-charge deliveries from the operator's own
Payrexx sandbox and compare them field-for-field with `webhook_payload.py`. Do not
manufacture a local payload and treat it as provider evidence.
Payrexx's managed-subscription guide currently says those subscriptions receive
a subscription webhook *instead of* a transaction webhook, while the transaction
webhook object page still documents a nested `subscription` field. The callback
therefore never assumes the lifecycle body moved money: after it records status,
it queues an after-commit provider pull scoped to the exact settings row,
subscription id, and reference. That worker obtains the charge from
`GET /Transaction/` and sends it through the same transaction callback path.
Provider I/O never runs inside the webhook request. The per-settings **Enable
Managed Subscriptions** switch defaults off and is the release gate: enable it
only after signed sandbox lifecycle delivery and the recovered transaction have
both been verified. Turning it off later blocks new mandates only; existing
subscriptions continue reconciliation, amount changes, and cancellation.

**The signup / installment split.** Payrexx echoes the same `referenceId` on
every charge of a subscription, because it was set once when the Gateway was
created. A monthly donor's twelfth payment therefore arrives pointing at the
Integration Request that settled their first one. The split is made on that
request's state, never on the reference:

| Referenced request | Meaning | What happens |
|---|---|---|
| Any state but Completed | the signup charge | falls through to ordinary settlement, unchanged |
| Completed, same transaction | a replay of the signup | ignored |
| Completed, new transaction | a later installment | dispatched to the owning app; nothing settled here |
| Missing | a later installment | dispatched to the owning app |

Without that split, the settled request's own terminality would silently
discard every payment after the first. The signup case deliberately returns
control to the existing settlement path rather than reimplementing it, so a
first charge and a one-off payment cannot drift apart.

The referenced Integration Request is hydrated through a current `FOR UPDATE`
read before this classification. A later recurring transaction locks or inserts
one deterministically named `Payrexx Subscription Event`. That row advances
monotonically by provider status: preliminary/failure states never consume a
later confirmation, the documented `uncaptured` state is accepted, claimed
same-status replays stop, and Unclaimed/Processing states retry after
provider-hook repair. An ordinary unclaimed event persists that state and
responds HTTP 503. If a provider raises after making partial writes, the callback
rolls back the complete webhook transaction first, then writes a fresh Unclaimed
row containing only subscription identity/status plus transaction id, status,
time, amount, currency, mode, reference, and TEST evidence. Payer and Contact
objects are deliberately excluded. This keeps partial provider effects out while
leaving enough authenticated financial evidence for local replay after Payrexx
stops retrying. A reversal of the signup transaction uses the ordinary
Integration Request reversal path. A refund, dispute, or chargeback targeting a
later installment instead gets its own durable event of type `Reversal`, retains
the original transaction identifier in the sanitized evidence, and dispatches
to the owning app without changing the shared signup Integration Request or its
confirmed transaction. Event state and provider effects remain
transaction-atomic on successful claims.

TEST-mode installments are refused under the same per-gateway opt-in that
governs one-off settlement.

Lifecycle and installment hooks run as the owning Payrexx Settings row's enabled
System User. **Reconciliation.** The daily scheduler enqueues one deduplicated
long worker per settings row, including rows whose new-signup gate is off. Each
worker first redrives every locally retained Unclaimed financial event, then
queries real transactions in ascending time order, and finally re-reads
subscriptions for lifecycle status. The initial transaction window is seven
days. Successful windows persist a UTC cursor; later runs re-read six hours of
overlap and catch up in windows of at most seven days. Pagination is capped at
100 pages of 100 rows so a bad provider response cannot loop forever. A failed
financial row prevents cursor advancement and is retried; already-claimed rows
are idempotent. Every event/subscription has its own commit/rollback boundary,
so one failure cannot starve later rows. The subscription list remains reporting
only; only authenticated rows returned by `GET /Transaction/` settle money.

## Security Model

- Pay-by-email URLs are signed with an HMAC derived from the site's `encryption_key`.
- New Payrexx success-return URLs are purpose-bound HMACs; only explicitly unmarked legacy Integration Requests accept unsigned returns.
- Payrexx webhooks are validated with `X-Webhook-Signature`.
- Every settings row owns a required enabled System User; payment and accounting-review side effects fail closed without one.
- Webhook signing key and API secret are separate values.
- API secrets are read and sent only after strict final-host validation; custom API hosts require an exact `payrexx_allowed_api_hosts` site-config entry.
- The API secret never becomes a frame variable, argument, or object attribute on the request path. It is attached through a `requests` auth callable holding it in its closure, and requests are sent as session-prepared requests instead of through `frappe.integrations.utils.make_*_request`. Frappe logs the frame variables of a failing outbound request to Error Log (and Sentry when telemetry is on) and its sanitizer does not match an `x-api-key` header key, so any secret reachable from those frames would be stored in plaintext. The same request frame drops its reference to the POST payer payload before the network call.
- Static QR targets are bound to the same published-origin allowlist as payment return URLs, so a permanent printed code can never point at an unconfigured origin.
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

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_url_utils

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_static_qr

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_rate_limit

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_refunds

bench --site development16.localhost run-tests \
  --module payrexx_integration.tests.test_subscriptions
```

```bash
cd frappe-bench/apps/payrexx_integration/playwright
npx playwright test
```

The Playwright project covers current Payrexx Settings and pay-by-email endpoint
behavior. Its optional booking-email spec accepts an existing eligible Good
Event Booking through `TEST_BOOKING_NAME`; this app does not create cross-app
event fixtures. CI runs only the two app-owned core specs after seeding a dummy
non-live `Sandbox` Payrexx Settings row (which creates its Payment Gateway) and
starting/waiting for the test site. Good Event is not installed or seeded for
the optional spec; browser reports, traces, screenshots, and video are uploaded
when the core phase fails.

## Migration

Patch `payrexx_integration.patches.v16_1.backfill_automation_user` runs after
DocType synchronization. It copies `Non Profit Settings.creation_user` only
when that legacy value names an enabled System User and only into empty existing
Payrexx Settings rows. It never invents an Administrator fallback, never
overwrites an explicit per-gateway user, and is idempotent. Rows left empty must
be configured by an operator before payment work can continue.

## Related Docs

- `README.md` - installation notes.
- `HOW_TO.md` - operator runbook.
- `AGENTS.md` - detailed implementation notes.
- `PAYREXX_INTEGRATION.md` - design rationale, repository layout, and the Payrexx provider wire format (payload/webhook shapes).
