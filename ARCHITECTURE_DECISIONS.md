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

## References

- [Technical documentation](DOCUMENTATION.md)
