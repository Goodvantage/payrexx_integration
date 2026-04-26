import { test, expect } from "@playwright/test";

/**
 * Tests for /api/method/payrexx_integration.api.pay_invoice.
 *
 * No real Payrexx call is made — these specs exercise the auth and 404
 * paths. The "happy path" (302 to https://<instance>.payrexx.com/...) needs
 * a sandbox Payrexx instance and is covered by `booking_email.spec.ts`
 * (which itself is gated on TEST_BOOKING_NAME).
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
