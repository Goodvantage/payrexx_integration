import { request, FullConfig } from "@playwright/test";

/**
 * Logs in as Administrator (or whatever credentials are in env) once and
 * persists the session cookies to ``auth.json``. Every spec then reuses that
 * state via ``storageState: 'auth.json'`` from playwright.config.ts.
 */
export default async function globalSetup(config: FullConfig) {
	const baseURL =
		(config.projects[0]?.use?.baseURL as string | undefined) ||
		process.env.PLAYWRIGHT_BASE_URL ||
		"http://localhost:8000";
	const usr = process.env.FRAPPE_USERNAME || "Administrator";
	const pwd = process.env.FRAPPE_PASSWORD || "admin";

	const ctx = await request.newContext({ baseURL, ignoreHTTPSErrors: true });
	const res = await ctx.post("/api/method/login", { form: { usr, pwd } });
	if (!res.ok()) {
		throw new Error(
			`Login failed (${res.status()}). Check PLAYWRIGHT_BASE_URL / FRAPPE_USERNAME / FRAPPE_PASSWORD env vars.`,
		);
	}
	await ctx.storageState({ path: "auth.json" });
	await ctx.dispose();
}
