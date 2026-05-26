"""Dev-only end-to-end test helpers for the Payrexx + event_app flow.

These are intentionally not whitelisted — call via:

    bench --site <site> execute \\
      payrexx_integration.dev_e2e.run_event_to_invoice_email \\
      --kwargs '{"email": "benediktmathis@gmail.com"}'
"""

from __future__ import annotations

import time
import traceback

import frappe
from frappe.utils import add_days, nowdate


def _ensure_contact(email: str) -> str:
	name = frappe.db.get_value("Contact", {"email_id": email}, "name")
	if name:
		return name
	doc = frappe.get_doc(
		{
			"doctype": "Contact",
			"first_name": "Benedikt",
			"last_name": "Mathis",
			"email_ids": [{"email_id": email, "is_primary": 1}],
		}
	).insert(ignore_permissions=True)
	return doc.name


def _ensure_customer(contact_name: str) -> str:
	name = frappe.db.get_value("Customer", {"customer_primary_contact": contact_name}, "name")
	if name:
		return name
	doc = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": "Benedikt Mathis (E2E)",
			"customer_type": "Individual",
			"customer_primary_contact": contact_name,
		}
	).insert(ignore_permissions=True)
	frappe.db.set_value("Customer", doc.name, "customer_primary_contact", contact_name, update_modified=False)
	return doc.name


def run_event_to_invoice_email(email: str = "benediktmathis@gmail.com") -> dict:
	"""Create event → publish → book (pay-later) → invoice → email; print + return summary."""
	tag = f"E2E-{int(time.time())}"
	out: dict = {"tag": tag}
	print(f"\n=== {tag} ===\n", flush=True)

	try:
		# 1. Customer + Contact
		contact_name = _ensure_contact(email)
		customer_name = _ensure_customer(contact_name)
		print(f"[OK] Contact: {contact_name}", flush=True)
		print(f"[OK] Customer: {customer_name}", flush=True)
		out.update({"contact": contact_name, "customer": customer_name})

		# 2. Buzz Event (published, allow pay-later)
		event = frappe.get_doc(
			{
				"doctype": "Buzz Event",
				"title": f"Payrexx E2E {tag}",
				"category": "Meetups",
				"host": "Test Host",
				"start_date": add_days(nowdate(), 14),
				"start_time": "09:00:00",
				"end_time": "17:00:00",
				"is_published": 1,
				"allow_guest_booking": 1,
				"allow_pay_later": 1,
				"pay_later_is_default": 1,
				"currency": "CHF",
			}
		).insert(ignore_permissions=True)
		print(
			f"[OK] Buzz Event: {event.name}  published={event.is_published}  "
			f"allow_pay_later={event.allow_pay_later}",
			flush=True,
		)
		out["event"] = event.name

		# 3. Ticket Type
		tt = frappe.get_doc(
			{
				"doctype": "Event Ticket Type",
				"event": event.name,
				"title": "Standard",
				"currency": "CHF",
				"price": 50,
				"is_published": 1,
			}
		).insert(ignore_permissions=True)
		print(f"[OK] Ticket Type: {tt.name}  {tt.price} {tt.currency}", flush=True)
		out["ticket_type"] = tt.name

		# 4. Event Booking (pay-later)
		booking = frappe.get_doc(
			{
				"doctype": "Event Booking",
				"event": event.name,
				"user": "Administrator",
				"currency": "CHF",
				"billing_customer": customer_name,
				"billing_contact": contact_name,
				"linked_customer": customer_name,
				"pay_later_selected": 1,
				"payment_preference": "Pay Later",
				"attendees": [
					{
						"first_name": "Benedikt",
						"last_name": "Mathis",
						"email": email,
						"ticket_type": tt.name,
						"currency": "CHF",
						"participant_contact": contact_name,
						"participant_name": "Benedikt Mathis",
					}
				],
			}
		)
		booking.insert(ignore_permissions=True)
		print(
			f"[OK] Booking inserted: {booking.name}  "
			f"status={booking.status} payment_status={booking.payment_status}",
			flush=True,
		)
		booking.submit()
		print(
			f"[OK] Booking submitted: status={booking.status} "
			f"payment_status={booking.payment_status} total={booking.total_amount}",
			flush=True,
		)
		out["booking"] = booking.name
		out["total"] = float(booking.total_amount or 0)

		# 5. Sales Invoice + email
		si_name = booking.create_sales_invoice()
		si = frappe.get_doc("Sales Invoice", si_name)
		print(
			f"[OK] Sales Invoice: {si.name}  grand_total={si.grand_total} {si.currency}  "
			f"contact_email={si.contact_email!r}",
			flush=True,
		)
		out["invoice"] = si.name
		out["invoice_total"] = float(si.grand_total)

		# 6. Verify email queue + show snippet
		eq = frappe.get_all(
			"Email Queue",
			filters={"reference_doctype": "Sales Invoice", "reference_name": si_name},
			fields=["name", "status", "message"],
			order_by="creation desc",
			limit=3,
		)
		_send_only_created_invoice_emails(eq)
		eq = frappe.get_all(
			"Email Queue",
			filters={"reference_doctype": "Sales Invoice", "reference_name": si_name},
			fields=["name", "status", "message"],
			order_by="creation desc",
			limit=3,
		)
		print("[OK] Created invoice email queue row(s) sent", flush=True)

		print(f"\n=== Email Queue rows: {len(eq)} ===", flush=True)
		for row in eq:
			msg = row.message or ""
			# QP-decode so substrings split across line wraps still match.
			plain = msg.replace("=\r\n", "").replace("=\n", "").replace("=3D", "=")
			has_pay_url = "payrexx_integration.api.pay_invoice" in plain
			has_qr_pdf = f"{si_name}.pdf" in plain and "Content-Disposition: attachment" in plain
			print(
				f"  {row.name} | status={row.status} | pay_url={has_pay_url} qr_pdf={has_qr_pdf}",
				flush=True,
			)

		# 8. Recipient audit
		if eq:
			recips = frappe.get_all(
				"Email Queue Recipient",
				filters={"parent": eq[0].name},
				fields=["recipient", "status"],
			)
			print("\n=== Recipients ===", flush=True)
			for r in recips:
				print(f"  {r.recipient}  | {r.status}", flush=True)

		out["email_queue_id"] = eq[0].name if eq else None
		out["email_queue_status"] = eq[0].status if eq else None

		# 9. Detailed queue inspection
		if eq:
			inspect_email_queue_row(eq[0].name)

		print("\n=== SUMMARY ===", flush=True)
		for k, v in out.items():
			print(f"  {k:18s} = {v}", flush=True)

		frappe.db.commit()  # nosemgrep: frappe-manual-commit
		return out

	except Exception:
		traceback.print_exc()
		raise


def _send_only_created_invoice_emails(queue_rows: list) -> None:
	previous_testing_email = frappe.flags.get("testing_email")
	frappe.flags.testing_email = True
	try:
		for row in queue_rows:
			frappe.get_doc("Email Queue", row.name).send(force_send=True)
	finally:
		frappe.flags.testing_email = previous_testing_email


def inspect_email_queue_row(name: str) -> dict:
	"""Print a human-readable report of an Email Queue row and return key facts."""
	import re

	eq = frappe.get_doc("Email Queue", name)
	msg = eq.message or ""

	def _hdr(field: str) -> str | None:
		m = re.search(rf"^{re.escape(field)}:\s*(.+)$", msg, re.MULTILINE)
		return m.group(1).strip() if m else None

	subject = _hdr("Subject")
	sender = _hdr("From")
	to_hdr = _hdr("To")

	# Decode the body's quoted-printable wrapping enough to find the URL.
	# QP wraps long lines with =\r\n (or =\n) and encodes '=' as '=3D'.
	plain = msg.replace("=\r\n", "").replace("=\n", "").replace("=3D", "=")
	# Pull the URL straight out of an <a href="…"> so HTML entity wrapping
	# (e.g. ``&amp;``) doesn't break the match.
	pay_m = re.search(r'href="([^"]*pay_invoice[^"]*)"', plain)
	pay_url = pay_m.group(1).replace("&amp;", "&") if pay_m else None
	has_qr_pdf = "Content-Disposition: attachment" in plain and ".pdf" in plain

	# Attachments listed in the MIME stream
	atts = re.findall(r'Content-Disposition: attachment;\s*filename="?([^"\n;]+)', msg)

	recips = frappe.get_all(
		"Email Queue Recipient",
		filters={"parent": name},
		fields=["recipient", "status", "error"],
	)

	# Snippet of the visible button/link area
	ix = plain.find("Pay")
	visible_snippet = plain[max(0, ix - 40) : ix + 200] if ix >= 0 else ""

	print(f"\n=== Email Queue {name} — detailed inspection ===", flush=True)
	print(f"  Status:        {eq.status}", flush=True)
	print(f"  Sender (DB):   {eq.sender}", flush=True)
	print(f"  From (header): {sender}", flush=True)
	print(f"  To (header):   {to_hdr}", flush=True)
	print(f"  Subject:       {subject}", flush=True)
	print(f"  Body bytes:    {len(msg)}  (incl. PDF attachment if present)", flush=True)
	print(f"  Attachments:   {atts or '(none)'}", flush=True)
	print(f"  QR-bill PDF:   {'attached' if has_qr_pdf else 'MISSING'}", flush=True)
	print(f"  Pay URL found: {bool(pay_url)}", flush=True)
	if pay_url:
		print(f"     {pay_url}", flush=True)
	print("  Recipients:", flush=True)
	for r in recips:
		print(
			f"     {r.recipient}  | {r.status}  | err={(r.error or '')[:120]}",
			flush=True,
		)
	if visible_snippet:
		print("\n  --- visible body snippet around 'Pay' ---", flush=True)
		print("  " + visible_snippet.replace("\n", "\n  "), flush=True)

	return {
		"name": name,
		"status": eq.status,
		"subject": subject,
		"sender": sender,
		"to": to_hdr,
		"pay_url": pay_url,
		"attachments": atts,
		"recipients": [(r.recipient, r.status) for r in recips],
	}
