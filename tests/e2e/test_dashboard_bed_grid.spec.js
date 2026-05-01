// IT-9 Slice 4 — full dashboard bed-grid E2E.
// Pre-seeded by tests/e2e/seed_e2e.py (beds + patrol + sensor rows for 4 states).
// Uvicorn must already be running on BASE_URL (default http://localhost:8001).

const { test, expect } = require('@playwright/test');
const path = require('path');
const fs = require('fs');

const BASE_URL = process.env.BASE_URL || 'http://localhost:8001';
const SCREENSHOT_DIR = path.join(__dirname, 'screenshots');

test.beforeAll(() => {
  if (!fs.existsSync(SCREENSHOT_DIR)) fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
});

test.describe('IT-9 dashboard bed-grid E2E', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(`${BASE_URL}/`);
    await page.waitForSelector('#bed-grid-main', { timeout: 5000 });
    // bedGrid.init() runs on DOMContentLoaded — wait for at least one card
    await page.waitForSelector('.bed-card', { timeout: 10_000 });
  });

  test('FEAT-012: dashboard renders bed cards with all 4 states', async ({ page }) => {
    const counts = await page.evaluate(() => ({
      valid: document.querySelectorAll('.bed-card--valid').length,
      stale: document.querySelectorAll('.bed-card--stale').length,
      invalid: document.querySelectorAll('.bed-card--invalid').length,
      unscheduled: document.querySelectorAll('.bed-card--unscheduled').length,
    }));
    // Seed gives us exactly one of each.
    expect(counts.valid, 'valid bed').toBeGreaterThan(0);
    expect(counts.stale, 'stale bed').toBeGreaterThan(0);
    expect(counts.invalid, 'invalid bed').toBeGreaterThan(0);
    expect(counts.unscheduled, 'unscheduled bed').toBeGreaterThan(0);
    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'it9-dashboard-overview.png'), fullPage: true });
  });

  test('Room collapse/expand', async ({ page }) => {
    const firstRoom = page.locator('.room-section').first();
    const cardsBefore = await firstRoom.locator('.bed-card').count();
    expect(cardsBefore).toBeGreaterThan(0);
    await firstRoom.locator('.room-header').click();
    await expect(firstRoom).toHaveClass(/room-section--collapsed/);
    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'it9-room-collapsed.png') });
    await firstRoom.locator('.room-header').click();
    await expect(firstRoom).not.toHaveClass(/room-section--collapsed/);
  });

  test('FEAT-013: robot frame click opens modal; ESC closes', async ({ page }) => {
    await page.locator('#robot-frame .frame-header').click();
    const modal = page.locator('#robot-modal');
    await expect(modal).toBeVisible();
    await expect(page.locator('#map-canvas')).toBeVisible();
    await expect(page.locator('#modal-btn-home')).toBeVisible();
    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'it9-robot-modal.png') });
    await page.keyboard.press('Escape');
    await expect(modal).toBeHidden();
  });

  test('Bed card click opens drawer with history', async ({ page }) => {
    // Click a non-unscheduled bed (which has location_id + history) — pick first valid card.
    const card = page.locator('.bed-card--valid').first();
    await card.click();
    const drawer = page.locator('#bed-drawer');
    await expect(drawer).toBeVisible();
    await expect(page.locator('#bed-drawer-title')).toContainText('Bed');
    await page.waitForSelector('#bed-drawer-body .drawer-row, #bed-drawer-body p', { timeout: 5000 });
    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'it9-bed-drawer.png') });
    await page.locator('#bed-drawer-close').click();
    await expect(drawer).toBeHidden();
  });

  test('Settings stale_hours field exists + persists', async ({ page }) => {
    await page.locator('button[data-tab="settings"]').click();
    await page.locator('button[data-settings-subtab="notifications"]').click();
    const field = page.locator('#setting-bed-card-stale-hours');
    await expect(field).toBeVisible();
    await field.fill('48');
    // No id on the save button — select by visible text inside settings panel.
    page.once('dialog', d => d.accept());  // saveSettings may alert on success
    await page.locator('button.btn-premium', { hasText: 'Save Settings' }).click();
    await page.waitForTimeout(500);
    await page.reload();
    await page.locator('button[data-tab="settings"]').click();
    await page.locator('button[data-settings-subtab="notifications"]').click();
    await expect(page.locator('#setting-bed-card-stale-hours')).toHaveValue('48');
    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'it9-settings-stale-hours.png') });
    // Restore default
    await page.locator('#setting-bed-card-stale-hours').fill('24');
    page.once('dialog', d => d.accept());
    await page.locator('button.btn-premium', { hasText: 'Save Settings' }).click();
  });

  test('CORNER-022: 5xx response keeps bed grid intact', async ({ page }) => {
    const before = await page.locator('.bed-card').count();
    expect(before).toBeGreaterThan(0);
    // Intercept future polls with 500
    await page.route('**/api/bio-sensor/latest-by-bed', (route) =>
      route.fulfill({ status: 500, contentType: 'application/json', body: '{"status":"error","message":"simulated"}' }),
    );
    // bedGrid polls every 10s; wait one cycle + buffer
    await page.waitForTimeout(11_000);
    const after = await page.locator('.bed-card').count();
    expect(after).toBe(before);
  });
});

test.describe('IT-9 listener-leak + clock-skew (own page contexts)', () => {
  test('FEAT-013 listener-leak: modal open/close 5× — no keydown listener growth', async ({ browser }) => {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await page.addInitScript(() => {
      window.__listenerCounts = { keydown: 0 };
      const orig = document.addEventListener.bind(document);
      const origRm = document.removeEventListener.bind(document);
      document.addEventListener = (type, ...rest) => {
        if (type === 'keydown') window.__listenerCounts.keydown += 1;
        return orig(type, ...rest);
      };
      document.removeEventListener = (type, ...rest) => {
        if (type === 'keydown') window.__listenerCounts.keydown -= 1;
        return origRm(type, ...rest);
      };
    });
    await page.goto(`${BASE_URL}/`);
    await page.waitForSelector('#robot-frame');
    await page.waitForSelector('.bed-card', { timeout: 10_000 });
    const baseline = await page.evaluate(() => window.__listenerCounts.keydown);
    const modalLoc = page.locator('#robot-modal');
    for (let i = 0; i < 5; i++) {
      await page.locator('#robot-frame .frame-header').click();
      await expect(modalLoc).toBeVisible();
      await page.keyboard.press('Escape');
      await expect(modalLoc).toBeHidden();
    }
    const final = await page.evaluate(() => window.__listenerCounts.keydown);
    expect(final, `baseline=${baseline} final=${final} (should match — modal must remove keydown on close)`)
      .toBe(baseline);
    await ctx.close();
  });

  test('CORNER-024: clock skew — future timestamp clamps to "剛剛"', async ({ page }) => {
    await page.goto(`${BASE_URL}/`);
    await page.waitForFunction(() => window.bedGrid && typeof window.bedGrid.formatRelativeTime === 'function');
    const formatted = await page.evaluate(() =>
      window.bedGrid.formatRelativeTime(
        new Date(Date.now() + 60_000).toISOString(),
        Date.now(),
      ),
    );
    expect(formatted).toBe('剛剛');
  });
});
