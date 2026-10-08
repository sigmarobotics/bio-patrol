// IT-21b — /demo is the standard SPA reading the demo DB.
// Pre-seeded by tests/e2e/seed_e2e.py (beds + real sensor rows) and then
// tests/e2e/seed_demo_e2e.py (one finished demo run on 101-1 normal,
// 101-2 abnormal, 102-1 absent, plus 30 days of synthetic history).
// Uvicorn must already be running on BASE_URL.

const { test, expect } = require('@playwright/test');
const path = require('path');
const fs = require('fs');

const BASE_URL = process.env.BASE_URL || 'http://localhost:8001';
const SCREENSHOT_DIR = path.join(__dirname, 'screenshots');

test.beforeAll(() => {
  if (!fs.existsSync(SCREENSHOT_DIR)) fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
});

// The seeded demo run's latest row per bed, straight from the API.
async function demoLatest(request) {
  const res = await request.get(`${BASE_URL}/api/bio-sensor/latest-by-bed`, {
    headers: { 'X-Bio-Data': 'demo' },
  });
  const body = await res.json();
  expect(body.status).toBe('success');
  return Object.fromEntries(body.data.map(r => [r.bed_name, r]));
}

function card(page, bedKey) {
  return page.locator(`.bed-card[data-bed-key="${bedKey}"]`);
}

test.describe('IT-21b /demo = standard SPA on the demo DB', () => {
  test.use({ viewport: { width: 1600, height: 900 } });

  test('bed cards show the synthetic values; no demo marking', async ({ page, request }) => {
    const demo = await demoLatest(request);
    const abnormal = demo['101-2'];
    expect(abnormal.bpm).toBeGreaterThanOrEqual(128);

    await page.goto(`${BASE_URL}/demo`);
    await page.waitForSelector('.bed-card', { timeout: 10_000 });

    await expect(card(page, '101-1')).toContainText(`${demo['101-1'].bpm}/${demo['101-1'].rpm}`);
    await expect(card(page, '101-2')).toContainText(`${abnormal.bpm}/${abnormal.rpm}`);
    await expect(card(page, '101-2')).toHaveClass(/bed-card--abnormal/);
    await expect(card(page, '101-2')).toContainText('心跳呼吸異常');
    await expect(card(page, '102-1')).toHaveClass(/bed-card--invalid/);

    // Pixel-identical to standard mode: the served page IS / (plus the base
    // tag that keeps its relative asset paths at the root), no watermark.
    const root = await (await request.get(`${BASE_URL}/`)).text();
    const demoHtml = await (await request.get(`${BASE_URL}/demo`)).text();
    expect(demoHtml.replace('<head>\n<base href="/">', '<head>')).toBe(root);
    await expect(page.locator('.demo-watermark')).toHaveCount(0);

    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'demo-view.png'), fullPage: true });
  });

  test('/demo/ (trailing slash) loads the same SPA with working assets', async ({ page }) => {
    const failed = [];
    page.on('response', r => { if (r.status() >= 400) failed.push(r.url()); });
    await page.goto(`${BASE_URL}/demo/`);
    await page.waitForSelector('.bed-card', { timeout: 10_000 });
    await expect(page.locator('.tab-btn')).toHaveCount(5);
    expect(failed.filter(u => /\.(css|js)(\?|$)/.test(u))).toEqual([]);
  });

  test('history tab lists the demo runs, one per day', async ({ page }) => {
    await page.goto(`${BASE_URL}/demo`);
    await page.waitForSelector('.bed-card', { timeout: 10_000 });
    await page.locator('.tab-btn[data-tab="sensor"]').click();
    const options = page.locator('#sensor-filter-run option');
    // "最新一輪" + today's demo run + 30 synthetic days.
    await expect(options).toHaveCount(32, { timeout: 10_000 });
    const labels = await options.allTextContents();
    expect(labels.slice(1).every(l => /^\d\d\/\d\d \d\d:\d\d 巡房$/.test(l))).toBe(true);
    await expect(page.locator('#sensor-table-body tr')).toHaveCount(3);
  });

  test('START PATROL starts a demo run', async ({ page }) => {
    let sent = null;
    let header = null;
    await page.route('**/api/patrol/start', async (route) => {
      sent = route.request().postDataJSON();
      header = route.request().headers()['x-bio-data'];
      await route.fulfill({ json: { status: 'ok', task_id: '20261008120000-e2e000', mode: sent.mode } });
    });
    page.on('dialog', d => d.dismiss());
    await page.goto(`${BASE_URL}/demo`);
    await page.waitForSelector('.bed-card', { timeout: 10_000 });
    await page.locator('button', { hasText: 'Start Patrol' }).click();
    await expect.poll(() => sent).not.toBeNull();
    expect(sent.mode).toBe('demo');
    expect(header).toBe('demo');
  });

  test('/ never shows the demo values', async ({ page, request }) => {
    const demo = await demoLatest(request);
    const abnormal = demo['101-2'];
    let header = 'unset';
    page.on('request', r => {
      if (r.url().includes('/api/bio-sensor/latest-by-bed')) header = r.headers()['x-bio-data'];
    });
    await page.goto(`${BASE_URL}/`);
    await page.waitForSelector('.bed-card', { timeout: 10_000 });
    expect(header).toBeUndefined();
    // seed_e2e.py: 101-2 is a failed real scan, 102-1 is stale.
    await expect(card(page, '101-2')).toHaveClass(/bed-card--invalid/);
    await expect(card(page, '101-2')).not.toContainText(`${abnormal.bpm}/${abnormal.rpm}`);
    await expect(card(page, '102-1')).toHaveClass(/bed-card--stale/);
  });
});
