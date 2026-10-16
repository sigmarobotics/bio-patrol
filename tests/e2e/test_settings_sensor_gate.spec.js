/**
 * IT-23: Settings → 硬體設定 shows the four sensor liveness gate fields and they
 * round-trip through Save → reload.
 *
 * WRITES settings.json — run only against a server whose data dir is a
 * throwaway copy, never against a real site.
 */
const { test, expect } = require('@playwright/test');
const path = require('path');
const fs = require('fs');

const BASE_URL = process.env.BASE_URL || 'http://localhost:8001';
const SCREENSHOT_DIR = path.join(__dirname, 'screenshots');
if (!fs.existsSync(SCREENSHOT_DIR)) fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });

const FIELDS = {
  'setting-sensor-precheck-wait-seconds': '45',
  'setting-sensor-precheck-retry-count': '2',
  'setting-sensor-precheck-retry-minutes': '15',
  'setting-sensor-silent-abort-beds': '0',
};

async function openHardwareSettings(page) {
  await page.goto(BASE_URL);
  await page.click('button.tab-btn[data-tab="settings"]');
  await page.click('button[data-settings-subtab="hardware"]');
}

// Opt-in: a bare `npx playwright test` must not rewrite the settings.json of
// whatever server BASE_URL points at. Run with SENSOR_GATE_E2E=1 against a copy.
test.skip(!process.env.SENSOR_GATE_E2E, 'set SENSOR_GATE_E2E=1 to run against a throwaway data dir');

test('sensor gate settings save and read back', async ({ page }) => {
  page.on('dialog', d => d.accept());

  await openHardwareSettings(page);
  for (const id of Object.keys(FIELDS)) {
    await expect(page.locator(`#${id}`)).toBeVisible();
  }
  await expect(page.locator('#setting-sensor-precheck-retry-minutes')).toHaveAttribute('min', '1');
  // loadSettings() fills the form asynchronously; wait for it before typing
  // (GET /api/settings always carries these keys via DEFAULT_SETTINGS).
  await expect(page.locator('#setting-sensor-precheck-wait-seconds')).not.toHaveValue('');

  for (const [id, value] of Object.entries(FIELDS)) {
    await page.fill(`#${id}`, value);
  }
  const saved = page.waitForResponse(r => r.url().endsWith('/api/settings') && r.request().method() === 'POST');
  await page.click('button.btn-premium:has-text("Save Settings")');
  expect((await saved).ok()).toBeTruthy();

  await page.reload();
  await page.click('button.tab-btn[data-tab="settings"]');
  await page.click('button[data-settings-subtab="hardware"]');
  for (const [id, value] of Object.entries(FIELDS)) {
    await expect(page.locator(`#${id}`)).toHaveValue(value);
  }

  const api = await (await page.request.get(`${BASE_URL}/api/settings`)).json();
  expect(api.sensor_precheck_wait_seconds).toBe(45);
  expect(api.sensor_precheck_retry_count).toBe(2);
  expect(api.sensor_precheck_retry_minutes).toBe(15);
  expect(api.sensor_silent_abort_beds).toBe(0);

  const panel = page.locator('.glass-panel', { has: page.locator('#setting-sensor-precheck-wait-seconds') });
  await panel.scrollIntoViewIfNeeded();
  await panel.screenshot({ path: path.join(SCREENSHOT_DIR, 'settings-sensor-gate.png') });
});

// The start request is fully mocked — no patrol is ever created on the server.
test('start patrol button locks while the sensor precheck is in flight', async ({ page }) => {
  let release;
  const gate = new Promise(r => { release = r; });
  await page.route('**/api/patrol/start', async route => {
    await gate;
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: '感測器無回應：30 秒內沒有收到感測墊訊息，請檢查感測棚車上的行動電源是否沒電或沒接' }),
    });
  });
  const dialogs = [];
  page.on('dialog', d => { dialogs.push(d.message()); d.accept(); });

  await page.goto(BASE_URL);
  const btn = page.locator('#btn-start-patrol');
  const original = await btn.textContent();
  await btn.click();
  await expect(btn).toBeDisabled();
  await expect(btn).toHaveText('檢查感測器中…');

  release();
  await expect(btn).toBeEnabled();
  await expect(btn).toHaveText(original);
  expect(dialogs[0]).toContain('感測器無回應');
});
