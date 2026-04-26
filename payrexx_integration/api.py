# Copyright (c) 2026, Goodvantage GmbH and contributors

"""Public, whitelisted entry points for the Payrexx integration.

The main one is :func:`pay_invoice` — a stable redirect URL we can embed in
"pay later" emails. Each click lazy-creates a Payrexx Gateway via a Payment
Request linked to the Sales Invoice and 302s the customer to the hosted
checkout. The URL never goes stale because nothing about the Payrexx side is
baked into the link itself.

The link is HMAC-signed so an attacker can't enumerate invoice names and pay
on someone else's behalf (or trigger arbitrary Payment Request creation).
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.utils import get_url


# ---------------------------------------------------------------- token helpers


def _signing_key() -> bytes:
	"""Per-site secret used to sign pay-by-email links.

	Reuses the site's ``encryption_key`` so we don't introduce yet another
	secret to manage. The key is in ``site_config.json`` and is required for
	a Frappe site to boot, so it's always present.
	"""
	key = frappe.local.conf.get("encryption_key")
	if not key:
		frappe.throw(_("Site encryption_key is not configured"))
	return key.encode("utf-8") if isinstance(key, str) else bytes(key)


def _sign(invoice_name: str) -> str:
	digest = hmac.new(_signing_key(), invoice_name.encode("utf-8"), hashlib.sha256).hexdigest()
	# 32 hex chars = 128 bits of HMAC output, enough to make guessing infeasible
	# while keeping the URL compact in an email.
	return digest[:32]


def _verify(invoice_name: str, token: str) -> bool:
	if not (invoice_name and token):
		return False
	return hmac.compare_digest(_sign(invoice_name), token)


# ---------------------------------------------------------------- jinja helper


def payrexx_pay_url(sales_invoice: str | None) -> str:
	"""Return the public pay-by-email URL for a Sales Invoice.

	Registered as a jinja method via ``hooks.py`` — call from any email
	template as ``{{ payrexx_pay_url(doc.name) }}``.

	Returns an empty string when called with no invoice so jinja templates
	can guard with ``{% if payrexx_pay_url(...) %}`` cleanly.
	"""
	if not sales_invoice:
		return ""
	params = {"si": sales_invoice, "token": _sign(sales_invoice)}
	return get_url("/api/method/payrexx_integration.api.pay_invoice?" + urlencode(params))


# -------------------------------------------------------------- redirect entry


@frappe.whitelist(allow_guest=True)
def pay_invoice(si: str | None = None, token: str | None = None, gateway_name: str | None = None):
	"""Lazy-create a Payrexx Gateway for a Sales Invoice and redirect to it.

	Embedded in pay-by-email links via :func:`payrexx_pay_url`. The signed
	token authorises the caller — without it, any URL is rejected.
	"""
	if not _verify(si, token):
		frappe.throw(_("Invalid or expired payment link"), frappe.PermissionError)

	if not frappe.db.exists("Sales Invoice", si):
		frappe.throw(_("Invoice not found"), frappe.DoesNotExistError)

	sales_invoice = frappe.get_doc("Sales Invoice", si)
	if sales_invoice.docstatus == 2:
		frappe.throw(_("This invoice has been cancelled"))
	if sales_invoice.outstanding_amount is not None and sales_invoice.outstanding_amount <= 0:
		# Already paid — send the customer to the success page instead of
		# creating a duplicate Payrexx gateway.
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = get_url(
			"/payment-success?doctype=Sales Invoice&docname=" + si
		)
		return

	settings_name = gateway_name or _resolve_default_settings()

	# Re-use an existing Payment Request for this invoice if one is still
	# pending, otherwise create a fresh one. This keeps the Payment Request
	# log clean when a customer clicks the email link multiple times.
	payment_request = _get_or_create_payment_request(sales_invoice, settings_name)
	checkout_url = payment_request.get_payment_url()

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = checkout_url


# ------------------------------------------------------------------- internals


def _resolve_default_settings() -> str:
	rows = frappe.get_all("Payrexx Settings", pluck="name", limit=2)
	if not rows:
		frappe.throw(_("No Payrexx Settings configured"))
	if len(rows) > 1:
		frappe.throw(
			_(
				"Multiple Payrexx Settings exist — pass ?gateway_name=... in the pay link"
			)
		)
	return rows[0]


def _get_or_create_payment_request(sales_invoice, settings_name: str):
	gateway = "Payrexx-" + settings_name
	existing = frappe.get_all(
		"Payment Request",
		filters={
			"reference_doctype": "Sales Invoice",
			"reference_name": sales_invoice.name,
			"status": ["in", ("Draft", "Requested", "Initiated", "Partially Paid")],
			"docstatus": ["<", 2],
		},
		pluck="name",
		limit=1,
		order_by="creation desc",
	)
	if existing:
		return frappe.get_doc("Payment Request", existing[0])

	from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

	pr_name = make_payment_request(
		dt="Sales Invoice",
		dn=sales_invoice.name,
		payment_gateway=gateway,
		submit_doc=1,
		mute_email=1,
		return_doc=False,
	)
	return frappe.get_doc("Payment Request", pr_name)
