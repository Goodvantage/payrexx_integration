# Copyright (c) 2026, Goodvantage GmbH and contributors

"""Everything this app assumes about the *shape* of a Payrexx webhook body.

Payrexx now publishes separate transaction and subscription webhook object
pages. The transaction example uses ``{"transaction": {...}}`` while the
subscription example shows the subscription object directly. Older SDK-derived
examples wrap that lifecycle object as ``{"subscription": {...}}``, so both
forms are accepted here and nowhere else.

**It has not yet been confirmed against a delivery from our own account.**
Every field name this integration depends on lives in this module precisely so
that confirming it is a small local correction rather than a hunt through the
settlement code. Nothing outside this module should reach into a webhook dict
by key.

Two shape facts drive the routing:

1. A delivery is ``{"transaction": {...}}``, ``{"subscription": {...}}``, or
   a documented bare subscription lifecycle object.
2. A transaction that belongs to a subscription carries the subscription object
   *nested inside it*. That is how a recurring charge announces itself, and it
   is what lets a subscription charge be told apart from a one-off without
   guessing from the absence of an Integration Request.

Every field is optional: Payrexx omits what does not apply, so read defensively.
"""

from __future__ import annotations

TRANSACTION_KEY = "transaction"
SUBSCRIPTION_KEY = "subscription"
PAYOUT_OBJECT = "payout"

# Subscription lifecycle states. Payrexx's event list implies four; the object
# tables carry five. `overdue` and `in_notice` are the two that get misread:
#   overdue    a charge failed and WILL be retried; a second failure -> failed
#   in_notice  the payer cancelled before the end date, and the remaining
#              charges still follow — it is not "stopped"
SUBSCRIPTION_ACTIVE = "active"
SUBSCRIPTION_OVERDUE = "overdue"
SUBSCRIPTION_FAILED = "failed"
SUBSCRIPTION_IN_NOTICE = "in_notice"
SUBSCRIPTION_CANCELLED = "cancelled"
SUBSCRIPTION_STATUSES = frozenset(
	{
		SUBSCRIPTION_ACTIVE,
		SUBSCRIPTION_OVERDUE,
		SUBSCRIPTION_FAILED,
		SUBSCRIPTION_IN_NOTICE,
		SUBSCRIPTION_CANCELLED,
	}
)


def _as_dict(value) -> dict:
	return value if isinstance(value, dict) else {}


def _text(value) -> str:
	return str(value).strip() if value not in (None, "") else ""


def transaction_of(body) -> dict:
	"""The transaction a delivery carries, or an empty dict."""
	return _as_dict(_as_dict(body).get(TRANSACTION_KEY))


def payout_of(body) -> dict:
	"""The documented bare payout object a delivery carries, or an empty dict."""
	body = _as_dict(body)
	if body.get("object") != PAYOUT_OBJECT:
		return {}
	return body


def is_payout_event(body) -> bool:
	return bool(payout_of(body))


def subscription_of(body) -> dict:
	"""The subscription a *subscription* delivery carries, or an empty dict.

	A transaction delivery is not a subscription delivery even when it has a
	subscription nested inside it — use :func:`embedded_subscription` for that.
	"""
	body = _as_dict(body)
	if transaction_of(body):
		return {}
	if wrapped := _as_dict(body.get(SUBSCRIPTION_KEY)):
		return wrapped
	return body if _looks_like_bare_subscription(body) else {}


def is_subscription_event(body) -> bool:
	return bool(subscription_of(body))


def _looks_like_bare_subscription(body: dict) -> bool:
	"""Recognise the documented lifecycle object without guessing from one key."""
	return bool(
		subscription_id(body)
		and subscription_status(body) in SUBSCRIPTION_STATUSES
		and any(key in body for key in ("paymentInterval", "valid_until", "invoice", "contact"))
	)


def embedded_subscription(transaction) -> dict:
	"""The subscription a charge belongs to, when the charge is a recurring one."""
	return _as_dict(_as_dict(transaction).get(SUBSCRIPTION_KEY))


def subscription_id(subscription) -> str:
	return _text(_as_dict(subscription).get("id"))


def subscription_status(subscription) -> str:
	return _text(_as_dict(subscription).get("status")).lower()


def subscription_next_payment(subscription) -> str:
	"""``valid_until`` — the date of the next charge, as a plain date string."""
	return _text(_as_dict(subscription).get("valid_until"))


def subscription_interval(subscription) -> str:
	return _text(_as_dict(subscription).get("paymentInterval")).upper()


def reference_id(obj) -> str:
	"""The value we set on the Gateway, echoed back on every delivery.

	Payrexx puts it on the invoice; the transaction's own ``referenceId`` is the
	documented fallback and is what older deliveries carry.
	"""
	obj = _as_dict(obj)
	invoice = _as_dict(obj.get("invoice"))
	return _text(invoice.get("referenceId")) or _text(obj.get("referenceId"))


def transaction_status(transaction) -> str:
	return _text(_as_dict(transaction).get("status")).lower()


def is_live(transaction) -> bool:
	"""Whether the transaction moved real money.

	``mode`` is authoritative; ``invoice.test`` says the same thing as 1/0. When
	neither is present the answer is undecidable, and the caller keeps its
	pre-existing behaviour rather than inventing one.
	"""
	mode = _text(_as_dict(transaction).get("mode")).upper()
	if mode:
		return mode == "LIVE"
	invoice = _as_dict(_as_dict(transaction).get("invoice"))
	return _text(invoice.get("test")) not in ("1", "True", "true")
