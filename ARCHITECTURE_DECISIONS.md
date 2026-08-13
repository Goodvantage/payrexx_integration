# Payrexx Integration Architecture Decisions

This register records durable architecture decisions owned by
`payrexx_integration`. Detailed current behavior remains in
[DOCUMENTATION.md](DOCUMENTATION.md). Bench-wide decisions are in the
[bench register](https://github.com/Benema3000/frappe_docker/blob/main/development/ARCHITECTURE_DECISIONS.md).

## Maintenance

- Add a numbered decision for durable provider, trust, idempotency, dependency,
  settlement, or recovery changes.
- Supersede accepted decisions instead of rewriting their history.
- Keep this register, `DOCUMENTATION.md`, requirements, and code aligned.

## ADR-0001: Implement Payrexx As A Domain-Neutral Payments Plug-In

- Status: Accepted
- Date: 2026-08-11
- Scope: `payrexx_integration`
- Supersedes: None

### Context

Payrexx must integrate with Frappe Payments and ERPNext without maintaining a
fork of upstream `payments`. Several products consume checkout, but the gateway
must not know their business domains. Provider Gateway creation has no supported
idempotency key, which makes retries and recovery security-critical.

### Decision

Keep Payrexx Integration as a separate app depending only on `payments`. It owns
Payrexx Settings, recurring subscription-event evidence, strict gateway
selection, signed redirect/webhook boundaries, provider clients, and settlement
coordination. Upstream Payment Gateway, Payment Request, Integration Request,
Sales Invoice, and Payment Entry remain authoritative standard records.

Each Settings row owns an enabled System User and selects its API base domain;
custom-host trust is owned by the site-level exact
`payrexx_allowed_api_hosts` allowlist. Consumers call domain-neutral APIs or
register source adapters; the provider app does not import consumer domains.
Gateway creation is serialized, stops retries after provider contact where
duplication is possible, and journals explicit orphan/recovery evidence instead
of hiding the gap with an internal commit.

### Alternatives Considered

- Patch upstream `payments`: rejected because every upstream update would
  create local conflicts.
- Implement one Payrexx client in each consumer: rejected because signature,
  settlement, replay, and recovery policy would diverge.
- Blindly retry provider POSTs: rejected because Payrexx supplies no dependable
  create-idempotency key.

### Consequences

- Consumers install Payrexx explicitly and remain responsible for their own
  eligibility and accounting source policy.
- Custom hosts require exact allowlisting before secrets are read.
- Provider success with local failure can require operator reconciliation; the
  audit trail must remain sufficient to do that safely.

## ADR-0002: Own Privacy-Minimized Payout Evidence Without Accounting Policy

- Status: Accepted
- Date: 2026-08-11
- Scope: `payrexx_integration`
- Supersedes: None

### Context

Payrexx now documents signed payout webhooks containing payout composition,
destination identity, transaction references, and merchant/owner PII. Later bank
reconciliation needs durable provider evidence, but ERPNext accounting policy and
private shared-integration dependencies are not part of the provider gateway's
standalone contract.

### Decision

Payrexx Integration owns a product-prefixed payout parent with transfer and item
child rows. It validates the documented shape, exact integer arithmetic, and
status lifecycle after existing HMAC/content-type checks. A deterministic key
scoped by settings row, TEST/LIVE mode, and payout UUID makes replay idempotent;
composition is immutable and only `processing` may advance to `sent` or
`failed`.

Persist only normalized provider evidence. Discard the raw payout and all
merchant, owner, contact, and account-holder fields. Represent destination IBAN
by a site-keyed HMAC and last four characters. Keep transaction UUID and
reference ID as neutral future reconciliation keys. Do not create ERPNext bank
accounting, import `good_connector`, or call another app.

### Alternatives Considered

- Store the raw signed payload: rejected because it unnecessarily retains
  merchant/owner and destination PII and weakens schema validation.
- Add payout fields to Integration Request: rejected because one payout contains
  multiple transfers/transactions and is provider evidence, not one checkout.
- Reconcile directly to Payment Entries now: rejected because accounting match,
  ambiguity, approval, and reversal policy require a separate explicit contract.
- Put payout capture in `good_connector`: rejected because the Payrexx provider
  app owns this source evidence and must remain standalone.

### Consequences

- System Manager and Accounts Manager can inspect immutable payout evidence.
- `sent` means settlement-ready provider evidence only; it posts no ledger row.
- Future reconciliation can link by transaction UUID/reference ID without
  migrating raw provider payloads, but requires its own ADR, requirements, and
  tests.

## ADR-0003: Bridge Synthetic TEST Payout Evidence Through Optional EBICS Hooks

- Status: Accepted
- Date: 2026-08-11
- Scope: `payrexx_integration` optional ERPNext/Good Connector integration
- Supersedes: None; supplements ADR-0002 without changing evidence ownership

### Context

Provider payout capture is now durable, but no signed sandbox payout is
available for end-to-end acceptance. The bench needs accounting proof without
making LIVE automatic reconciliation possible or adding Good Connector as a
dependency of the payments plug-in.

### Decision

Keep signed TEST/LIVE evidence review-only. Add a separate, non-whitelisted,
developer/test-gated synthetic constructor that derives immutable TEST evidence
only from exact existing Completed Payrexx receipt chains. Reserve
`SYNTHETIC-*` identifiers and reject them at the signed webhook boundary.

Payrexx owns component eligibility, destination/settings checks, composition
policy, and construction of an unsaved gross-to-net Internal Transfer with one
configured fee deduction. It registers an optional exact bank-reference hook by
dotted path and never imports Good Connector. Good Connector owns EBICS import,
candidate aggregation, savepoint rollback, Payment Entry insertion/submission,
Bank Transaction allocation, and callback ordering. LIVE has no enable setting
or eligible code path.

### Alternatives Considered

- Let signed TEST payout evidence reconcile: rejected because acceptance data
  must be unmistakably synthetic and signed evidence may later be replayed on a
  differently configured site.
- Add a hard Good Connector dependency: rejected because Payrexx must remain a
  payments-only provider plug-in.
- Let Good Connector understand Payrexx schema/accounting: rejected because
  provider evidence and payout composition policy belong here.
- Match by amount when no reference exists: rejected because payout amounts are
  not unique identities.

### Consequences

- Operators must explicitly configure and later disable the synthetic gate.
- The same component receipt Payment Entry cannot belong to two payout records.
- Receipt ownership and exact bank-reference lookup are indexed; ownership is
  enforced with a current locking read plus a unique database field.
- V1 accepts only exact Sales Invoice Payment Request receipt chains in the
  company's two-decimal default currency; direct-source and FX receipts remain
  unsupported while the builder fixes exchange rates at one.
- Signed and LIVE evidence always remains Review in V1.
- Cross-app releases coordinate through hook contracts rather than dependency
  edges; focused cross-app real-document tests remain necessary.

## ADR-0004: Identify The Credential Probe By Its Asserted Fact, Not Its HTTP Status

- Status: Accepted
- Date: 2026-08-13
- Scope: `payrexx_integration` credential validation (`ping_gateway`, `_ping`)
- Supersedes: The exact-envelope/HTTP-200-only rule in REQ-PRX-SET-02

### Context

The credential probe was specified as "HTTP 200 with the exact JSON object
`{"status":"error","message":"No Gateway found with id 0"}`", and every 404 was
rejected outright. Payrexx has since restated the same response twice without
changing its meaning: the message gained an `"An error occurred: "` prefix, and
the envelope now arrives with **HTTP 404** where it previously arrived with HTTP
200. Because `raise_for_status()` runs before the body is parsed, a valid API key
could no longer be saved on any environment — the failure reached dev and
production simultaneously despite different deployed code, which is the
signature of a provider-side change rather than a regression.

Pinning an older API version was rejected: it freezes the integration against
one observed provider state and defers the same break.

### Decision

Treat the Gateway-zero envelope as the credential signal in its own right and
accept it under either HTTP 200 or HTTP 404, matching on the stable clause
`No Gateway found with id 0` rather than the whole string. The clause is
anchored with a non-digit guard so `id 00`/`id 01` — different gateways — can
never satisfy the id-0 probe. Only that exact envelope may be recovered from a
failure status; any other 404 body, and any unparseable one, is re-raised as the
provider failure it is.

404 is declared an *expected* status for this one call, so a healthy probe
leaves no Error Log row. It deliberately does **not** enable the custom-domain
fallback: a partner instance does not exist on `api.payrexx.com`, so retrying
there would turn a good credential into a second, more confusing failure.

### Consequences

- Credential validation survives provider prose and status drift; only a change
  to the asserted fact itself breaks it again.
- The probe no longer proves anything about the provider's HTTP status choice.
  Status-based assertions for this call belong in tests, not in operator docs.
- Envelope strictness moved from "exact object" to "exact clause": extra
  envelope keys are tolerated, gateway-identity precision is not.
- 401/403 keep their existing meanings, including the custom-host fallback.

## References

- [Technical documentation](DOCUMENTATION.md)
