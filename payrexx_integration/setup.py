from __future__ import annotations


def ensure_payout_reconciliation_fields() -> None:
	"""Install ERPNext links only when the optional accounting doctypes exist."""
	import frappe
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	required = ("Account", "Bank Account", "Bank Transaction", "Cost Center", "Payment Entry")
	if not all(frappe.db.exists("DocType", doctype) for doctype in required):
		return

	custom_fields = {
		"Payrexx Settings": [
			{
				"fieldname": "payout_reconciliation_section",
				"fieldtype": "Section Break",
				"label": "Synthetic Payout Acceptance",
				"insert_after": "transaction_reconciliation_cursor",
				"collapsible": 1,
			},
			{
				"fieldname": "enable_synthetic_payout_acceptance",
				"fieldtype": "Check",
				"label": "Enable Synthetic Payout Acceptance",
				"insert_after": "payout_reconciliation_section",
				"default": "0",
				"description": "Developer/test-only TEST evidence gate. It can never enable LIVE reconciliation.",
			},
			{
				"fieldname": "payout_clearing_account",
				"fieldtype": "Link",
				"label": "Payout Clearing Account",
				"options": "Account",
				"insert_after": "enable_synthetic_payout_acceptance",
			},
			{
				"fieldname": "payout_destination_bank_account",
				"fieldtype": "Link",
				"label": "Payout Destination Bank Account",
				"options": "Bank Account",
				"insert_after": "payout_clearing_account",
			},
			{
				"fieldname": "payout_fee_expense_account",
				"fieldtype": "Link",
				"label": "Payout Fee Expense Account",
				"options": "Account",
				"insert_after": "payout_destination_bank_account",
			},
			{
				"fieldname": "payout_fee_cost_center",
				"fieldtype": "Link",
				"label": "Payout Fee Cost Center",
				"options": "Cost Center",
				"insert_after": "payout_fee_expense_account",
			},
		],
		"Payrexx Payout Evidence": [
			{
				"fieldname": "bank_transaction",
				"fieldtype": "Link",
				"label": "Bank Transaction",
				"options": "Bank Transaction",
				"insert_after": "reconciliation_reason",
				"read_only": 1,
				"allow_on_submit": 1,
			},
			{
				"fieldname": "payout_payment_entry",
				"fieldtype": "Link",
				"label": "Payout Payment Entry",
				"options": "Payment Entry",
				"insert_after": "bank_transaction",
				"read_only": 1,
				"allow_on_submit": 1,
			},
		],
		"Payrexx Payout Transfer": [
			{
				"fieldname": "integration_request",
				"fieldtype": "Link",
				"label": "Integration Request",
				"options": "Integration Request",
				"insert_after": "reference_id",
				"read_only": 1,
			},
			{
				"fieldname": "payrexx_payment_entry",
				"fieldtype": "Link",
				"label": "Receipt Payment Entry",
				"options": "Payment Entry",
				"insert_after": "integration_request",
				"read_only": 1,
				"search_index": 0,
				"unique": 1,
			},
		],
	}
	create_custom_fields(custom_fields, ignore_validate=True, update=True)
	for doctype in custom_fields:
		frappe.clear_cache(doctype=doctype)
