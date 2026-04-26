import { defineConfig, devices } from "@playwright/test";

// Default to http://localhost:8000 — works because the bench has a single
// site and Frappe routes the host-less request to it. Override with
// PLAYWRIGHT_BASE_URL when you have a public hostname (e.g. ngrok).
const baseURL = process.env.PLAYWRIGHT_BASE_URL || "http://localhost:8000";

export default defineConfig({
	testDir: "./tests",
	timeout: 60_000,
	expect: { timeout: 10_000 },
	fullyParallel: false,
	workers: 1,
	reporter: [["list"], ["html", { open: "never" }]],
	globalSetup: "./tests/helpers/global-setup.ts",
	use: {
		baseURL,
		trace: "retain-on-failure",
		screenshot: "only-on-failure",
		video: "retain-on-failure",
		ignoreHTTPSErrors: true,
		storageState: "auth.json",
	},
	projects: [
		{
			name: "chromium",
			use: { ...devices["Desktop Chrome"] },
		},
	],
});
