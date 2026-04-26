import { test, expect } from "@playwright/test";
import { callMethod, getDoc } from "./helpers/frappe";

/**
 * Phase 2 + 3A coverage: workflow_state derivation and manual triggers for
 * registration_confirmation, acceptance, and rejection.
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

test.describe("Event Booking workflow state", () => {
	test("workflow_state is populated on the booking (backfill or live derivation)", async ({
		request,
	}) => {
		const booking = (await getDoc(request, "Event Booking", BOOKING!)) as {
			workflow_state: string;
		};
		expect(booking).not.toBeNull();
		expect(
			["Draft", "Pending Review", "Approved", "Waitlisted", "Confirmed", "Rejected", "Cancelled"],
			`unexpected workflow_state value: ${booking.workflow_state}`,
		).toContain(booking.workflow_state);
	});
});

test.describe("Manual correspondence triggers (api.send_correspondence)", () => {
	test("registration_confirmation queues a German confirmation email per attendee", async ({
		request,
	}) => {
		const result = (await callMethod(request, "event_app.api.send_correspondence", {
			booking: BOOKING!,
			flow: "registration_confirmation",
		})) as { sent: string[]; skipped: string[]; flow: string };
		expect(result.flow).toBe("registration_confirmation");
		expect(result.sent.length).toBeGreaterThan(0);

		const { subject, message } = await fetchLatestQueueFor(request, "Event Booking", BOOKING!);
		expect(subject).toContain("Anmeldebest"); // "Anmeldebestätigung" — accent encoded in MIME header
		expect(message).toContain("Anmeldung");
		expect(message).toContain("ist bestätigt");
	});

	test("acceptance flow queues a German Zusage email", async ({ request }) => {
		const result = (await callMethod(request, "event_app.api.send_correspondence", {
			booking: BOOKING!,
			flow: "acceptance",
		})) as { sent: string[] };
		expect(result.sent.length).toBeGreaterThan(0);

		const { subject, message } = await fetchLatestQueueFor(request, "Event Booking", BOOKING!);
		expect(subject).toContain("Zusage");
		expect(message).toContain("freuen uns");
	});

	test("rejection flow queues a German Absage email", async ({ request }) => {
		const result = (await callMethod(request, "event_app.api.send_correspondence", {
			booking: BOOKING!,
			flow: "rejection",
		})) as { sent: string[] };
		expect(result.sent.length).toBeGreaterThan(0);

		const { subject, message } = await fetchLatestQueueFor(request, "Event Booking", BOOKING!);
		expect(subject).toContain("Absage");
		expect(message).toContain("Leider");
	});

	test("rejects unknown flow keys", async ({ request }) => {
		const r = await request.post("/api/method/event_app.api.send_correspondence", {
			form: {
				booking: BOOKING!,
				flow: "not_a_real_flow_key",
			},
		});
		expect(r.status()).toBeGreaterThanOrEqual(400);
		expect(r.status()).toBeLessThan(500);
	});

	test("forced language fr produces a French body", async ({ request }) => {
		const result = (await callMethod(request, "event_app.api.send_correspondence", {
			booking: BOOKING!,
			flow: "acceptance",
			language: "fr",
		})) as { sent: string[] };
		expect(result.sent.length).toBeGreaterThan(0);

		const { subject, message } = await fetchLatestQueueFor(request, "Event Booking", BOOKING!);
		expect(subject).toMatch(/Acceptation/);
		expect(message).toContain("Bonjour");
	});

	test("combined_bundle queues the comprehensive German confirmation", async ({ request }) => {
		const result = (await callMethod(request, "event_app.api.send_correspondence", {
			booking: BOOKING!,
			flow: "combined_bundle",
		})) as { sent: string[] };
		expect(result.sent.length).toBeGreaterThan(0);

		const { subject, message } = await fetchLatestQueueFor(request, "Event Booking", BOOKING!);
		expect(subject).toContain("Bestätigung");
		expect(message).toContain("ist bestätigt");
	});

	test("webinar_access queues the webinar credentials email", async ({ request }) => {
		const result = (await callMethod(request, "event_app.api.send_correspondence", {
			booking: BOOKING!,
			flow: "webinar_access",
		})) as { sent: string[] };
		expect(result.sent.length).toBeGreaterThan(0);

		const { subject } = await fetchLatestQueueFor(request, "Event Booking", BOOKING!);
		expect(subject).toContain("Webinar-Zugangsdaten");
	});

	test("waitlist_offer queues the offer email with deadline placeholder", async ({ request }) => {
		const result = (await callMethod(request, "event_app.api.send_correspondence", {
			booking: BOOKING!,
			flow: "waitlist_offer",
		})) as { sent: string[] };
		expect(result.sent.length).toBeGreaterThan(0);

		const { subject } = await fetchLatestQueueFor(request, "Event Booking", BOOKING!);
		expect(subject).toContain("Warteliste");
	});

	test("ad_hoc rejects when no body is provided", async ({ request }) => {
		const r = await request.post("/api/method/event_app.api.send_correspondence", {
			form: {
				booking: BOOKING!,
				flow: "ad_hoc",
			},
		});
		expect(r.status()).toBeGreaterThanOrEqual(400);
		expect(r.status()).toBeLessThan(500);
	});

	test("ad_hoc with body queues a custom-body email", async ({ request }) => {
		const result = (await callMethod(request, "event_app.api.send_correspondence", {
			booking: BOOKING!,
			flow: "ad_hoc",
			context_overrides: { ad_hoc_body: "<p>Custom test message — only on this run.</p>" },
		})) as { sent: string[] };
		expect(result.sent.length).toBeGreaterThan(0);

		const { message } = await fetchLatestQueueFor(request, "Event Booking", BOOKING!);
		expect(message).toContain("Custom test message");
	});
});
