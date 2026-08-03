import { Page, APIRequestContext, expect } from "@playwright/test";

/** Navigate straight to a Desk form and wait for it to render. */
export async function gotoDesk(page: Page, doctype: string, name: string) {
	// Frappe 16 serves the Desk SPA at /desk/<route> (/app/<route> 301s there).
	// Never land on the bare /desk root: its workspace redirect can loop on a
	// fresh CI site, and any page.evaluate/waitForFunction handle held across
	// that window dies with "Execution context was destroyed". Deep-link the
	// form route directly and wait with locators only — locator waits survive
	// SPA re-navigations.
	const slug = doctype.toLowerCase().replace(/ /g, "-");
	await page.goto(`/desk/${slug}/${encodeURIComponent(name)}`, {
		waitUntil: "domcontentloaded",
	});
	await expect(page.locator(".form-layout").first()).toBeVisible({ timeout: 30_000 });
}

/** Convenience GET against an API endpoint using the authed context. */
export async function apiGet(api: APIRequestContext, path: string) {
	const r = await api.get(path);
	expect(r.status(), `${path} returned ${r.status()}`).toBeLessThan(500);
	return r;
}

/**
 * Issue a request to a Frappe whitelisted method via /api/method/...
 * Returns the JSON-decoded ``message`` field if the call succeeded.
 */
export async function callMethod(
	api: APIRequestContext,
	method: string,
	params: Record<string, unknown> = {},
) {
	const r = await api.post(`/api/method/${method}`, { form: stringifyForm(params) });
	const body = await r.json().catch(() => ({}));
	expect(r.ok(), `${method} -> ${r.status()}: ${JSON.stringify(body)}`).toBeTruthy();
	return body.message;
}

/** Read a single doctype row by name. */
export async function getDoc(api: APIRequestContext, doctype: string, name: string) {
	const r = await api.get(`/api/resource/${encodeURIComponent(doctype)}/${encodeURIComponent(name)}`);
	if (r.status() === 404) return null;
	const body = await r.json();
	return body.data;
}

/** Delete a row, ignoring 404. */
export async function deleteDoc(api: APIRequestContext, doctype: string, name: string) {
	const r = await api.delete(
		`/api/resource/${encodeURIComponent(doctype)}/${encodeURIComponent(name)}`,
	);
	if (r.status() !== 404 && !r.ok()) {
		throw new Error(`delete ${doctype}/${name} -> ${r.status()}`);
	}
}

function stringifyForm(params: Record<string, unknown>): Record<string, string> {
	const out: Record<string, string> = {};
	for (const [k, v] of Object.entries(params)) {
		out[k] = typeof v === "string" ? v : JSON.stringify(v);
	}
	return out;
}
