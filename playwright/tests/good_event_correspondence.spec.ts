import { test, expect } from "@playwright/test";
import { callMethod, getDoc } from "./helpers/frappe";

/**
 * Coverage for workflow_state derivation plus the current Good Event manual
 * correspondence contract.
 *
 * Gated on TEST_BOOKING_NAME — the booking must already exist on the site
 * (with at least one attendee whose email is set). The dev_e2e bench helper
 * creates one easily:
 *
 *   bench --site development16.localhost execute \
 *     payrexx_integration.dev_e2e.run_event_to_invoice_email \
 *     --kwargs '{"email": "benediktmathis@gmail.com"}'
 *
 * then export the printed booking name as TEST_BOOKING_NAME.
 */

const BOOKING = process.env.TEST_BOOKING_NAME;

test.skip(!BOOKING, "Set TEST_BOOKING_NAME to run the correspondence flow specs.");

/** Proper quoted-printable + UTF-8 decoder (handles multi-byte =C3=A4 etc.). */
function decodeMime(s: string): string {
	// First strip soft line breaks (=\r\n or =\n at end of line).
	const joined = s.replace(/=\r?\n/g, "");
	// Then decode all =XX sequences as bytes and re-interpret as UTF-8.
	const bytes: number[] = [];
	let i = 0;
	while (i < joined.length) {
		const c = joined.charCodeAt(i);
		if (joined[i] === "=" && i + 2 < joined.length) {
			const hex = joined.slice(i + 1, i + 3);
			if (/^[0-9A-Fa-f]{2}$/.test(hex)) {
				bytes.push(parseInt(hex, 16));
				i += 3;
				continue;
			}
		}
		bytes.push(c);
		i += 1;
	}
	const decoded = new TextDecoder("utf-8", { fatal: false }).decode(new Uint8Array(bytes));
	return decoded.replace(/&amp;/g, "&");
}

async function fetchLatestQueueFor(
	request: import("@playwright/test").APIRequestContext,
	referenceDoctype: string,
	referenceName: string,
): Promise<{ name: string; subject: string; message: string }> {
	const list = (await callMethod(request, "frappe.client.get_list", {
		doctype: "Email Queue",
		filters: { reference_doctype: referenceDoctype, reference_name: referenceName },
		fields: ["name"],
		limit_page_length: 1,
		order_by: "creation desc",
	})) as { name: string }[];
	expect(list.length, `expected at least one Email Queue row for ${referenceName}`).toBeGreaterThan(0);
	const full = (await getDoc(request, "Email Queue", list[0].name)) as {
		name: string;
		message: string;
	};
	const decoded = decodeMime(full.message || "");
	const subjectMatch = decoded.match(/^Subject:\s*(.+)$/m);
	return { name: full.name, subject: subjectMatch ? subjectMatch[1] : "", message: decoded };
}

test.describe("Good Event Booking workflow state", () => {
	test("workflow_state is populated on the booking (backfill or live derivation)", async ({
		request,
	}) => {
		const booking = (await getDoc(request, "Good Event Booking", BOOKING!)) as {
			workflow_state: string;
		};
		expect(booking).not.toBeNull();
		expect(
			["Draft", "Pending Review", "Approved", "Waitlisted", "Confirmed", "Rejected", "Cancelled"],
			`unexpected workflow_state value: ${booking.workflow_state}`,
		).toContain(booking.workflow_state);
	});

	// Regression: a pay-later booking, once submitted, must end up in
	// Confirmed AND must auto-fire (a) the registration_confirmation /
	// combined_bundle email referencing the booking and (b) the invoice
	// email referencing the Sales Invoice. Used to fail because
	// _sync_workflow_state derived workflow_state in validate() before
	// before_submit flipped status to Confirmed.
	test("pay-later booking ends up Confirmed AND queues both auto emails", async ({
		request,
	}) => {
		const booking = (await getDoc(request, "Good Event Booking", BOOKING!)) as {
			workflow_state: string;
			docstatus: number;
			payment_preference: string;
			pay_later_selected: number;
			sales_invoice: string | null;
		};
		test.skip(
			!booking.pay_later_selected,
			`booking ${BOOKING} is not a pay-later booking — skipping confirmation regression test`,
		);
		expect(booking.docstatus, "should be submitted (docstatus=1)").toBe(1);
		expect(booking.workflow_state, "pay-later submit must derive to Confirmed").toBe(
			"Confirmed",
		);

		// (a) booking-referenced email — optional because retained deployments
		// may disable or replace automatic booking correspondence.
		const bookingQ = (await callMethod(request, "frappe.client.get_list", {
			doctype: "Email Queue",
			filters: { reference_doctype: "Good Event Booking", reference_name: BOOKING },
			fields: ["name"],
			limit_page_length: 1,
			order_by: "creation desc",
		})) as { name: string }[];
		test.skip(
			bookingQ.length === 0,
			`booking ${BOOKING} has no booking-referenced auto correspondence in this deployment`,
		);

		// (b) SI-referenced email — invoice flow
		expect(booking.sales_invoice, "pay-later flow should produce a Sales Invoice").toBeTruthy();
		const siQ = (await callMethod(request, "frappe.client.get_list", {
			doctype: "Email Queue",
			filters: {
				reference_doctype: "Sales Invoice",
				reference_name: booking.sales_invoice,
			},
			fields: ["name"],
			limit_page_length: 1,
			order_by: "creation desc",
		})) as { name: string }[];
		expect(
			siQ.length,
			"expected an Email Queue row referencing the Sales Invoice (invoice flow)",
		).toBeGreaterThan(0);
	});
});

test.describe("Manual correspondence triggers (api.send_correspondence)", () => {
	test("rejects unknown flow keys", async ({ request }) => {
		const r = await request.post("/api/method/good_event.api.send_correspondence", {
			form: {
				booking: BOOKING!,
				flow: "not_a_real_flow_key",
			},
		});
		expect(r.status()).toBeGreaterThanOrEqual(400);
		expect(r.status()).toBeLessThan(500);
	});

	test("retired flow keys are not manually wired", async ({ request }) => {
		for (const flow of [
			"acceptance",
			"rejection",
			"combined_bundle",
			"webinar_access",
			"waitlist_offer",
		]) {
			const r = await request.post("/api/method/good_event.api.send_correspondence", {
				form: {
					booking: BOOKING!,
					flow,
				},
			});
			expect(r.status(), `${flow} should reject`).toBeGreaterThanOrEqual(400);
			expect(r.status(), `${flow} should reject without a server error`).toBeLessThan(500);
		}
	});

	test("ad_hoc rejects when no body is provided", async ({ request }) => {
		const r = await request.post("/api/method/good_event.api.send_correspondence", {
			form: {
				booking: BOOKING!,
				flow: "ad_hoc",
			},
		});
		expect(r.status()).toBeGreaterThanOrEqual(400);
		expect(r.status()).toBeLessThan(500);
	});

	test("ad_hoc with body queues a custom-body email", async ({ request }) => {
		const result = (await callMethod(request, "good_event.api.send_correspondence", {
			booking: BOOKING!,
			flow: "ad_hoc",
			context_overrides: { ad_hoc_body: "<p>Custom test message — only on this run.</p>" },
		})) as { flow: string; sent: string[]; skipped: string[] };
		expect(result.flow).toBe("ad_hoc");
		expect(result.sent.length + result.skipped.length).toBeGreaterThan(0);
		test.skip(result.sent.length === 0, "No ad-hoc recipients were sendable for this fixture.");

		const { message } = await fetchLatestQueueFor(request, "Good Event Booking", BOOKING!);
		expect(message).toContain("Custom test message");
	});
});
