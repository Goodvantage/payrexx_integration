from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import flt, now_datetime, nowdate

from payrexx_integration.payout_reconciliation import (
	SYNTHETIC_CONFIRMATION,
	_claim_check,
	_validated_configuration,
	build_payout_payment_entry,
	complete_reconciliation,
	create_synthetic_acceptance_evidence,
	get_reconciliation_candidates,
	on_payment_entry_before_cancel,
)
from payrexx_integration.payrexx_integration.doctype.payrexx_settings.test_payrexx_settings import (
	_create_test_user,
	_ensure_settings,
	_ensure_test_customer,
	_ensure_test_item,
	_test_company,
	_test_payment_account,
)


class TestPayoutReconciliationSetup(UnitTestCase):
	def test_setup_hooks_and_custom_field_contract(self):
		from payrexx_integration import hooks, setup

		self.assertEqual(hooks.after_install, "payrexx_integration.setup.ensure_payout_reconciliation_fields")
		self.assertEqual(hooks.after_migrate, [hooks.after_install])
		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch(
				"frappe.custom.doctype.custom_field.custom_field.create_custom_fields"
			) as create_custom_fields,
			patch.object(frappe, "clear_cache") as clear_cache,
		):
			setup.ensure_payout_reconciliation_fields()

		custom_fields = create_custom_fields.call_args.args[0]
		self.assertEqual(
			{field["fieldname"] for field in custom_fields["Payrexx Settings"]},
			{
				"payout_reconciliation_section",
				"enable_synthetic_payout_acceptance",
				"payout_clearing_account",
				"payout_destination_bank_account",
				"payout_fee_expense_account",
				"payout_fee_cost_center",
			},
		)
		receipt_field = next(
			field
			for field in custom_fields["Payrexx Payout Transfer"]
			if field["fieldname"] == "payrexx_payment_entry"
		)
		self.assertEqual(
			(receipt_field["fieldtype"], receipt_field["options"], receipt_field["unique"]),
			("Link", "Payment Entry", 1),
		)
		self.assertEqual({call.kwargs["doctype"] for call in clear_cache.call_args_list}, set(custom_fields))

	def test_setup_is_inert_until_every_optional_accounting_doctype_exists(self):
		from payrexx_integration import setup

		with (
			patch.object(frappe.db, "exists", side_effect=lambda _doctype, name: name != "Bank Transaction"),
			patch(
				"frappe.custom.doctype.custom_field.custom_field.create_custom_fields"
			) as create_custom_fields,
		):
			setup.ensure_payout_reconciliation_fields()

		create_custom_fields.assert_not_called()


class TestPayoutReconciliationSchema(IntegrationTestCase):
	def test_migrated_schema_contains_reconciliation_links_and_unique_receipt_owner(self):
		settings_meta = frappe.get_meta("Payrexx Settings")
		self.assertEqual(settings_meta.get_field("payout_clearing_account").options, "Account")
		self.assertEqual(
			settings_meta.get_field("payout_destination_bank_account").options,
			"Bank Account",
		)

		evidence_meta = frappe.get_meta("Payrexx Payout Evidence")
		self.assertEqual(evidence_meta.get_field("bank_transaction").options, "Bank Transaction")
		self.assertEqual(evidence_meta.get_field("payout_payment_entry").options, "Payment Entry")
		self.assertEqual(evidence_meta.get_field("transfers").options, "Payrexx Payout Transfer")
		self.assertEqual(evidence_meta.get_field("items").options, "Payrexx Payout Item")

		receipt_field = frappe.get_meta("Payrexx Payout Transfer").get_field("payrexx_payment_entry")
		self.assertEqual(receipt_field.options, "Payment Entry")
		self.assertEqual(receipt_field.unique, 1)


class TestSyntheticPayoutReconciliation(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.company = _test_company()
		self.currency = frappe.db.get_value("Company", self.company, "default_currency")
		self.clearing_account = _test_payment_account(self.company)
		self.destination_account = self._account("Bank", parent_of=self.clearing_account)
		self.fee_account = self._expense_account()
		self.cost_center = frappe.db.get_value(
			"Cost Center",
			{"company": self.company, "is_group": 0, "disabled": 0},
			"name",
		)
		self.bank_account = self._bank_account()
		self.automation_user = _create_test_user()
		self.settings_name = _ensure_settings(
			f"Synthetic-{frappe.generate_hash(length=8)}",
			automation_user=self.automation_user,
		)
		frappe.db.set_value(
			"Payrexx Settings",
			self.settings_name,
			{
				"supported_currencies": self.currency,
				"allow_test_transactions": 1,
				"enable_synthetic_payout_acceptance": 1,
				"payout_clearing_account": self.clearing_account,
				"payout_destination_bank_account": self.bank_account,
				"payout_fee_expense_account": self.fee_account,
				"payout_fee_cost_center": self.cost_center,
			},
			update_modified=False,
		)
		frappe.clear_document_cache("Payrexx Settings", self.settings_name)

	def _account(self, account_type: str, *, parent_of: str) -> str:
		parent = frappe.db.get_value("Account", parent_of, "parent_account")
		return (
			frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": f"Payrexx Synthetic {account_type} {frappe.generate_hash(length=8)}",
					"company": self.company,
					"parent_account": parent,
					"account_type": account_type,
					"account_currency": self.currency,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _expense_account(self) -> str:
		account = frappe.db.get_value(
			"Account",
			{
				"company": self.company,
				"root_type": "Expense",
				"is_group": 0,
				"disabled": 0,
				"account_currency": self.currency,
			},
			"name",
		)
		if account:
			return account
		parent = frappe.db.get_value(
			"Account",
			{"company": self.company, "root_type": "Expense", "is_group": 1},
			"name",
		)
		return (
			frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": f"Payrexx Synthetic Fees {frappe.generate_hash(length=8)}",
					"company": self.company,
					"parent_account": parent,
					"account_currency": self.currency,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _bank_account(self) -> str:
		bank = frappe.db.get_value("Bank", {}, "name")
		if not bank:
			bank = (
				frappe.get_doc({"doctype": "Bank", "bank_name": "Payrexx Synthetic Test Bank"})
				.insert(ignore_permissions=True)
				.name
			)
		return (
			frappe.get_doc(
				{
					"doctype": "Bank Account",
					"account_name": f"Payrexx Synthetic {frappe.generate_hash(length=8)}",
					"bank": bank,
					"is_company_account": 1,
					"company": self.company,
					"account": self.destination_account,
					"iban": "CH9300762011623852957",
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _receipt_chain(self, amount: int, fee: int) -> tuple[str, str]:
		invoice = frappe.new_doc("Sales Invoice")
		invoice.company = self.company
		invoice.customer = _ensure_test_customer()
		invoice.currency = self.currency
		invoice.conversion_rate = 1
		invoice.append("items", {"item_code": _ensure_test_item(), "qty": 1, "rate": amount / 100})
		invoice.insert(ignore_permissions=True)
		invoice.submit()

		from erpnext.accounts.doctype.payment_request.payment_request import (
			PaymentRequest,
			make_payment_request,
		)

		payment_gateway = f"Payrexx-{self.settings_name}"
		gateway_account = frappe.db.get_value(
			"Payment Gateway Account",
			{
				"payment_gateway": payment_gateway,
				"currency": self.currency,
				"company": self.company,
			},
			"name",
		)
		if not gateway_account:
			gateway_account = (
				frappe.get_doc(
					{
						"doctype": "Payment Gateway Account",
						"payment_gateway": payment_gateway,
						"payment_account": self.clearing_account,
						"currency": self.currency,
						"company": self.company,
					}
				)
				.insert(ignore_permissions=True)
				.name
			)
		else:
			frappe.db.set_value(
				"Payment Gateway Account",
				gateway_account,
				"payment_account",
				self.clearing_account,
				update_modified=False,
			)
		with patch.object(PaymentRequest, "get_payment_url", return_value="https://pay.example/test"):
			payment_request = make_payment_request(
				dt="Sales Invoice",
				dn=invoice.name,
				recipient_id="payrexx-payout@example.test",
				mute_email=1,
				payment_gateway_account=gateway_account,
				submit_doc=1,
				return_doc=1,
			)
		self.assertEqual(payment_request.payment_gateway, payment_gateway)
		self.assertEqual(payment_request.payment_account, self.clearing_account)
		integration_request = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"reference_doctype": "Payment Request",
				"reference_docname": payment_request.name,
				"data": frappe.as_json({"payrexx_settings": self.settings_name}),
			}
		).insert(ignore_permissions=True)
		payment_entry = payment_request.set_as_paid()
		transaction = {
			"uuid": frappe.generate_hash(length=8),
			"status": "confirmed",
			"mode": "TEST",
			"type": "E-Commerce",
			"amount": amount,
			"fee": fee,
			"currency": self.currency,
			"time": f"{nowdate()}T10:00:00+02:00",
			"payment": {"brand": "visa"},
			"referenceId": integration_request.name,
			"invoice": {
				"referenceId": integration_request.name,
				"currency": self.currency,
				"test": 1,
			},
		}
		integration_request.db_set(
			{
				"status": "Completed",
				"data": frappe.as_json(
					{
						"payrexx_settings": self.settings_name,
						"payrexx_payment_entry": payment_entry.name,
						"payrexx_transaction": transaction,
					}
				),
			},
			update_modified=False,
		)
		return integration_request.name, payment_entry.name

	def _evidence(self):
		first, first_payment = self._receipt_chain(10000, 150)
		second, second_payment = self._receipt_chain(12000, 176)
		result = create_synthetic_acceptance_evidence(
			self.settings_name,
			[first, second],
			SYNTHETIC_CONFIRMATION,
		)
		return frappe.get_doc("Payrexx Payout Evidence", result["payout_evidence"]), (
			first_payment,
			second_payment,
		)

	def _bank_transaction(self, evidence, *, amount: float | None = None, reference: str | None = None):
		return frappe._dict(
			name="BANK-SYNTHETIC",
			date=evidence.payout_date,
			currency=evidence.currency,
			bank_account=self.bank_account,
			deposit=amount if amount is not None else evidence.amount / 100,
			transaction_id=reference or evidence.bank_match_reference,
		)

	def test_creation_is_test_only_deterministic_and_uses_exact_receipt_evidence(self):
		evidence, component_payments = self._evidence()
		self.assertEqual(evidence.evidence_origin, "Synthetic Acceptance")
		self.assertEqual(evidence.provider_status, "synthetic")
		self.assertEqual(evidence.mode, "TEST")
		self.assertFalse(evidence.settlement_ready)
		self.assertEqual((evidence.gross_amount, evidence.total_fees, evidence.amount), (22000, 326, 21674))
		self.assertTrue(evidence.payout_uuid.startswith("SYNTHETIC-"))
		self.assertTrue(evidence.bank_match_reference.startswith("SYNTHETIC-PAYOUT-"))
		self.assertEqual(
			sorted(row.payrexx_payment_entry for row in evidence.transfers),
			sorted(component_payments),
		)
		replay = create_synthetic_acceptance_evidence(
			self.settings_name,
			[row.integration_request for row in evidence.transfers],
			SYNTHETIC_CONFIRMATION,
		)
		self.assertFalse(replay["created"])
		self.assertEqual(replay["payout_evidence"], evidence.name)

	def test_gate_confirmation_and_optional_payout_fee_are_explicit(self):
		first, _first_payment = self._receipt_chain(10000, 150)
		with self.assertRaisesRegex(frappe.ValidationError, "confirmation text"):
			create_synthetic_acceptance_evidence(self.settings_name, [first], "wrong")
		frappe.db.set_value(
			"Payrexx Settings",
			self.settings_name,
			"enable_synthetic_payout_acceptance",
			0,
			update_modified=False,
		)
		with self.assertRaisesRegex(frappe.ValidationError, "disabled"):
			create_synthetic_acceptance_evidence(
				self.settings_name,
				[first],
				SYNTHETIC_CONFIRMATION,
			)
		frappe.db.set_value(
			"Payrexx Settings",
			self.settings_name,
			"enable_synthetic_payout_acceptance",
			1,
			update_modified=False,
		)
		result = create_synthetic_acceptance_evidence(
			self.settings_name,
			[first],
			SYNTHETIC_CONFIRMATION,
			payout_fee_minor=10,
		)
		evidence = frappe.get_doc("Payrexx Payout Evidence", result["payout_evidence"])
		self.assertEqual((evidence.gross_amount, evidence.total_fees, evidence.amount), (10000, 160, 9840))
		self.assertEqual([row.transfer_type for row in evidence.transfers], ["transaction", "payout-fee"])

	def test_unbound_integration_request_is_not_an_exact_receipt_chain(self):
		integration_request, _payment_entry = self._receipt_chain(10000, 150)
		frappe.db.set_value(
			"Integration Request",
			integration_request,
			{"reference_doctype": None, "reference_docname": None},
			update_modified=False,
		)

		with self.assertRaisesRegex(frappe.ValidationError, "Payment Request"):
			create_synthetic_acceptance_evidence(
				self.settings_name,
				[integration_request],
				SYNTHETIC_CONFIRMATION,
			)

	def test_recorded_payment_entry_must_belong_to_the_same_payment_request(self):
		integration_request, _payment_entry = self._receipt_chain(10000, 150)
		_other_request, other_payment_entry = self._receipt_chain(10000, 150)
		data = frappe.parse_json(frappe.db.get_value("Integration Request", integration_request, "data"))
		data["payrexx_payment_entry"] = other_payment_entry
		frappe.db.set_value(
			"Integration Request",
			integration_request,
			"data",
			frappe.as_json(data),
			update_modified=False,
		)

		with self.assertRaisesRegex(frappe.ValidationError, "exact Payment Request receipt chain"):
			create_synthetic_acceptance_evidence(
				self.settings_name,
				[integration_request],
				SYNTHETIC_CONFIRMATION,
			)

	def test_foreign_currency_configuration_is_rejected_before_exchange_rate_one(self):
		foreign_currency = "EUR" if self.currency != "EUR" else "USD"
		accounts = (self.clearing_account, self.destination_account, self.fee_account)
		try:
			for account in accounts:
				frappe.db.set_value(
					"Account",
					account,
					"account_currency",
					foreign_currency,
					update_modified=False,
				)
			frappe.db.set_value(
				"Payrexx Settings",
				self.settings_name,
				"supported_currencies",
				foreign_currency,
				update_modified=False,
			)
			frappe.clear_document_cache("Payrexx Settings", self.settings_name)

			with self.assertRaisesRegex(frappe.ValidationError, "company default currency"):
				_validated_configuration(frappe.get_doc("Payrexx Settings", self.settings_name))
		finally:
			for account in accounts:
				frappe.db.set_value(
					"Account",
					account,
					"account_currency",
					self.currency,
					update_modified=False,
				)
			frappe.db.set_value(
				"Payrexx Settings",
				self.settings_name,
				"supported_currencies",
				self.currency,
				update_modified=False,
			)
			frappe.clear_document_cache("Payrexx Settings", self.settings_name)

	def test_exact_reference_matches_but_amount_only_and_secondary_failures_do_not(self):
		evidence, _component_payments = self._evidence()
		bank_transaction = self._bank_transaction(evidence)
		self.assertEqual(
			get_reconciliation_candidates(
				bank_transaction=bank_transaction,
				bank_reference="different",
				amount=Decimal("216.74"),
			),
			[],
		)
		exact = get_reconciliation_candidates(
			bank_transaction=bank_transaction,
			bank_reference=evidence.bank_match_reference,
			amount=Decimal("216.74"),
		)
		self.assertEqual(len(exact), 1)
		self.assertTrue(exact[0]["eligible_for_automatic_reconciliation"])
		wrong_amount = get_reconciliation_candidates(
			bank_transaction=bank_transaction,
			bank_reference=evidence.bank_match_reference,
			amount=Decimal("220.00"),
		)
		self.assertFalse(wrong_amount[0]["eligible_for_automatic_reconciliation"])

	def test_builder_posts_exact_gross_net_and_single_fee_row(self):
		evidence, _component_payments = self._evidence()
		bank_transaction = self._bank_transaction(evidence)
		candidate = get_reconciliation_candidates(
			bank_transaction=bank_transaction,
			bank_reference=evidence.bank_match_reference,
			amount=Decimal("216.74"),
		)[0]
		result = build_payout_payment_entry(
			bank_transaction=bank_transaction,
			candidate=candidate,
			amount=Decimal("216.74"),
			bank_account=self.destination_account,
		)
		payment_entry = result["payment_entry"]
		self.assertTrue(payment_entry.is_new())
		self.assertEqual(payment_entry.payment_type, "Internal Transfer")
		self.assertEqual((payment_entry.paid_amount, payment_entry.received_amount), (220, 216.74))
		self.assertEqual(len(payment_entry.deductions), 1)
		self.assertTrue(payment_entry.deductions[0].is_exchange_gain_loss)
		self.assertEqual(payment_entry.deductions[0].account, self.fee_account)
		self.assertEqual(flt(payment_entry.deductions[0].amount, 2), 3.26)
		payment_entry.insert(ignore_permissions=True)
		payment_entry.submit()
		gl_by_account = {
			row.account: (flt(row.debit, 2), flt(row.credit, 2))
			for row in frappe.get_all(
				"GL Entry",
				filters={"voucher_type": "Payment Entry", "voucher_no": payment_entry.name},
				fields=["account", "debit", "credit"],
			)
		}
		self.assertEqual(gl_by_account[self.destination_account], (216.74, 0))
		self.assertEqual(gl_by_account[self.fee_account], (3.26, 0))
		self.assertEqual(gl_by_account[self.clearing_account], (0, 220))

	def test_good_connector_bridge_reconciles_once_with_real_documents(self):
		if "good_connector" not in frappe.get_installed_apps():
			self.skipTest("Good Connector is an optional cross-app test dependency")
		evidence, _component_payments = self._evidence()
		bank_transaction = frappe.get_doc(
			{
				"doctype": "Bank Transaction",
				"date": evidence.payout_date,
				"bank_account": self.bank_account,
				"currency": evidence.currency,
				"deposit": evidence.amount / 100,
				"withdrawal": 0,
				"description": "Synthetic payout acceptance",
				"reference_number": evidence.bank_match_reference,
				"transaction_id": frappe.generate_hash(length=32),
				"gc_ebics_bank_reference": evidence.bank_match_reference,
				"gc_ebics_reconciliation_status": "Pending",
			}
		).insert(ignore_permissions=True)
		bank_transaction.submit()

		from good_connector.bank_integration import reconcile_bank_transaction

		reconcile_bank_transaction(bank_transaction.name)
		bank_transaction.reload()
		evidence.reload()
		self.assertEqual(bank_transaction.gc_ebics_reconciliation_status, "Reconciled")
		self.assertEqual(evidence.reconciliation_status, "Reconciled")
		self.assertEqual(evidence.bank_transaction, bank_transaction.name)
		self.assertEqual(evidence.payout_payment_entry, bank_transaction.gc_ebics_payment_entry)
		self.assertEqual(len(bank_transaction.payment_entries), 1)

		reconcile_bank_transaction(bank_transaction.name)
		bank_transaction.reload()
		self.assertEqual(len(bank_transaction.payment_entries), 1)
		self.assertEqual(
			frappe.db.count("Payment Entry", {"name": evidence.payout_payment_entry, "docstatus": 1}),
			1,
		)
		frappe.get_doc("Payment Entry", evidence.payout_payment_entry).cancel()
		evidence.reload()
		bank_transaction.reload()
		self.assertEqual(evidence.reconciliation_status, "Pending")
		self.assertFalse(evidence.payout_payment_entry)
		self.assertEqual(bank_transaction.gc_ebics_reconciliation_status, "Review")
		self.assertFalse(bank_transaction.gc_ebics_payment_entry)

	def test_completion_replay_and_cancellation_handlers_are_safe(self):
		evidence, component_payments = self._evidence()
		bank_transaction = self._bank_transaction(evidence)
		candidate = {"reference_name": evidence.name}
		payment_entry = frappe._dict(name="ACC-PAY-SYNTHETIC")
		context = {"payout_evidence": evidence.name}
		complete_reconciliation(
			bank_transaction=bank_transaction,
			candidate=candidate,
			payment_entry=payment_entry,
			settlement_context=context,
		)
		complete_reconciliation(
			bank_transaction=bank_transaction,
			candidate=candidate,
			payment_entry=payment_entry,
			settlement_context=context,
		)
		evidence.reload()
		self.assertEqual(evidence.reconciliation_status, "Reconciled")
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be cancelled"):
			on_payment_entry_before_cancel(frappe._dict(name=component_payments[0]))
		on_payment_entry_before_cancel(payment_entry)
		evidence.reload()
		self.assertEqual(evidence.reconciliation_status, "Pending")
		self.assertFalse(evidence.bank_transaction)
		self.assertFalse(evidence.payout_payment_entry)

	def test_bad_component_and_unsupported_reversal_are_rejected(self):
		integration_request, payment_entry = self._receipt_chain(10000, 150)
		data = frappe.parse_json(frappe.db.get_value("Integration Request", integration_request, "data"))
		data["payrexx_reversals"] = [{"status": "refunded"}]
		frappe.db.set_value("Integration Request", integration_request, "data", frappe.as_json(data))
		with self.assertRaisesRegex(frappe.ValidationError, "not supported"):
			create_synthetic_acceptance_evidence(
				self.settings_name,
				[integration_request],
				SYNTHETIC_CONFIRMATION,
			)
		data.pop("payrexx_reversals")
		data["payrexx_payment_entry"] = "missing-payment-entry"
		frappe.db.set_value("Integration Request", integration_request, "data", frappe.as_json(data))
		with self.assertRaises(frappe.DoesNotExistError):
			create_synthetic_acceptance_evidence(
				self.settings_name,
				[integration_request],
				SYNTHETIC_CONFIRMATION,
			)
		self.assertEqual(frappe.db.get_value("Payment Entry", payment_entry, "docstatus"), 1)


class TestSyntheticPayoutClaimConcurrency(IntegrationTestCase):
	def test_claim_check_observes_a_competing_claim_after_a_stale_snapshot(self):
		payment_entry_name = f"PAYREXX-CLAIM-{frappe.generate_hash(length=10)}"
		claim_parent = f"PAYREXX-PAYOUT-{frappe.generate_hash(length=10)}"
		claim_row = f"PAYREXX-TRANSFER-{frappe.generate_hash(length=10)}"

		with self.primary_connection():
			self.assertEqual(
				frappe.get_all(
					"Payrexx Payout Transfer",
					filters={"payrexx_payment_entry": payment_entry_name},
					pluck="parent",
				),
				[],
			)

		try:
			with self.secondary_connection():
				frappe.get_doc(
					{
						"doctype": "Payrexx Payout Transfer",
						"name": claim_row,
						"parent": claim_parent,
						"parenttype": "Payrexx Payout Evidence",
						"parentfield": "transfers",
						"idx": 1,
						"transfer_index": 1,
						"transfer_type": "transaction",
						"amount": 100,
						"date_time": now_datetime(),
						"payrexx_payment_entry": payment_entry_name,
					}
				).db_insert()
				frappe.db.commit()  # Publish the competing claim to the stale transaction. # nosemgrep

			with self.primary_connection():
				with self.assertRaises((frappe.ValidationError, frappe.QueryDeadlockError)) as raised:
					_claim_check(payment_entry_name, allowed_parent="ANOTHER-PAYOUT")
				if isinstance(raised.exception, frappe.QueryDeadlockError):
					frappe.db.rollback()
					with self.assertRaisesRegex(frappe.ValidationError, "already claimed"):
						_claim_check(payment_entry_name, allowed_parent="ANOTHER-PAYOUT")
		finally:
			with self.primary_connection():
				frappe.db.rollback()
			with self.secondary_connection():
				frappe.db.delete("Payrexx Payout Transfer", {"name": claim_row})
				frappe.db.commit()  # Persist cross-connection test cleanup. # nosemgrep
