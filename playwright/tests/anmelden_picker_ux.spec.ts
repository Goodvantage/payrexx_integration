import { test, expect } from "@playwright/test";

/**
 * Legacy Buzz regression for the /anmelden picker UX:
 *   1. Picking "Im Namen einer Trägerschaft" must reveal exactly ONE
 *      visible search input (the hidden ea_customer field stays hidden).
 *   2. The typeahead dropdown must FLOAT above neighbouring content —
 *      it must not push the submit button down when results render.
 *
 * Runs as Guest (no admin login needed for this public route).
 */

const RUN_LEGACY_BUZZ_E2E = process.env.RUN_LEGACY_BUZZ_E2E === "1";
const SLUG = process.env.TEST_GUEST_EVENT_SLUG || "notfallkurs-f%C3%BCr-kinder";

test.use({ storageState: { cookies: [], origins: [] } });

test.describe("/anmelden picker UX", () => {
	test.skip(!RUN_LEGACY_BUZZ_E2E, "Set RUN_LEGACY_BUZZ_E2E=1 to run retired Buzz /anmelden specs.");

	test("Trägerschaft mode: exactly one visible input + floating typeahead", async ({ page }) => {
		await page.goto(`/anmelden/${SLUG}`);
		await page.waitForLoadState("networkidle");

		// Click the "Im Namen einer Trägerschaft" radio
		await page.locator('input[name="ea_mode"][value="traegerschaft"]').check();

		// (a) Exactly ONE visible <input>/<textarea> in the customer block
		//     (mode radios live in a sibling fieldset).
		const customerBlock = page.locator(".ea-anmelden-customer-block");
		await expect(customerBlock).toBeVisible();
		const visibleFields = customerBlock.locator("input:visible, textarea:visible");
		await expect(
			visibleFields,
			"the customer block should expose exactly one visible field — the search input",
		).toHaveCount(1);

		// (b) Snapshot the submit button's Y position BEFORE the typeahead opens.
		const submitBtn = page.locator('form#ea-anmelden-form button[type="submit"]');
		const beforeBox = await submitBtn.boundingBox();
		expect(beforeBox, "submit button must be in the layout").not.toBeNull();

		// Trigger the typeahead to render results.
		const search = customerBlock.locator("#ea-customer-search");
		await search.click();
		await search.fill("a");
		// Either rows render or the empty state — both reveal the panel.
		const typeahead = customerBlock.locator("#ea-typeahead");
		await expect(typeahead).toBeVisible({ timeout: 5_000 });

		// (c) Submit button must NOT have shifted down — typeahead is floating.
		const afterBox = await submitBtn.boundingBox();
		expect(afterBox, "submit button must still be in the layout").not.toBeNull();
		const drift = (afterBox?.y ?? 0) - (beforeBox?.y ?? 0);
		expect(
			Math.abs(drift),
			`submit button must not move when typeahead opens (drift=${drift}px)`,
		).toBeLessThan(8);

		// (d) Typeahead is z-index'd above the form/page — its rect must
		//     overlap the rect immediately below the search field.
		const taBox = await typeahead.boundingBox();
		expect(taBox, "typeahead must have a layout box").not.toBeNull();
		const searchBox = await search.boundingBox();
		expect(taBox!.y).toBeGreaterThanOrEqual(searchBox!.y);

		// (e) Pick the first row → selected chip renders with the new
		//     uppercase + ✕ button, search input is hidden (regression for
		//     the chip restyle + the [hidden]+display:flex specificity bug).
		const firstRow = customerBlock.locator(".ea-typeahead-row").first();
		if (await firstRow.count()) {
			await firstRow.click();
			await expect(customerBlock.locator(".ea-anmelden-selected")).toBeVisible();
			await expect(
				customerBlock.locator("#ea-customer-search"),
				"search input must be hidden after a Trägerschaft is picked",
			).toBeHidden();
			const clearBtn = customerBlock.locator("button.ea-anmelden-clear");
			await expect(clearBtn).toBeVisible();
			// Label is uppercased via CSS, content stays as authored.
			const transform = await clearBtn.evaluate((el) => getComputedStyle(el).textTransform);
			expect(transform).toBe("uppercase");
			// ✕ icon is the LAST visual child, so it sits on the right.
			const xPos = await customerBlock
				.locator(".ea-anmelden-clear-x")
				.evaluate((el) => el.getBoundingClientRect().x);
			const labelText = clearBtn.locator("span").first();
			const labelPos = await labelText.evaluate((el) => el.getBoundingClientRect().x);
			expect(xPos, "× must be to the right of the text label").toBeGreaterThan(labelPos);
		}
	});
});
