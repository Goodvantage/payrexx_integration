import { test, expect } from "@playwright/test";
import { getDoc, gotoDesk } from "./helpers/frappe";

const SETTINGS_NAME = process.env.TEST_PAYREXX_SETTINGS || "Sandbox";
const PAYMENT_GATEWAY_PK = `Payrexx-${SETTINGS_NAME}`;

test.describe("Payrexx Settings — installed gateway", () => {
	test("settings row exists, form renders, and the Payment Gateway row is registered", async ({
		page,
		request,
	}) => {
		// Pre-condition: the row exists. Fail loudly if not — that's a setup
		// problem, not a test problem.
		const settings = await getDoc(request, "Payrexx Settings", SETTINGS_NAME);
		expect(
			settings,
			`expected Payrexx Settings ${SETTINGS_NAME} to exist on this site (set TEST_PAYREXX_SETTINGS to override)`,
		).not.toBeNull();

		// 1. Form renders with the correct values
		await gotoDesk(page, "Payrexx Settings", SETTINGS_NAME);
		await expect(
			page.locator('input[data-fieldname="gateway_name"]').first(),
		).toHaveValue(SETTINGS_NAME, { timeout: 30_000 });
		await expect(
			page.locator('input[data-fieldname="instance_name"]').first(),
		).not.toHaveValue("");
		const webhookMessage = page.locator(".form-message", {
			hasText: "Webhook URL for Payrexx:",
		});
		await expect(webhookMessage).toHaveCount(1);
		await page.evaluate(() => (window as any).cur_frm.trigger("gateway_name"));
		await page.evaluate(() => (window as any).cur_frm.trigger("gateway_name"));
		await expect(webhookMessage).toHaveCount(1);

		// 2. The matching Payment Gateway registry row was auto-created by on_update
		const gateway = await getDoc(request, "Payment Gateway", PAYMENT_GATEWAY_PK);
		expect(
			gateway,
			`expected Payment Gateway ${PAYMENT_GATEWAY_PK} to exist (auto-created by on_update)`,
		).not.toBeNull();
		expect(gateway.gateway_settings).toBe("Payrexx Settings");
		expect(gateway.gateway_controller).toBe(SETTINGS_NAME);
	});
});
