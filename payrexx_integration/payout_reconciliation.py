from __future__ import annotations

import hashlib
from datetime import UTC
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, now_datetime

from payrexx_integration.payrexx_integration.payrexx.payout_evidence import (
	PAYOUT_EVIDENCE_DOCTYPE,
	SYNTHETIC_EVIDENCE_ORIGIN,
	SYNTHETIC_PREFIX,
	_composition_hash,
	_evidence_key,
	_hash_iban,
	_normalize_iban,
)

SYNTHETIC_CONFIRMATION = "CREATE SYNTHETIC PAYREXX TEST PAYOUT"
SYNTHETIC_STATUS = "synthetic"
SUPPORTED_ITEM_TYPES = frozenset({"transaction", "transaction-fee", "payout-fee"})
SUPPORTED_TRANSACTION_CHANNELS = frozenset({"E-Commerce", "POS-Terminal", "Tap to Pay"})


def create_synthetic_acceptance_evidence(
	settings_name: str,
	integration_request_names: list[str] | tuple[str, ...],
	confirmation: str,
	*,
	payout_fee_minor: int = 0,
) -> dict[str, bool | str]:
	"""Create deterministic TEST-only evidence from settled local receipt chains."""
	if not (frappe.flags.in_test or cint(frappe.conf.get("developer_mode"))):
		frappe.throw(_("Synthetic payout evidence is available only in developer or test mode."))
	if confirmation != SYNTHETIC_CONFIRMATION:
		frappe.throw(_("Synthetic payout confirmation text does not match."), frappe.ValidationError)
	if isinstance(payout_fee_minor, bool) or not isinstance(payout_fee_minor, int) or payout_fee_minor < 0:
		frappe.throw(_("Synthetic payout fee must be a non-negative integer in minor units."))
	names = sorted(set(integration_request_names or ()))
	if not names or len(names) != len(integration_request_names or ()):
		frappe.throw(_("Synthetic payout sources must be a non-empty list without duplicates."))

	settings = frappe.get_doc("Payrexx Settings", settings_name, for_update=True)
	if not cint(settings.get("enable_synthetic_payout_acceptance")):
		frappe.throw(_("Synthetic payout acceptance is disabled for this Payrexx Settings row."))
	configuration = _validated_configuration(settings)
	digest = hashlib.sha256(f"{settings.name}\0{'|'.join(names)}\0{payout_fee_minor}".encode()).hexdigest()
	payout_uuid = f"{SYNTHETIC_PREFIX}{digest[:30]}"
	bank_reference = f"SYNTHETIC-PAYOUT-{digest[:22]}"
	evidence_key = _evidence_key(settings.name, "TEST", payout_uuid)
	components = [
		_validated_component(name, settings, configuration, allowed_parent=evidence_key) for name in names
	]
	company = components[0]["company"]
	currency = components[0]["currency"]
	if any(component["company"] != company or component["currency"] != currency for component in components):
		frappe.throw(_("Synthetic payout receipt components must use one company and currency."))

	existing = _locked_evidence(evidence_key)
	if existing:
		_validate_deterministic_replay(existing, names, bank_reference)
		return _synthetic_result(existing.name, created=False)

	transfers = []
	items = []
	for index, component in enumerate(components, start=1):
		transfers.append(
			{
				"transfer_index": index,
				"transfer_type": "transaction",
				"amount": component["gross_amount"] - component["fee"],
				"date_time": component["transaction_time"],
				"has_transaction": 1,
				"transaction_type": "transaction",
				"transaction_amount": component["gross_amount"],
				"transaction_uuid": component["transaction_uuid"],
				"transaction_fee": component["fee"],
				"transaction_currency": currency,
				"transaction_time": component["transaction_time"],
				"payment_brand": component["payment_brand"],
				"reference_id": component["integration_request"],
				"integration_request": component["integration_request"],
				"payrexx_payment_entry": component["payment_entry"],
			}
		)
		items.extend(
			(
				{
					"transfer_index": index,
					"provider_item_index": 1,
					"item_type": "transaction",
					"amount": component["gross_amount"],
				},
				{
					"transfer_index": index,
					"provider_item_index": 2,
					"item_type": "transaction-fee",
					"amount": -component["fee"],
				},
			)
		)
	if payout_fee_minor:
		index = len(transfers) + 1
		transfers.append(
			{
				"transfer_index": index,
				"transfer_type": "payout-fee",
				"amount": -payout_fee_minor,
				"date_time": max(component["transaction_time"] for component in components),
				"has_transaction": 0,
			}
		)
		items.append(
			{
				"transfer_index": index,
				"provider_item_index": 1,
				"item_type": "payout-fee",
				"amount": -payout_fee_minor,
			}
		)

	gross_amount = sum(component["gross_amount"] for component in components)
	total_fees = sum(component["fee"] for component in components) + payout_fee_minor
	payout_date = max(component["posting_date"] for component in components)
	normalized = {
		"payout_uuid": payout_uuid,
		"mode": "TEST",
		"amount": gross_amount - total_fees,
		"gross_amount": gross_amount,
		"total_fees": total_fees,
		"currency": currency,
		"payout_date": payout_date,
		"statement": "Synthetic acceptance evidence",
		"payer": "payrexx",
		"status": SYNTHETIC_STATUS,
		"is_manual_payout": False,
		"destination_type": "bank_account",
		"destination_iban_hash": configuration["destination_iban_hash"],
		"destination_iban_last_four": configuration["destination_iban_last_four"],
		"transfers": transfers,
		"items": items,
	}
	normalized["composition_hash"] = _composition_hash(normalized)
	evidence = frappe.get_doc(
		{
			"doctype": PAYOUT_EVIDENCE_DOCTYPE,
			"evidence_key": evidence_key,
			"payrexx_settings": settings.name,
			"payout_uuid": payout_uuid,
			"mode": "TEST",
			"provider_status": SYNTHETIC_STATUS,
			"evidence_origin": SYNTHETIC_EVIDENCE_ORIGIN,
			"reconciliation_status": "Pending",
			"reconciliation_reason": "Waiting for one exact synthetic EBICS reference match.",
			"bank_match_reference": bank_reference,
			"settlement_ready": 0,
			"amount": normalized["amount"],
			"gross_amount": gross_amount,
			"total_fees": total_fees,
			"currency": currency,
			"payout_date": payout_date,
			"statement": normalized["statement"],
			"payer": "payrexx",
			"is_manual_payout": 0,
			"destination_type": "bank_account",
			"destination_iban_hash": normalized["destination_iban_hash"],
			"destination_iban_last_four": normalized["destination_iban_last_four"],
			"composition_hash": normalized["composition_hash"],
			"received_on": now_datetime(),
			"status_updated_on": now_datetime(),
		}
	)
	for transfer in transfers:
		evidence.append("transfers", transfer)
	for item in items:
		evidence.append("items", item)
	try:
		evidence.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		existing = _locked_evidence(evidence_key)
		if not existing:
			raise
		_validate_deterministic_replay(existing, names, bank_reference)
		return _synthetic_result(existing.name, created=False)
	return _synthetic_result(evidence.name, created=True)


def get_reconciliation_candidates(*, bank_transaction, bank_reference: str, amount: Decimal):
	"""Return exact-reference identities; secondary failures make them ineligible."""
	bank_reference = cstr(bank_reference).strip()
	if not bank_reference:
		return []
	evidence_table = frappe.qb.DocType(PAYOUT_EVIDENCE_DOCTYPE)
	names = (
		frappe.qb.from_(evidence_table)
		.select(evidence_table.name)
		.where(evidence_table.bank_match_reference == bank_reference)
		.for_update()
	).run(pluck=True)
	candidates = []
	for name in names:
		evidence = frappe.get_doc(PAYOUT_EVIDENCE_DOCTYPE, name, for_update=True)
		reason = _automatic_eligibility_reason(evidence, bank_transaction, amount)
		candidate = {
			"reference_doctype": PAYOUT_EVIDENCE_DOCTYPE,
			"reference_name": evidence.name,
			"bank_reference": bank_reference,
			"eligible_for_automatic_reconciliation": reason is None,
		}
		if reason is None:
			candidate.update(
				{
					"settlement_builder": "payrexx_integration.payout_reconciliation.build_payout_payment_entry",
					"reconciliation_completed": "payrexx_integration.payout_reconciliation.complete_reconciliation",
				}
			)
		else:
			candidate["reconciliation_reason"] = reason
		candidates.append(candidate)
	return candidates


def build_payout_payment_entry(*, bank_transaction, candidate: dict, amount: Decimal, bank_account: str):
	"""Build one unsaved gross-clearing to net-bank Internal Transfer."""
	evidence = frappe.get_doc(PAYOUT_EVIDENCE_DOCTYPE, candidate["reference_name"], for_update=True)
	reason = _automatic_eligibility_reason(evidence, bank_transaction, amount)
	if reason:
		frappe.throw(reason, frappe.ValidationError)
	settings = frappe.get_doc("Payrexx Settings", evidence.payrexx_settings, for_update=True)
	configuration = _validated_configuration(settings)
	if bank_account != configuration["destination_ledger_account"]:
		frappe.throw(_("EBICS destination ledger account does not match Payrexx payout settings."))

	payment_entry = frappe.new_doc("Payment Entry")
	payment_entry.payment_type = "Internal Transfer"
	payment_entry.company = configuration["company"]
	payment_entry.posting_date = bank_transaction.date
	payment_entry.paid_from = configuration["clearing_account"]
	payment_entry.paid_to = configuration["destination_ledger_account"]
	payment_entry.paid_from_account_currency = evidence.currency
	payment_entry.paid_to_account_currency = evidence.currency
	payment_entry.source_exchange_rate = 1
	payment_entry.target_exchange_rate = 1
	payment_entry.paid_amount = _major_units(evidence.gross_amount)
	payment_entry.received_amount = _major_units(evidence.amount)
	payment_entry.reference_no = evidence.bank_match_reference
	payment_entry.reference_date = bank_transaction.date
	payment_entry.setup_party_account_field()
	payment_entry.set_missing_values()
	payment_entry.source_exchange_rate = 1
	payment_entry.target_exchange_rate = 1
	payment_entry.paid_amount = _major_units(evidence.gross_amount)
	payment_entry.received_amount = _major_units(evidence.amount)
	payment_entry.append(
		"deductions",
		{
			"account": configuration["fee_expense_account"],
			"cost_center": configuration["fee_cost_center"],
			"is_exchange_gain_loss": 1,
			"amount": _major_units(evidence.total_fees),
		},
	)
	payment_entry.set_amounts()
	if len(payment_entry.deductions) != 1:
		frappe.throw(_("Payrexx payout Payment Entry must contain exactly one fee deduction."))
	return {
		"payment_entry": payment_entry,
		"settlement_context": {"payout_evidence": evidence.name},
	}


def complete_reconciliation(*, bank_transaction, candidate: dict, payment_entry, settlement_context) -> None:
	"""Link completed accounting evidence after Good Connector proves allocation."""
	evidence_name = (settlement_context or {}).get("payout_evidence")
	if evidence_name != candidate.get("reference_name"):
		frappe.throw(_("Payrexx payout completion context is invalid."))
	evidence = frappe.get_doc(PAYOUT_EVIDENCE_DOCTYPE, evidence_name, for_update=True)
	if evidence.reconciliation_status == "Reconciled":
		if (
			evidence.bank_transaction == bank_transaction.name
			and evidence.payout_payment_entry == payment_entry.name
		):
			return
		frappe.throw(_("Payrexx payout evidence is already reconciled elsewhere."))
	evidence.db_set(
		{
			"reconciliation_status": "Reconciled",
			"reconciliation_reason": "Exact synthetic EBICS reference fully allocated.",
			"bank_transaction": bank_transaction.name,
			"payout_payment_entry": payment_entry.name,
		},
		update_modified=False,
	)


def on_payment_entry_before_cancel(doc, method: str | None = None) -> None:
	del method
	if not frappe.db.exists("DocType", PAYOUT_EVIDENCE_DOCTYPE):
		return
	if not frappe.get_meta("Payrexx Payout Transfer").has_field("payrexx_payment_entry"):
		return
	component_parents = frappe.get_all(
		"Payrexx Payout Transfer",
		filters={"payrexx_payment_entry": doc.name},
		pluck="parent",
	)
	if component_parents and frappe.db.exists(
		PAYOUT_EVIDENCE_DOCTYPE,
		{"name": ["in", component_parents], "reconciliation_status": "Reconciled"},
	):
		frappe.throw(_("A receipt Payment Entry cannot be cancelled while its payout is reconciled."))
	for evidence_name in frappe.get_all(
		PAYOUT_EVIDENCE_DOCTYPE,
		filters={"payout_payment_entry": doc.name},
		pluck="name",
	):
		_return_to_pending(evidence_name, "Payout Payment Entry was cancelled or unlinked.")


def on_bank_transaction_update_after_submit(doc, method: str | None = None) -> None:
	del method
	if not frappe.db.exists("DocType", PAYOUT_EVIDENCE_DOCTYPE):
		return
	if not frappe.get_meta(PAYOUT_EVIDENCE_DOCTYPE).has_field("bank_transaction"):
		return
	linked_payment_entries = {
		row.payment_entry
		for row in doc.get("payment_entries") or []
		if row.payment_document == "Payment Entry"
	}
	for row in frappe.get_all(
		PAYOUT_EVIDENCE_DOCTYPE,
		filters={"bank_transaction": doc.name},
		fields=["name", "payout_payment_entry"],
	):
		if row.payout_payment_entry not in linked_payment_entries:
			_return_to_pending(row.name, "Bank Transaction allocation was removed.")


def _automatic_eligibility_reason(evidence, bank_transaction, amount: Decimal) -> str | None:
	if evidence.evidence_origin != SYNTHETIC_EVIDENCE_ORIGIN:
		return "Only explicit synthetic acceptance evidence is eligible."
	if evidence.mode != "TEST" or evidence.provider_status != SYNTHETIC_STATUS:
		return "Only synthetic TEST evidence is eligible."
	if evidence.settlement_ready:
		return "Synthetic evidence must not carry provider settlement-ready evidence."
	if evidence.reconciliation_status != "Pending":
		return "Payout evidence is not pending reconciliation."
	if Decimal(str(amount)) != Decimal(evidence.amount) / 100:
		return "EBICS amount does not equal the exact synthetic payout net amount."
	if cstr(bank_transaction.currency).upper() != evidence.currency:
		return "EBICS currency does not equal the synthetic payout currency."
	if getdate(bank_transaction.date) != getdate(evidence.payout_date):
		return "EBICS booking date does not equal the synthetic payout date."
	try:
		settings = frappe.get_doc("Payrexx Settings", evidence.payrexx_settings, for_update=True)
		configuration = _validated_configuration(settings)
	except (frappe.DoesNotExistError, frappe.ValidationError) as error:  # fmt: skip
		return cstr(error)
	if bank_transaction.bank_account != configuration["destination_bank_account"]:
		return "EBICS Bank Account does not equal the configured payout destination."
	if evidence.destination_iban_hash != configuration["destination_iban_hash"]:
		return "Synthetic payout destination identity no longer matches configuration."
	try:
		_validate_evidence_components(evidence, settings, configuration)
	except (frappe.DoesNotExistError, frappe.ValidationError) as error:  # fmt: skip
		return cstr(error)
	return None


def _validated_configuration(settings) -> dict[str, str]:
	if not cint(settings.get("enable_synthetic_payout_acceptance")):
		frappe.throw(_("Synthetic payout acceptance is disabled."))
	if not cint(settings.get("allow_test_transactions")):
		frappe.throw(_("Synthetic payout acceptance requires Allow TEST Transactions."))
	field_values = {
		"clearing_account": settings.get("payout_clearing_account"),
		"destination_bank_account": settings.get("payout_destination_bank_account"),
		"fee_expense_account": settings.get("payout_fee_expense_account"),
		"fee_cost_center": settings.get("payout_fee_cost_center"),
	}
	if not all(field_values.values()):
		frappe.throw(_("Payrexx synthetic payout accounting configuration is incomplete."))
	bank_account = frappe.get_doc("Bank Account", field_values["destination_bank_account"], for_update=True)
	if not bank_account.company or not bank_account.account or not bank_account.iban:
		frappe.throw(_("Payout destination Bank Account requires company, ledger account, and IBAN."))
	iban = _normalize_iban(bank_account.iban)
	company = bank_account.company
	account_currencies = set()
	for account in (
		field_values["clearing_account"],
		bank_account.account,
		field_values["fee_expense_account"],
	):
		row = frappe.db.get_value("Account", account, ["company", "account_currency"], as_dict=True)
		if not row or row.company != company:
			frappe.throw(_("All synthetic payout accounts must belong to the destination company."))
		if row.account_currency not in settings._supported_currencies():
			frappe.throw(_("Synthetic payout account currency is not supported by the Payrexx gateway."))
		account_currencies.add(row.account_currency)
	if len(account_currencies) != 1:
		frappe.throw(_("Synthetic payout clearing, bank, and fee accounts must use one currency."))
	currency = account_currencies.pop()
	if currency != frappe.db.get_value("Company", company, "default_currency"):
		frappe.throw(
			_(
				"Synthetic payout accounts must use the company default currency while exchange rates are fixed at 1."
			)
		)
	if cint(frappe.db.get_value("Currency", currency, "fraction_units")) != 100:
		frappe.throw(_("Synthetic payout accounting supports only two-decimal currencies."))
	if frappe.db.get_value("Cost Center", field_values["fee_cost_center"], "company") != company:
		frappe.throw(_("Synthetic payout fee Cost Center must belong to the destination company."))
	return {
		**field_values,
		"company": company,
		"destination_ledger_account": bank_account.account,
		"destination_iban_hash": _hash_iban(iban),
		"destination_iban_last_four": iban[-4:],
		"currency": currency,
	}


def _validated_component(
	integration_request_name: str,
	settings,
	configuration: dict,
	*,
	allowed_parent: str | None = None,
) -> dict:
	integration_request = frappe.get_doc("Integration Request", integration_request_name, for_update=True)
	data = frappe.parse_json(integration_request.data) or {}
	transaction = data.get("payrexx_transaction") or {}
	invoice = transaction.get("invoice") or {}
	if (
		integration_request.status != "Completed"
		or integration_request.integration_request_service != "Payrexx"
	):
		frappe.throw(_("Synthetic payout sources require completed Payrexx Integration Requests."))
	if data.get("payrexx_settings") != settings.name:
		frappe.throw(_("Synthetic payout source uses different Payrexx settings."))
	if any(data.get(key) for key in ("payrexx_reversals", "payrexx_settlement_conflict")):
		frappe.throw(_("Refunded, disputed, reversed, or conflicted payments are not supported."))
	if (
		cstr(transaction.get("status")).lower() != "confirmed"
		or cstr(transaction.get("mode")).upper() != "TEST"
	):
		frappe.throw(_("Synthetic payout sources require confirmed TEST transactions."))
	if (
		transaction.get("payoutUuid")
		or cstr(transaction.get("type")).strip() not in SUPPORTED_TRANSACTION_CHANNELS
	):
		frappe.throw(_("Synthetic payout sources support ordinary transactions only."))
	transaction_uuid = cstr(transaction.get("uuid")).strip()
	reference_id = cstr(invoice.get("referenceId") or transaction.get("referenceId")).strip()
	if (
		not transaction_uuid
		or transaction_uuid.startswith(SYNTHETIC_PREFIX)
		or reference_id != integration_request.name
	):
		frappe.throw(_("Synthetic payout transaction UUID/reference validation failed."))
	gross_amount = _minor_integer(transaction.get("amount"), "transaction amount")
	fee = _minor_integer(transaction.get("fee"), "transaction fee")
	if gross_amount <= 0 or fee < 0 or fee >= gross_amount:
		frappe.throw(_("Synthetic payout transaction amount/fee is invalid."))
	currency = cstr(invoice.get("currency") or transaction.get("currency")).upper()
	if not currency or currency not in settings._supported_currencies():
		frappe.throw(_("Synthetic payout transaction currency is invalid."))
	if currency != configuration["currency"]:
		frappe.throw(_("Synthetic payout transaction currency does not match configured accounts."))
	if (
		integration_request.reference_doctype != "Payment Request"
		or not integration_request.reference_docname
	):
		frappe.throw(_("Synthetic payout sources require an exact submitted Payment Request receipt chain."))
	payment_request = frappe.get_doc(
		"Payment Request",
		integration_request.reference_docname,
		for_update=True,
	)
	if (
		payment_request.docstatus != 1
		or payment_request.status != "Paid"
		or flt(payment_request.outstanding_amount) != 0
	):
		frappe.throw(_("Synthetic payout Payment Request is not submitted and fully paid."))
	if payment_request.reference_doctype != "Sales Invoice" or not payment_request.reference_name:
		frappe.throw(_("Synthetic payout Payment Request does not identify one Sales Invoice."))
	if payment_request.payment_gateway != f"Payrexx-{settings.name}":
		frappe.throw(_("Synthetic payout Payment Request does not use the owning Payrexx gateway."))
	if payment_request.payment_account != configuration["clearing_account"]:
		frappe.throw(_("Synthetic payout Payment Request does not use the configured clearing account."))
	if (
		cstr(payment_request.currency).upper() != currency
		or _minor_from_major(payment_request.grand_total) != gross_amount
	):
		frappe.throw(
			_("Synthetic payout Payment Request currency or amount does not match provider evidence.")
		)
	sales_invoice = frappe.get_doc("Sales Invoice", payment_request.reference_name, for_update=True)
	if (
		sales_invoice.docstatus != 1
		or sales_invoice.company != configuration["company"]
		or cstr(sales_invoice.currency).upper() != currency
	):
		frappe.throw(_("Synthetic payout Payment Request source is not an exact submitted Sales Invoice."))
	payment_entry_name = cstr(data.get("payrexx_payment_entry")).strip()
	if not payment_entry_name:
		frappe.throw(_("Completed Payrexx Integration Request has no recorded Payment Entry."))
	payment_entry = frappe.get_doc("Payment Entry", payment_entry_name, for_update=True)
	if (
		payment_entry.docstatus != 1
		or payment_entry.payment_type != "Receive"
		or payment_entry.company != configuration["company"]
		or payment_entry.paid_to != configuration["clearing_account"]
		or payment_entry.paid_from_account_currency != currency
		or payment_entry.paid_to_account_currency != currency
		or _minor_from_major(payment_entry.paid_amount) != gross_amount
		or _minor_from_major(payment_entry.received_amount) != gross_amount
		or _minor_from_major(payment_entry.total_allocated_amount) != gross_amount
		or _minor_from_major(payment_entry.unallocated_amount) != 0
	):
		frappe.throw(_("Recorded Payrexx receipt Payment Entry is not an exact submitted allocated receipt."))
	if (
		payment_entry.reference_no != payment_request.name
		or not payment_entry.references
		or any(
			row.reference_doctype != payment_request.reference_doctype
			or row.reference_name != payment_request.reference_name
			or row.payment_request != payment_request.name
			for row in payment_entry.references
		)
		or sum(_minor_from_major(row.allocated_amount) for row in payment_entry.references) != gross_amount
	):
		frappe.throw(
			_("Recorded Payrexx Payment Entry does not belong to the exact Payment Request receipt chain.")
		)
	_claim_check(payment_entry.name, allowed_parent=allowed_parent)
	return {
		"integration_request": integration_request.name,
		"payment_entry": payment_entry.name,
		"company": payment_entry.company,
		"currency": currency,
		"gross_amount": gross_amount,
		"fee": fee,
		"transaction_uuid": transaction_uuid,
		"transaction_time": _required_transaction_time(transaction.get("time")),
		"posting_date": getdate(payment_entry.posting_date),
		"payment_brand": cstr((transaction.get("payment") or {}).get("brand")),
	}


def _validate_evidence_components(evidence, settings, configuration: dict) -> None:
	transaction_transfers = [row for row in evidence.transfers if row.transfer_type == "transaction"]
	payout_fee_transfers = [row for row in evidence.transfers if row.transfer_type == "payout-fee"]
	if not transaction_transfers or len(transaction_transfers) + len(payout_fee_transfers) != len(
		evidence.transfers
	):
		frappe.throw(_("Synthetic payout composition contains unsupported transfers."))
	if len(payout_fee_transfers) > 1:
		frappe.throw(_("Synthetic payout composition contains multiple payout fees."))
	if any(row.item_type not in SUPPORTED_ITEM_TYPES for row in evidence.items):
		frappe.throw(_("Synthetic payout composition contains unsupported items."))
	components = [
		_validated_component(
			row.integration_request,
			settings,
			configuration,
			allowed_parent=evidence.name,
		)
		for row in transaction_transfers
	]
	for row, component in zip(transaction_transfers, components, strict=True):
		if (
			row.payrexx_payment_entry != component["payment_entry"]
			or row.transaction_uuid != component["transaction_uuid"]
			or row.reference_id != component["integration_request"]
			or row.transaction_amount != component["gross_amount"]
			or row.transaction_fee != component["fee"]
			or row.transaction_currency != component["currency"]
		):
			frappe.throw(_("Synthetic payout component evidence no longer matches its receipt chain."))
	payout_fee = -payout_fee_transfers[0].amount if payout_fee_transfers else 0
	gross = sum(component["gross_amount"] for component in components)
	fees = sum(component["fee"] for component in components) + payout_fee
	if evidence.gross_amount != gross or evidence.total_fees != fees or evidence.amount != gross - fees:
		frappe.throw(_("Synthetic payout gross/fee/net arithmetic is inconsistent."))


def _claim_check(payment_entry_name: str, allowed_parent: str | None) -> None:
	transfer = frappe.qb.DocType("Payrexx Payout Transfer")
	parents = (
		frappe.qb.from_(transfer)
		.select(transfer.parent)
		.where(transfer.payrexx_payment_entry == payment_entry_name)
		.for_update()
	).run(pluck=True)
	if any(parent != allowed_parent for parent in parents):
		frappe.throw(_("A Payrexx receipt Payment Entry is already claimed by another payout."))


def _locked_evidence(evidence_key: str):
	if not frappe.db.exists(PAYOUT_EVIDENCE_DOCTYPE, evidence_key):
		return None
	return frappe.get_doc(PAYOUT_EVIDENCE_DOCTYPE, evidence_key, for_update=True)


def _validate_deterministic_replay(
	evidence, integration_request_names: list[str], bank_reference: str
) -> None:
	if (
		evidence.evidence_origin != SYNTHETIC_EVIDENCE_ORIGIN
		or evidence.mode != "TEST"
		or evidence.provider_status != SYNTHETIC_STATUS
		or evidence.bank_match_reference != bank_reference
		or sorted(row.integration_request for row in evidence.transfers if row.integration_request)
		!= integration_request_names
	):
		frappe.throw(_("Synthetic payout deterministic replay conflicts with stored evidence."))


def _return_to_pending(evidence_name: str, reason: str) -> None:
	frappe.db.set_value(
		PAYOUT_EVIDENCE_DOCTYPE,
		evidence_name,
		{
			"reconciliation_status": "Pending",
			"reconciliation_reason": reason,
			"bank_transaction": None,
			"payout_payment_entry": None,
		},
		update_modified=False,
	)


def _minor_integer(value, label: str) -> int:
	if isinstance(value, bool) or not isinstance(value, int):
		frappe.throw(_("Synthetic payout {0} must be an integer in minor units.").format(label))
	return value


def _minor_from_major(value) -> int:
	return int((Decimal(str(value)) * 100).quantize(Decimal("1")))


def _major_units(value: int) -> float:
	return flt(Decimal(value) / 100, 2)


def _required_transaction_time(value):
	if not value:
		frappe.throw(_("Synthetic payout transaction time is required."))
	date_time = get_datetime(value)
	if date_time.tzinfo is None:
		frappe.throw(_("Synthetic payout transaction time requires a timezone."))
	return date_time.astimezone(UTC).replace(tzinfo=None)


def _synthetic_result(name: str, *, created: bool) -> dict[str, bool | str]:
	return {
		"ok": True,
		"payout_evidence": name,
		"status": SYNTHETIC_STATUS,
		"created": created,
		"status_changed": False,
	}
