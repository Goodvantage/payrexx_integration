import { Page, APIRequestContext, expect } from "@playwright/test";

/** Navigate to the desk URL and wait for the SPA to settle. */
export async function gotoDesk(page: Page, route: string) {
	// A fresh API-authenticated session can still inherit the website home route.
	await page.goto("/desk");
	await page.waitForLoadState("networkidle");
	await page.goto(route);
	// Frappe is an SPA: shell loads first, then the form is rendered after a
	// few XHRs. Wait until network goes idle so locators have something to bind.
	await page.waitForLoadState("networkidle");
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
