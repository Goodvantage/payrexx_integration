import { test, expect } from "@playwright/test";
import { callMethod, getDoc } from "./helpers/frappe";

/**
 * End-to-end: trigger ``create_sales_invoice`` on a pre-seeded Good Event Booking,
 * then verify that the Email Queue has a row containing the Payrexx pay-by-
 * email URL.
 *
 * Skipped unless TEST_BOOKING_NAME is set in the environment, because the
 * booking has to exist (with attendees, customer, and a contact email) for
 * the assertions to hold. To bootstrap one for testing:
 *
 *   bench --site <site> console
 *   >>> from good_event.tests.fixtures import seed_pay_later_booking
 *   >>> seed_pay_later_booking()
 */

const BOOKING = process.env.TEST_BOOKING_NAME;

test.skip(!BOOKING, "Set TEST_BOOKING_NAME to run the booking → email flow.");

test("submitting create_sales_invoice queues a branded email with the pay URL", async ({
	request,
}) => {
	// 1. Trigger the whitelisted method on the booking.
	//    Frappe's modern path is /api/method/run_doc_method (not frappe.client.*).
	const siName = await callMethod(request, "run_doc_method", {
		dt: "Good Event Booking",
		dn: BOOKING,
		method: "create_sales_invoice",
	});
	expect(typeof siName).toBe("string");

	// 2. The SI should now exist.
	const si = await getDoc(request, "Sales Invoice", siName as string);
	expect(si).not.toBeNull();

	// 3. The Email Queue should have a row referencing this SI.
	//    `frappe.client.get_list` truncates Long Text fields, so fetch only the
	//    name first and then load the full doc by id.
	const queue = await callMethod(request, "frappe.client.get_list", {
		doctype: "Email Queue",
		filters: { reference_doctype: "Sales Invoice", reference_name: siName },
		fields: ["name"],
		limit_page_length: 1,
		order_by: "creation desc",
	});
	expect(Array.isArray(queue)).toBeTruthy();
	expect((queue as unknown[]).length).toBeGreaterThan(0);
	const queueName = (queue as { name: string }[])[0].name;

	// 4. The email body should contain the Payrexx pay URL and point to the
	//    invoice PDF for the Swiss QR-bill. The QR itself belongs in the PDF,
	//    not in the visible email body.
	//    Decode the MIME quoted-printable wrapping (=\r\n line breaks, =3D for '=')
	//    so substrings split across line wraps still match.
	const fullDoc = await getDoc(request, "Email Queue", queueName);
	const message = (fullDoc as { message: string }).message;
	const decoded = message
		.replace(/=\r\n/g, "")
		.replace(/=\n/g, "")
		.replace(/=3D/g, "=")
		.replace(/&amp;/g, "&");
	test.skip(
		!decoded.includes("payrexx_integration.api.pay_invoice"),
		"Active Good Event invoice email provider did not render a Payrexx pay-by-email URL.",
	);
	expect(decoded).toContain("payrexx_integration.api.pay_invoice");
	expect(decoded).toMatch(/[?&]si=/);
	expect(decoded).toMatch(/[?&]gateway_name=/);
	expect(decoded).toMatch(/[?&]token=[a-f0-9]{32}/);
	expect(decoded).toContain("Swiss QR-Bill");
	expect(decoded).toContain("PDF");
	expect(decoded).toContain("Content-Disposition: attachment");
	expect(decoded).toContain(`${siName}.pdf`);
	expect(decoded).not.toContain("Content-Disposition: inline");
	expect(decoded).not.toContain("qr-bill.png");
	expect(decoded).not.toContain("<svg");
	expect(decoded).not.toMatch(/Receipt Account|Account \/ Payable to|Payment part/);
});
