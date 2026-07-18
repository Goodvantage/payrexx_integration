import { test, expect } from "@playwright/test";

/**
 * Tests for /api/method/payrexx_integration.api.pay_invoice.
 *
 * No real Payrexx call is made. These specs exercise missing/invalid-token
 * rejection paths only. The "happy path" 302 to Payrexx needs a separately
 * guarded sandbox scenario; `booking_email.spec.ts` verifies only that a
 * signed URL is rendered into an email when TEST_BOOKING_NAME is configured.
 *
 * The full URL+token round trip is exercised in the Python integration
 * tests at apps/payrexx_integration/payrexx_integration/payrexx_integration/
 * doctype/payrexx_settings/test_payrexx_settings.py.
 */

test.describe("pay_invoice endpoint — auth & error handling", () => {
	test("rejects requests without a token", async ({ request }) => {
		const r = await request.get(
			"/api/method/payrexx_integration.api.pay_invoice?si=ACC-SINV-NOPE",
			{ maxRedirects: 0 },
		);
		// Frappe surfaces ``frappe.PermissionError`` as HTTP 403.
		expect(r.status()).toBe(403);
	});

	test("rejects requests with a tampered token", async ({ request }) => {
		const r = await request.get(
			"/api/method/payrexx_integration.api.pay_invoice?si=ACC-SINV-NOPE&token=deadbeefdeadbeefdeadbeefdeadbeef",
			{ maxRedirects: 0 },
		);
		expect(r.status()).toBe(403);
	});

	test("rejects requests with a missing si parameter", async ({ request }) => {
		const r = await request.get(
			"/api/method/payrexx_integration.api.pay_invoice?token=anything",
			{ maxRedirects: 0 },
		);
		expect(r.status()).toBe(403);
	});

	test("rejects requests with no params at all", async ({ request }) => {
		const r = await request.get(
			"/api/method/payrexx_integration.api.pay_invoice",
			{ maxRedirects: 0 },
		);
		expect(r.status()).toBe(403);
	});
});
