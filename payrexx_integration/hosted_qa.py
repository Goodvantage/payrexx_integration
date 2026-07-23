"""Read-only evidence endpoints for explicitly enabled hosted Payrexx QA."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, nowdate

from payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings import (
	_canonical_gateway_amount,
	_provider_gateway_amount,
	get_webhook_url,
)

HOSTED_QA_CONFIG = "payrexx_hosted_qa_enabled"
HOSTED_QA_GATEWAY_CONFIG = "payrexx_hosted_qa_gateway"
HOSTED_QA_INVOICE_CONFIG = "payrexx_hosted_qa_invoice"
MAX_ACCEPTANCE_AMOUNT = 500
RUN_ID_PATTERN = re.compile(r"^PRX-SBX-E2E-(\d{8})-([a-f0-9]{8})$")


@frappe.whitelist(methods=["POST"])
def preflight(run_id: str) -> dict[str, Any]:
	"""Validate one exact sandbox checkout target without creating or reconciling it."""
	settings, invoice = _validate_request(run_id)
	_validate_invoice(invoice, settings)
	settings._ping()

	gateway = f"Payrexx-{settings.name}"
	gateway_accounts = frappe.get_list(
		"Payment Gateway Account",
		filters={
			"payment_gateway": gateway,
			"company": invoice.company,
			"currency": invoice.currency,
		},
		fields=["name"],
	)
	if len(gateway_accounts) != 1:
		frappe.throw(
			_("Hosted Payrexx QA requires exactly one matching Payment Gateway Account."),
			frappe.ValidationError,
		)

	payment_requests = frappe.get_all(
		"Payment Request",
		filters={
			"reference_doctype": "Sales Invoice",
			"reference_name": invoice.name,
			"docstatus": ["<", 2],
		},
		fields=[
			"name",
			"docstatus",
			"status",
			"payment_gateway",
			"payment_url",
			"grand_total",
			"currency",
		],
		order_by="creation asc",
	)
	if len(payment_requests) > 1:
		frappe.throw(
			_("Hosted Payrexx QA found multiple active Payment Requests for the configured invoice."),
			frappe.ValidationError,
		)

	payment_request_name = None
	integration_request_name = None
	checkout_present = False
	stage = "ready_for_checkout"
	if payment_requests:
		payment_request = payment_requests[0]
		payment_request_name = payment_request.name
		if payment_request.payment_gateway != gateway or payment_request.docstatus != 1:
			frappe.throw(
				_("The configured invoice has an unexpected active Payment Request."),
				frappe.ValidationError,
			)
		if payment_request.status not in {"Requested", "Initiated", "Paid"}:
			frappe.throw(
				_("The configured Payment Request is not in an accepted hosted QA state."),
				frappe.ValidationError,
			)

		integration_requests = frappe.get_all(
			"Integration Request",
			filters={
				"reference_doctype": "Payment Request",
				"reference_docname": payment_request.name,
				"integration_request_service": "Payrexx",
			},
			fields=["name", "status", "data"],
			order_by="creation asc",
		)
		if len(integration_requests) != 1:
			frappe.throw(
				_("Hosted Payrexx QA requires exactly one linked Integration Request."),
				frappe.ValidationError,
			)
		integration_request = integration_requests[0]
		integration_request_name = integration_request.name
		if integration_request.status != "Queued":
			frappe.throw(
				_("The existing Integration Request is not a pending Payrexx checkout."),
				frappe.ValidationError,
			)
		request_data = frappe.parse_json(integration_request.data) or {}
		if request_data.get("payrexx_settings") != settings.name:
			frappe.throw(
				_("The Integration Request is not bound to the configured Payrexx gateway."),
				frappe.ValidationError,
			)
		canonical_amount = _canonical_gateway_amount(
			payment_request.grand_total,
			payment_request.currency,
		)
		checkout_present = bool(
			payment_request.payment_url
			and request_data.get("payrexx_gateway_id")
			and request_data.get("payrexx_gateway_hash")
			and request_data.get("payrexx_checkout_url")
			and request_data.get("payrexx_gateway_amount") == canonical_amount
			and request_data.get("payrexx_gateway_currency") == payment_request.currency
		)
		if not checkout_present:
			frappe.throw(
				_("The existing Payrexx checkout metadata is incomplete."),
				frappe.ValidationError,
			)
		stage = "settled_pending_inspection" if payment_request.status == "Paid" else "awaiting_payment"

	callback_url = urlparse(get_webhook_url(settings.name))
	if callback_url.scheme != "https" or not callback_url.hostname:
		frappe.throw(_("The hosted Payrexx callback URL must use HTTPS."), frappe.ValidationError)

	return {
		"enabled": True,
		"run_id": run_id,
		"stage": stage,
		"settings": settings.name,
		"gateway": gateway,
		"gateway_account": gateway_accounts[0].name,
		"invoice": invoice.name,
		"company": invoice.company,
		"currency": invoice.currency,
		"amount": flt(invoice.outstanding_amount, 2),
		"callback_host": callback_url.hostname,
		"callback_path": callback_url.path,
		"checkout_present": checkout_present,
		"payment_request": payment_request_name,
		"integration_request": integration_request_name,
	}


@frappe.whitelist(methods=["POST"])
def inspect_settlement(
	run_id: str,
	payment_request_name: str,
	integration_request_name: str,
) -> dict[str, Any]:
	"""Inspect the provider-to-ledger chain without invoking reconciliation."""
	settings, invoice = _validate_request(run_id)
	payment_request = frappe.get_doc("Payment Request", payment_request_name)
	integration_request = frappe.get_doc("Integration Request", integration_request_name)
	for document in (payment_request, integration_request):
		document.check_permission("read")

	expected_gateway = f"Payrexx-{settings.name}"
	if (
		payment_request.reference_doctype != "Sales Invoice"
		or payment_request.reference_name != invoice.name
		or payment_request.payment_gateway != expected_gateway
	):
		frappe.throw(_("Payment Request ownership validation failed."), frappe.PermissionError)
	if (
		integration_request.reference_doctype != "Payment Request"
		or integration_request.reference_docname != payment_request.name
		or integration_request.integration_request_service != "Payrexx"
	):
		frappe.throw(_("Integration Request ownership validation failed."), frappe.PermissionError)

	request_data = frappe.parse_json(integration_request.data) or {}
	transaction = request_data.get("payrexx_transaction") or {}
	transaction_invoice = transaction.get("invoice") or {}
	provider_reference = transaction_invoice.get("referenceId") or transaction.get("referenceId")
	provider_currency = _provider_transaction_currency(settings, request_data, transaction)
	provider_mode = cstr(transaction.get("mode")).upper()
	canonical_request_amount = request_data.get("payrexx_gateway_amount")
	canonical_request_currency = cstr(request_data.get("payrexx_gateway_currency")).upper()
	expected_amount_cents = _canonical_gateway_amount(payment_request.grand_total, payment_request.currency)
	try:
		provider_amount = _provider_gateway_amount(transaction.get("amount"))
	except ValueError:
		provider_amount = None

	payment_reference_rows = frappe.get_all(
		"Payment Entry Reference",
		filters={"payment_request": payment_request.name, "docstatus": 1},
		fields=["parent", "reference_doctype", "reference_name", "allocated_amount"],
	)
	payment_entry_names = sorted({row.parent for row in payment_reference_rows})
	payment_entries = []
	for payment_entry_name in payment_entry_names:
		payment_entry = frappe.get_doc("Payment Entry", payment_entry_name)
		payment_entry.check_permission("read")
		payment_entries.append(payment_entry)

	invoice_reference_rows = frappe.get_all(
		"Payment Entry Reference",
		filters={
			"reference_doctype": "Sales Invoice",
			"reference_name": invoice.name,
			"docstatus": 1,
		},
		fields=["parent", "payment_request"],
	)
	invoice_payment_entries = {row.parent for row in invoice_reference_rows}
	allocated_amount = sum(flt(row.allocated_amount) for row in payment_reference_rows)
	payment_entry_amount = sum(flt(payment_entry.paid_amount) for payment_entry in payment_entries)
	checks = {
		"integration_request_completed": integration_request.status == "Completed",
		"integration_request_error_empty": not integration_request.error,
		"integration_request_gateway_bound": request_data.get("payrexx_settings") == settings.name,
		"integration_request_amount_bound": canonical_request_amount == expected_amount_cents,
		"integration_request_currency_bound": canonical_request_currency == payment_request.currency,
		"provider_transaction_confirmed": cstr(transaction.get("status")).lower() == "confirmed",
		"provider_transaction_test_mode": provider_mode == "TEST",
		"provider_transaction_amount_exact": provider_amount == canonical_request_amount,
		"provider_transaction_currency_exact": provider_currency == canonical_request_currency,
		"provider_transaction_reference_exact": provider_reference == integration_request.name,
		"provider_transaction_identifier_present": bool(transaction.get("id") or transaction.get("uuid")),
		"payment_request_submitted": payment_request.docstatus == 1,
		"payment_request_paid": payment_request.status == "Paid",
		"payment_request_outstanding_zero": flt(payment_request.outstanding_amount) == 0,
		"invoice_submitted": invoice.docstatus == 1,
		"invoice_paid": invoice.status == "Paid",
		"invoice_outstanding_zero": flt(invoice.outstanding_amount) == 0,
		"exactly_one_payment_entry": len(payment_entry_names) == 1,
		"payment_entry_recorded_by_payrexx": len(payment_entry_names) == 1
		and request_data.get("payrexx_payment_entry") == payment_entry_names[0],
		"payment_entry_submitted": len(payment_entries) == 1 and payment_entries[0].docstatus == 1,
		"payment_entry_reference_number_exact": len(payment_entries) == 1
		and payment_entries[0].reference_no == payment_request.name,
		"payment_entry_account_exact": len(payment_entries) == 1
		and payment_entries[0].paid_to == payment_request.payment_account,
		"payment_entry_references_exact_invoice": bool(payment_reference_rows)
		and all(
			row.reference_doctype == "Sales Invoice" and row.reference_name == invoice.name
			for row in payment_reference_rows
		),
		"payment_entry_allocation_exact": len(payment_entries) == 1
		and abs(allocated_amount - payment_entry_amount) < 0.005,
		"no_unexpected_invoice_payment_entries": invoice_payment_entries == set(payment_entry_names),
	}

	return {
		"run_id": run_id,
		"settled": all(checks.values()),
		"checks": checks,
		"settings": settings.name,
		"invoice": invoice.name,
		"invoice_status": invoice.status,
		"invoice_outstanding": flt(invoice.outstanding_amount, 2),
		"payment_request": payment_request.name,
		"payment_request_status": payment_request.status,
		"integration_request": integration_request.name,
		"integration_request_status": integration_request.status,
		"provider_status": cstr(transaction.get("status")).lower() or None,
		"provider_mode": provider_mode or None,
		"provider_identifier_present": bool(transaction.get("id") or transaction.get("uuid")),
		"payment_entries": payment_entry_names,
		"allocated_amount": flt(allocated_amount, 2),
	}


def _validate_request(run_id: str):
	if frappe.session.user == "Guest" or "System Manager" not in frappe.get_roles():
		frappe.throw(_("System Manager access is required."), frappe.PermissionError)
	if not cint(frappe.conf.get("developer_mode")):
		frappe.throw(_("Hosted QA is available only in developer mode."), frappe.PermissionError)
	if cint(frappe.conf.get(HOSTED_QA_CONFIG)) != 1:
		frappe.throw(_("Hosted QA is disabled for this site."), frappe.PermissionError)
	match = RUN_ID_PATTERN.fullmatch(run_id or "")
	if not match:
		frappe.throw(_("Invalid hosted QA run marker."), frappe.ValidationError)
	try:
		run_date = datetime.strptime(match.group(1), "%Y%m%d").date()
	except ValueError:
		frappe.throw(_("Invalid hosted QA run marker."), frappe.ValidationError)
	if run_date != getdate(nowdate()):
		frappe.throw(_("Hosted QA run marker must use today's date."), frappe.ValidationError)

	settings_name = cstr(frappe.conf.get(HOSTED_QA_GATEWAY_CONFIG)).strip()
	invoice_name = cstr(frappe.conf.get(HOSTED_QA_INVOICE_CONFIG)).strip()
	if not settings_name or not invoice_name:
		frappe.throw(_("Hosted Payrexx QA target configuration is incomplete."), frappe.ValidationError)
	if "Accounts Manager" not in frappe.get_roles():
		frappe.throw(_("Accounts Manager access is required."), frappe.PermissionError)
	settings = frappe.get_doc("Payrexx Settings", settings_name)
	invoice = frappe.get_doc("Sales Invoice", invoice_name)
	settings.check_permission("read")
	invoice.check_permission("read")
	return settings, invoice


def _validate_invoice(invoice, settings) -> None:
	if invoice.docstatus != 1 or invoice.get("is_return"):
		frappe.throw(_("Hosted Payrexx QA requires a submitted non-return invoice."), frappe.ValidationError)
	outstanding = flt(invoice.outstanding_amount, 2)
	payable_total = flt(invoice.rounded_total or invoice.grand_total, 2)
	if outstanding <= 0 or abs(outstanding - payable_total) >= 0.005:
		frappe.throw(_("Hosted Payrexx QA requires a fully unpaid invoice."), frappe.ValidationError)
	if outstanding > MAX_ACCEPTANCE_AMOUNT:
		frappe.throw(
			_("Hosted Payrexx QA invoice amount exceeds the acceptance ceiling."),
			frappe.ValidationError,
		)
	settings.validate_transaction_currency(invoice.currency)
	if not settings.get_password("api_secret", raise_exception=False) or not settings.get_password(
		"webhook_signing_key", raise_exception=False
	):
		frappe.throw(_("Hosted Payrexx QA requires both Payrexx secrets."), frappe.ValidationError)


def _provider_transaction_currency(settings, request_data: dict, transaction: dict) -> str:
	if not transaction:
		return ""
	transaction_invoice = transaction.get("invoice") or {}
	stored_currency = cstr(transaction_invoice.get("currency") or transaction.get("currency")).upper()
	if stored_currency:
		return stored_currency

	gateway_id = request_data.get("payrexx_gateway_id")
	if not gateway_id:
		return ""
	gateway = settings._client().retrieve_gateway(cint(gateway_id))
	transaction_id = transaction.get("id")
	transaction_uuid = transaction.get("uuid")
	for provider_invoice in gateway.get("invoices") or []:
		for candidate in provider_invoice.get("transactions") or []:
			matches_id = transaction_id and candidate.get("id") == transaction_id
			matches_uuid = transaction_uuid and candidate.get("uuid") == transaction_uuid
			if matches_id or matches_uuid:
				return cstr(provider_invoice.get("currency") or candidate.get("currency")).upper()
	return ""
