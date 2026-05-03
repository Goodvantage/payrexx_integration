import { test, expect, request as playwrightRequest, APIRequestContext } from "@playwright/test";
import { callMethod, getDoc } from "./helpers/frappe";

const RUN_SANDBOX_PAYMENT = process.env.RUN_PAYREXX_SANDBOX_PAYMENT === "1";
const BASE = process.env.PLAYWRIGHT_BASE_URL || "http://localhost:8000";
const ADMIN_USER = process.env.FRAPPE_USERNAME || "Administrator";
const ADMIN_PASS = process.env.FRAPPE_PASSWORD || "admin";
const EVENT_NAME = process.env.TEST_GUEST_EVENT || "2997";
const TICKET_TYPE = process.env.TEST_GUEST_TICKET || "5782";
const OFFLINE_METHOD = process.env.TEST_GUEST_OFFLINE_METHOD || "68";

test("event payment success page renders without invoice params", async ({ page }) => {
	await page.goto("/payment-success");
	await expect(page.getByRole("heading", { name: "Zahlung erfolgreich" })).toBeVisible();
	await expect(page.getByText("Zur Übersicht")).toBeVisible();
});

test.describe("Payrexx hosted checkout sandbox payment", () => {
	test.skip(
		!RUN_SANDBOX_PAYMENT,
		"Set RUN_PAYREXX_SANDBOX_PAYMENT=1 to submit a real Payrexx sandbox test payment.",
	);

	test("creates an Event App invoice and submits a Payrexx test payment", async ({ page }) => {
		const fixture = await createPayLaterInvoiceWithPayUrl();
		expect(fixture.payUrl).toContain("payrexx_integration.api.pay_invoice");
		expect(fixture.payUrl).toContain("gateway_name=Sandbox");

		await page.goto(fixture.payUrl, { waitUntil: "networkidle" });
		await expect(page).toHaveURL(/spendedirekt\.payrexx\.com/);
		await expect(page.getByText("Einige Zahlungsmittel sind im Testmodus")).toBeVisible();

		await page.locator(".masonry-type .payment-method").first().click();
		await expect(page.getByText("Dies ist eine Test-Zahlung")).toBeVisible();
		await page.locator('input[name="email"]:visible').fill(fixture.email);

		await page.locator("button.pay-button:visible", { hasText: /Bezahlen/ }).click();
		const result = await waitForPayrexxResult(page);
		expect(
			result,
			`expected Payrexx to submit the test payment, got URL=${page.url()}`,
		).toBeTruthy();

		await page.goto(`${BASE}/payment-success?doctype=Sales%20Invoice&docname=${fixture.invoiceName}`);
		await expect(page.getByRole("heading", { name: "Zahlung erfolgreich" })).toBeVisible();
		await expect(page.getByText(fixture.invoiceName)).toBeVisible();
	});
});

async function createPayLaterInvoiceWithPayUrl() {
	const guestCtx = await playwrightRequest.newContext({ baseURL: BASE, ignoreHTTPSErrors: true });
	const adminCtx = await playwrightRequest.newContext({ baseURL: BASE, ignoreHTTPSErrors: true });
	const tag = Date.now().toString(36);
	const email = `playwright-payrexx-${tag}@example.com`;

	try {
		await loginAsAdmin(adminCtx);
		const bookingId = await createGuestPayLaterBooking(guestCtx, email, tag);
		const booking = await waitForSubmittedBooking(adminCtx, bookingId);
		const invoiceName = String(booking.sales_invoice || "");
		expect(invoiceName, `booking ${bookingId} should have a Sales Invoice`).toBeTruthy();

		await callMethod(adminCtx, "event_app.api.send_correspondence", {
			booking: bookingId,
			flow: "invoice",
		});
		const queueName = await waitForInvoiceEmail(adminCtx, invoiceName);
		const queueDoc = await getDoc(adminCtx, "Email Queue", queueName);
		const payUrl = extractPayUrl(decodeMime((queueDoc as { message: string }).message));
		expect(payUrl, `invoice email ${queueName} should contain Payrexx pay URL`).toBeTruthy();

		return { bookingId, invoiceName, email, payUrl };
	} finally {
		await guestCtx.dispose();
		await adminCtx.dispose();
	}
}

async function loginAsAdmin(adminCtx: APIRequestContext) {
	const loginResp = await adminCtx.post("/api/method/login", {
		form: { usr: ADMIN_USER, pwd: ADMIN_PASS },
	});
	expect(loginResp.ok(), `admin login failed (${loginResp.status()})`).toBeTruthy();
}

async function createGuestPayLaterBooking(guestCtx: APIRequestContext, email: string, tag: string) {
	const processResp = await guestCtx.post("/api/method/buzz.api.process_booking", {
		headers: { "Content-Type": "application/json" },
		data: {
			event: EVENT_NAME,
			guest_email: email,
			guest_full_name: `Playwright Payrexx ${tag}`,
			is_offline: true,
			offline_payment_method: OFFLINE_METHOD,
			attendees: [
				{
					first_name: "Playwright",
					last_name: `Payrexx ${tag}`,
					email,
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
	const bookingId =
		processBody?.message?.booking_name ||
		processBody?.message?.booking_id ||
		processBody?.message?.name ||
		processBody?.message;
	expect(typeof bookingId === "string" && bookingId.startsWith("B")).toBeTruthy();
	return bookingId as string;
}

async function waitForSubmittedBooking(adminCtx: APIRequestContext, bookingId: string) {
	let booking: Record<string, unknown> | null = null;
	const deadline = Date.now() + 30_000;
	while (Date.now() < deadline) {
		const response = await adminCtx.get(`/api/resource/Event Booking/${encodeURIComponent(bookingId)}`);
		if (response.ok()) {
			booking = (await response.json()).data;
			if (booking?.docstatus === 1 && booking?.sales_invoice) break;
		}
		await new Promise((resolve) => setTimeout(resolve, 1000));
	}
	expect(booking, `booking ${bookingId} not readable after process_booking`).not.toBeNull();
	expect(booking?.docstatus, `booking ${bookingId} should auto-submit`).toBe(1);
	return booking as { sales_invoice?: string };
}

async function waitForInvoiceEmail(adminCtx: APIRequestContext, invoiceName: string) {
	const deadline = Date.now() + 20_000;
	while (Date.now() < deadline) {
		const rows = await callMethod(adminCtx, "frappe.client.get_list", {
			doctype: "Email Queue",
			filters: { reference_doctype: "Sales Invoice", reference_name: invoiceName },
			fields: ["name"],
			limit_page_length: 1,
			order_by: "creation desc",
		});
		if (Array.isArray(rows) && rows.length > 0) return (rows as { name: string }[])[0].name;
		await new Promise((resolve) => setTimeout(resolve, 1000));
	}
	throw new Error(`Timed out waiting for invoice email for ${invoiceName}`);
}

async function waitForPayrexxResult(page) {
	const deadline = Date.now() + 45_000;
	while (Date.now() < deadline) {
		const bodyText = await page.locator("body").innerText().catch(() => "");
		if (
			page.url().includes("/payment-success") ||
			page.url().includes("result=1") ||
			bodyText.includes("Die Zahlung wird verarbeitet") ||
			bodyText.includes("Zahlung erfolgreich")
		) {
			return true;
		}
		await page.waitForTimeout(1000);
	}
	return false;
}

function decodeMime(message: string) {
	return message
		.replace(/=\r\n/g, "")
		.replace(/=\n/g, "")
		.replace(/=3D/g, "=")
		.replace(/&amp;/g, "&");
}

function extractPayUrl(decodedMessage: string) {
	return decodedMessage.match(/href="([^"]*pay_invoice[^"]*)"/)?.[1]?.replace(/&amp;/g, "&") || "";
}
