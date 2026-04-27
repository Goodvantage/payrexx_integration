import { test, expect, request as playwrightRequest } from "@playwright/test";

/**
 * Real-browser guest pay-later regression test.
 *
 * Why this exists separately from the existing Python / mock-guest tests:
 * `frappe.set_user("Guest")` in a backend test runs through the desk-side
 * insert chain with `ignore_permissions=True`. The actual public booking flow
 * goes through `buzz.api.process_booking` over HTTP **with no auth cookie**,
 * which has different permission, hook, and queue interactions. B048 / B088
 * stayed in Draft on the live site even though the Python set_user('Guest')
 * test passed — so we now drive the real network path.
 *
 * The test:
 *   1. opens an *unauthenticated* request context (no auth.json)
 *   2. POSTs `buzz.api.process_booking` with `is_offline=true`
 *      (the buzz dashboard's "Book Tickets" button does the same)
 *   3. polls the resulting booking via an Administrator API context
 *      (we still need admin to *verify* the post-state, but the booking
 *      itself was created by Guest)
 *   4. asserts docstatus=1, workflow_state="Confirmed", sales_invoice set
 */

const EVENT_NAME = process.env.TEST_GUEST_EVENT || "2997"; // Notfallkurs für Kinder
const TICKET_TYPE = process.env.TEST_GUEST_TICKET || "5782"; // Standard ticket
// Offline Payment Method NAME (not title) on the event. buzz looks this up via
// `name` filter — so it's the autoname of the Offline Payment Method row.
const OFFLINE_METHOD = process.env.TEST_GUEST_OFFLINE_METHOD || "68";
const ADMIN_USER = process.env.FRAPPE_USERNAME || "Administrator";
const ADMIN_PASS = process.env.FRAPPE_PASSWORD || "admin";
const BASE = process.env.PLAYWRIGHT_BASE_URL || "http://localhost:8000";
const FIXED_GUEST_EMAIL = process.env.TEST_GUEST_EMAIL || "";
const FIXED_GUEST_FULL_NAME = process.env.TEST_GUEST_FULL_NAME || "";

// This file deliberately does NOT inherit the auth.json storageState — every
// API call below uses freshly constructed contexts so the test mirrors a
// real anonymous visitor.
test.use({ storageState: { cookies: [], origins: [] } });

test.describe("Guest pay-later flow over HTTP", () => {
	test("anonymous process_booking → after_insert auto-submit → Confirmed + emails", async () => {
		const guestCtx = await playwrightRequest.newContext({
			baseURL: BASE,
			ignoreHTTPSErrors: true,
		});
		const adminCtx = await playwrightRequest.newContext({
			baseURL: BASE,
			ignoreHTTPSErrors: true,
		});

		const tag = Date.now().toString(36);
		const guestEmail = FIXED_GUEST_EMAIL || `playwright-guest-${tag}@example.com`;
		const guestFullName = FIXED_GUEST_FULL_NAME || `Playwright Guest ${tag}`;

		try {
			// Sanity: prove the guest ctx is actually unauthenticated. Frappe
			// returns "Guest" for anonymous, "Administrator"/email for logged-in.
			// Some sites return an empty/None message for guests — accept either.
			const whoamiBefore = await guestCtx
				.get("/api/method/frappe.auth.get_logged_user")
				.then((r) => r.json())
				.catch(() => ({ message: null }));
			expect(
				[null, undefined, "Guest"].includes(whoamiBefore?.message),
				`guest ctx must be unauth at start (got: ${JSON.stringify(whoamiBefore)})`,
			).toBeTruthy();

			// 1. Drive process_booking exactly like the dashboard does. Frappe
			//    v17 enforces pydantic typing on whitelisted args, so the
			//    payload must be a real JSON body — form-encoded JSON strings
			//    no longer coerce to list/dict/bool.
			const processResp = await guestCtx.post("/api/method/buzz.api.process_booking", {
				headers: { "Content-Type": "application/json" },
				data: {
					event: EVENT_NAME,
					guest_email: guestEmail,
					guest_full_name: guestFullName,
					is_offline: true,
					offline_payment_method: OFFLINE_METHOD,
					attendees: [
						{
							first_name: "Playwright",
							last_name: `Guest ${tag}`,
							email: guestEmail,
							ticket_type: TICKET_TYPE,
						},
					],
				},
			});
			const processBody = await processResp.json().catch(() => ({}));
			expect(
				processResp.ok(),
				`process_booking failed (${processResp.status()}): ${JSON.stringify(processBody)}`,
			).toBeTruthy();

			// process_booking offline path returns { booking_name, offline_payment }
			// in `message`. The free / online paths use different shapes.
			const bookingId: string =
				processBody?.message?.booking_name ||
				processBody?.message?.booking_id ||
				processBody?.message?.name ||
				processBody?.message;
			expect(
				typeof bookingId === "string" && bookingId.startsWith("B"),
				`expected a booking id from process_booking, got: ${JSON.stringify(processBody?.message)}`,
			).toBeTruthy();

			// 2. Log in as Administrator on a *separate* context to read the
			//    post-state. This is read-only verification, not the action.
			const loginResp = await adminCtx.post("/api/method/login", {
				form: { usr: ADMIN_USER, pwd: ADMIN_PASS },
			});
			expect(loginResp.ok(), `admin login failed (${loginResp.status()})`).toBeTruthy();

			// 3. Poll until the after_insert background job promotes the booking
			//    to docstatus=1 / Confirmed. Worker may take a beat.
			let booking: Record<string, unknown> | null = null;
			const deadline = Date.now() + 30_000;
			while (Date.now() < deadline) {
				const r = await adminCtx.get(
					`/api/resource/Event Booking/${encodeURIComponent(bookingId)}`,
				);
				if (r.ok()) {
					const body = await r.json();
					booking = body.data;
					if ((booking as { docstatus?: number })?.docstatus === 1) break;
				}
				await new Promise((res) => setTimeout(res, 1000));
			}

			expect(booking, `booking ${bookingId} not readable after submit`).not.toBeNull();
			expect(
				(booking as { docstatus?: number })?.docstatus,
				`booking ${bookingId} stayed in Draft (after_insert auto-submit didn't run — likely a stale RQ worker that doesn't have payrexx_integration in its module map; restart bench worker)`,
			).toBe(1);
			expect(
				(booking as { workflow_state?: string })?.workflow_state,
				`booking ${bookingId} workflow_state should derive to Confirmed for guest pay-later`,
			).toBe("Confirmed");
			expect(
				(booking as { status?: string })?.status,
				`booking ${bookingId} status should be Confirmed`,
			).toBe("Confirmed");
			expect(
				(booking as { pay_later_selected?: number })?.pay_later_selected,
				"pay_later_selected must be flipped on by buzz's offline path",
			).toBe(1);
			expect(
				(booking as { sales_invoice?: string | null })?.sales_invoice,
				"on_submit should have auto-created a Sales Invoice",
			).toBeTruthy();

			// 4. Email Queue should have at least the booking-referenced email
			//    (registration_confirmation / combined_bundle) AND the
			//    SI-referenced invoice email.
			const eqBookingResp = await adminCtx.get(
				"/api/method/frappe.client.get_list?doctype=Email Queue" +
					"&fields=" +
					encodeURIComponent('["name"]') +
					"&filters=" +
					encodeURIComponent(
						JSON.stringify({
							reference_doctype: "Event Booking",
							reference_name: bookingId,
						}),
					) +
					"&limit_page_length=5",
			);
			const eqBooking = (await eqBookingResp.json())?.message ?? [];
			expect(
				eqBooking.length,
				`expected ≥1 Email Queue row referencing the booking ${bookingId}`,
			).toBeGreaterThan(0);

			const siName = (booking as { sales_invoice?: string }).sales_invoice;
			const eqSiResp = await adminCtx.get(
				"/api/method/frappe.client.get_list?doctype=Email Queue" +
					"&fields=" +
					encodeURIComponent('["name"]') +
					"&filters=" +
					encodeURIComponent(
						JSON.stringify({
							reference_doctype: "Sales Invoice",
							reference_name: siName,
						}),
					) +
					"&limit_page_length=5",
			);
			const eqSi = (await eqSiResp.json())?.message ?? [];
			expect(eqSi.length, `expected ≥1 Email Queue row referencing SI ${siName}`).toBeGreaterThan(
				0,
			);
		} finally {
			await guestCtx.dispose();
			await adminCtx.dispose();
		}
	});
});
