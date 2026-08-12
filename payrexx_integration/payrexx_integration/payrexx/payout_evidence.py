# Copyright (c) 2026, Goodvantage GmbH and contributors

"""Validated, privacy-minimized evidence for signed Payrexx payout webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from copy import deepcopy
from datetime import UTC, datetime

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

PAYOUT_EVIDENCE_DOCTYPE = "Payrexx Payout Evidence"
SIGNED_EVIDENCE_ORIGIN = "Signed Provider Webhook"
SYNTHETIC_EVIDENCE_ORIGIN = "Synthetic Acceptance"
SYNTHETIC_PREFIX = "SYNTHETIC-"
PAYOUT_STATUSES = frozenset({"initiated", "pending", "under-review", "processing", "sent", "failed"})
PAYOUT_MODES = frozenset({"TEST", "LIVE"})
PAYOUT_PAYERS = frozenset({"stripe", "payrexx", "unknown"})
TRANSFER_TYPES = frozenset(
	{
		"transaction",
		"transaction-reversal",
		"dispute",
		"dispute-reversal",
		"payout",
		"payout-reversal",
		"adjustment",
		"manual-adjustment",
		"payout-fee",
		"payout-reserve",
		"payout-reserve-reversal",
		"alternative-currency-payout-fee-percent",
	}
)
ITEM_TYPES = frozenset(
	{
		"transaction",
		"transaction-reversal",
		"transaction-fee",
		"transaction-fee-reversal",
		"application-fee",
		"application-fee-reversal",
		"transaction-currency-conversion-fee",
		"transaction-currency-conversion-fee-reversal",
		"dispute",
		"dispute-reversal",
		"dispute-fee",
		"dispute-fee-reversal",
		"dispute-currency-conversion-fee",
		"dispute-currency-conversion-fee-reversal",
		"payout",
		"payout-reversal",
		"adjustment",
		"manual-adjustment",
		"payout-fee",
		"payout-reserve",
		"payout-reserve-reversal",
	}
)
TRANSACTION_TYPES = frozenset({"transaction", "transaction-reversal"})
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_IBAN_PATTERN = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")


def capture_payout_evidence(settings_name: str, payout: dict) -> dict[str, bool | str]:
	"""Insert or monotonically advance one authenticated payout observation."""
	normalized = normalize_payout(payout)
	evidence_key = _evidence_key(settings_name, normalized["mode"], normalized["payout_uuid"])
	existing = _locked_evidence(evidence_key)
	if existing:
		return _apply_replay(existing, normalized)

	document = frappe.get_doc(
		{
			"doctype": PAYOUT_EVIDENCE_DOCTYPE,
			"evidence_key": evidence_key,
			"payrexx_settings": settings_name,
			"payout_uuid": normalized["payout_uuid"],
			"mode": normalized["mode"],
			"amount": normalized["amount"],
			"gross_amount": normalized["gross_amount"],
			"total_fees": normalized["total_fees"],
			"currency": normalized["currency"],
			"payout_date": normalized["payout_date"],
			"statement": normalized["statement"],
			"payer": normalized["payer"],
			"provider_status": normalized["status"],
			"evidence_origin": SIGNED_EVIDENCE_ORIGIN,
			"reconciliation_status": "Review",
			"reconciliation_reason": "Signed TEST/LIVE payout evidence is not eligible in synthetic acceptance V1.",
			"settlement_ready": normalized["status"] == "sent",
			"is_manual_payout": normalized["is_manual_payout"],
			"destination_type": normalized["destination_type"],
			"destination_iban_hash": normalized["destination_iban_hash"],
			"destination_iban_last_four": normalized["destination_iban_last_four"],
			"composition_hash": normalized["composition_hash"],
			"received_on": now_datetime(),
			"status_updated_on": now_datetime(),
		}
	)
	for transfer in normalized["transfers"]:
		document.append("transfers", transfer)
	for item in normalized["items"]:
		document.append("items", item)
	try:
		document.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		# The deterministic document name is the concurrency boundary. A second
		# request may have inserted it after our existence check.
		existing = _locked_evidence(evidence_key)
		if not existing:
			raise
		return _apply_replay(existing, normalized)

	return _result(document.name, normalized["status"], created=True, status_changed=False)


def normalize_payout(payout: dict) -> dict:
	if not isinstance(payout, dict) or payout.get("object") != "payout":
		_invalid(_("Payrexx payout evidence must be a bare payout object."))

	payout_uuid = _required_text(payout, "uuid")
	if payout_uuid.startswith(SYNTHETIC_PREFIX):
		_invalid(_("Signed Payrexx payout UUIDs must not use the reserved SYNTHETIC- prefix."))
	if not 8 <= len(payout_uuid) <= 40:
		_invalid(_("Payrexx payout UUID must contain 8 to 40 characters."))
	mode = _choice(payout, "mode", PAYOUT_MODES)
	amount = _required_integer(payout, "amount")
	total_fees = _required_integer(payout, "total_fees")
	currency = _required_text(payout, "currency")
	if not _CURRENCY_PATTERN.fullmatch(currency):
		_invalid(_("Payrexx payout currency must be an uppercase ISO 4217 code."))
	payout_date = _date(payout, "date")
	statement = _required_text(payout, "statement", allow_empty=True)
	payer = _choice(payout, "payer", PAYOUT_PAYERS)
	status = _choice(payout, "status", PAYOUT_STATUSES)
	if status == "synthetic":
		_invalid(_("Signed Payrexx payout status must not use the reserved synthetic value."))
	is_manual_payout = payout.get("is_manual_payout")
	if not isinstance(is_manual_payout, bool):
		_invalid(_("Payrexx payout is_manual_payout must be a boolean."))

	destination = _required_dict(payout, "destination")
	if _required_text(destination, "type") != "bank_account":
		_invalid(_("Payrexx payout destination type must be bank_account."))
	iban = _normalize_iban(_required_text(destination, "iban"))
	_required_text(destination, "account_holder", allow_empty=True)
	_validate_merchant(_required_dict(payout, "merchant"))

	provider_transfers = payout.get("transfers")
	if not isinstance(provider_transfers, list):
		_invalid(_("Payrexx payout transfers must be an array."))
	transfers = []
	items = []
	for transfer_index, provider_transfer in enumerate(provider_transfers, start=1):
		transfer, transfer_items = _normalize_transfer(provider_transfer, transfer_index)
		transfers.append(transfer)
		items.extend(transfer_items)
	if sum(transfer["amount"] for transfer in transfers) != amount:
		_invalid(_("Payrexx payout amount does not equal the sum of transfer amounts."))

	normalized = {
		"payout_uuid": payout_uuid,
		"mode": mode,
		"amount": amount,
		"gross_amount": amount + total_fees,
		"total_fees": total_fees,
		"currency": currency,
		"payout_date": payout_date,
		"statement": statement,
		"payer": payer,
		"status": status,
		"is_manual_payout": is_manual_payout,
		"destination_type": "bank_account",
		"destination_iban_hash": _hash_iban(iban),
		"destination_iban_last_four": iban[-4:],
		"transfers": transfers,
		"items": items,
	}
	normalized["composition_hash"] = _composition_hash(normalized)
	return normalized


def _normalize_transfer(value, transfer_index: int) -> tuple[dict, list[dict]]:
	if not isinstance(value, dict):
		_invalid(_("Every Payrexx payout transfer must be an object."))
	transfer_type = _choice(value, "type", TRANSFER_TYPES)
	amount = _required_integer(value, "amount")
	date_time = _date_time(value, "date_time")
	provider_items = value.get("items")
	if not isinstance(provider_items, list):
		_invalid(_("Every Payrexx payout transfer items value must be an array."))
	items = []
	for item_index, provider_item in enumerate(provider_items, start=1):
		if not isinstance(provider_item, dict):
			_invalid(_("Every Payrexx payout transfer item must be an object."))
		items.append(
			{
				"transfer_index": transfer_index,
				"provider_item_index": item_index,
				"item_type": _choice(provider_item, "type", ITEM_TYPES),
				"amount": _required_integer(provider_item, "amount"),
			}
		)
	if sum(item["amount"] for item in items) != amount:
		_invalid(_("A Payrexx payout transfer amount does not equal the sum of its item amounts."))

	transaction = value.get("transaction")
	if not isinstance(transaction, dict):
		_invalid(_("Every Payrexx payout transfer transaction must be an object."))
	row = {
		"transfer_index": transfer_index,
		"transfer_type": transfer_type,
		"amount": amount,
		"date_time": date_time,
		"has_transaction": bool(transaction),
	}
	if transaction:
		payment = _required_dict(transaction, "payment")
		transaction_uuid = _required_text(transaction, "uuid")
		if not 8 <= len(transaction_uuid) <= 40:
			_invalid(_("Payrexx payout transaction UUID must contain 8 to 40 characters."))
		row.update(
			{
				"transaction_type": _choice(transaction, "type", TRANSACTION_TYPES),
				"transaction_amount": _required_integer(transaction, "amount"),
				"transaction_uuid": transaction_uuid,
				"transaction_fee": _required_integer(transaction, "fee"),
				"transaction_currency": _currency(transaction, "currency"),
				"transaction_time": _date_time(transaction, "time"),
				"payment_brand": _required_text(payment, "brand"),
				"reference_id": _required_text(transaction, "reference_id", allow_empty=True),
			}
		)
	return row, items


def _apply_replay(document, normalized: dict) -> dict[str, bool | str]:
	if document.composition_hash != normalized["composition_hash"]:
		_invalid(_("Payrexx payout composition changed for an existing evidence key."))
	old_status = cstr(document.provider_status)
	new_status = normalized["status"]
	if old_status == new_status:
		return _result(document.name, old_status, created=False, status_changed=False)
	if old_status != "processing" or new_status not in {"sent", "failed"}:
		_invalid(_("Payrexx payout status may only progress from processing to sent or failed."))
	document.db_set(
		{
			"provider_status": new_status,
			"settlement_ready": new_status == "sent",
			"status_updated_on": now_datetime(),
		},
		update_modified=False,
	)
	return _result(document.name, new_status, created=False, status_changed=True)


def _result(name: str, status: str, *, created: bool, status_changed: bool) -> dict[str, bool | str]:
	return {
		"ok": True,
		"payout_evidence": name,
		"status": status,
		"created": created,
		"status_changed": status_changed,
	}


def _locked_evidence(evidence_key: str):
	if not frappe.db.exists(PAYOUT_EVIDENCE_DOCTYPE, evidence_key):
		return None
	return frappe.get_doc(PAYOUT_EVIDENCE_DOCTYPE, evidence_key, for_update=True)


def _evidence_key(settings_name: str, mode: str, payout_uuid: str) -> str:
	return hashlib.sha256(f"{settings_name}\0{mode}\0{payout_uuid}".encode()).hexdigest()


def _composition_hash(normalized: dict) -> str:
	composition = deepcopy(
		{key: value for key, value in normalized.items() if key not in {"status", "composition_hash"}}
	)
	for key in ("payout_date",):
		composition[key] = composition[key].isoformat()
	for transfer in composition["transfers"]:
		transfer["date_time"] = transfer["date_time"].isoformat()
		if transfer.get("transaction_time"):
			transfer["transaction_time"] = transfer["transaction_time"].isoformat()
	canonical = json.dumps(composition, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
	return hashlib.sha256(canonical.encode()).hexdigest()


def _hash_iban(normalized_iban: str) -> str:
	key = frappe.local.conf.get("encryption_key")
	if not key:
		frappe.throw(_("Site encryption_key is not configured"))
	key = key.encode() if isinstance(key, str) else bytes(key)
	return hmac.new(key, normalized_iban.encode(), hashlib.sha256).hexdigest()


def _normalize_iban(value: str) -> str:
	iban = "".join(value.split()).upper()
	if not _IBAN_PATTERN.fullmatch(iban):
		_invalid(_("Payrexx payout destination IBAN is invalid."))
	rearranged = iban[4:] + iban[:4]
	numeric = "".join(
		str(ord(character) - 55) if character.isalpha() else character for character in rearranged
	)
	if int(numeric) % 97 != 1:
		_invalid(_("Payrexx payout destination IBAN is invalid."))
	return iban


def _validate_merchant(merchant: dict) -> None:
	if "id" in merchant:
		_required_text(merchant, "id")
	_required_text(merchant, "name")
	_required_text(merchant, "site_title")
	owner = _required_dict(merchant, "owner")
	for key in ("company", "first_name", "last_name", "address", "zip", "place", "email"):
		_required_text(owner, key, allow_empty=True)


def _required_dict(value: dict, key: str) -> dict:
	result = value.get(key)
	if not isinstance(result, dict):
		_invalid(_("Payrexx payout field {0} must be an object.").format(key))
	return result


def _required_text(value: dict, key: str, *, allow_empty: bool = False) -> str:
	result = value.get(key)
	if not isinstance(result, str) or (not allow_empty and not result):
		_invalid(_("Payrexx payout field {0} must be a string.").format(key))
	return result


def _required_integer(value: dict, key: str) -> int:
	result = value.get(key)
	if isinstance(result, bool) or not isinstance(result, int):
		_invalid(_("Payrexx payout field {0} must be an integer in provider minor units.").format(key))
	return result


def _choice(value: dict, key: str, choices: frozenset[str]) -> str:
	result = _required_text(value, key)
	if result not in choices:
		_invalid(_("Payrexx payout field {0} has an unsupported value.").format(key))
	return result


def _currency(value: dict, key: str) -> str:
	currency = _required_text(value, key)
	if not _CURRENCY_PATTERN.fullmatch(currency):
		_invalid(_("Payrexx payout transaction currency must be an uppercase ISO 4217 code."))
	return currency


def _date(value: dict, key: str):
	text = _required_text(value, key)
	try:
		return datetime.strptime(text, "%Y-%m-%d").date()
	except ValueError:
		_invalid(_("Payrexx payout date must use YYYY-MM-DD."))


def _date_time(value: dict, key: str):
	text = _required_text(value, key)
	try:
		parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
	except ValueError:
		_invalid(_("Payrexx payout date-time fields must use ISO 8601 with a timezone."))
	if parsed.tzinfo is None:
		_invalid(_("Payrexx payout date-time fields must use ISO 8601 with a timezone."))
	return parsed.astimezone(UTC).replace(tzinfo=None)


def _invalid(message: str):
	frappe.throw(message, frappe.ValidationError)
