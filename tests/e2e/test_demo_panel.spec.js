// IT-21 — /demo full-screen synthetic-data panel.
// Pre-seeded by tests/e2e/seed_demo_e2e.py: one finished 3-seat demo run
// (S01 normal, S02 abnormal, S03 absent) driven through the real TaskEngine,
// plus its 30-day synthetic history. The seed's robot IP is non-routable, so
// the robot reads as disconnected. Uvicorn must already be running on BASE_URL.

const { test, expect } = require('@playwright/test');
const path = require('path');
const fs = require('fs');

const BASE_URL = process.env.BASE_URL || 'http://localhost:8001';
const SCREENSHOT_DIR = path.join(__dirname, 'screenshots');

test.beforeAll(() => {
  if (!fs.existsSync(SCREENSHOT_DIR)) fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
});

// Force the robot connection field of /api/demo/live — everything else is the
// real backend response.
async function forceRobot(page, connected) {
  await page.route('**/api/demo/live', async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    body.robot = { ...body.robot, connected, state: connected ? 'connected' : 'disconnected' };
    await route.fulfill({ response: res, json: body });
  });
}

test.describe('IT-21 /demo panel', () => {
  test.use({ viewport: { width: 1600, height: 900 } });

  test('shows the last demo run: three seat states, alert, preview, watermark', async ({ page }) => {
    await page.goto(`${BASE_URL}/demo`);
    await page.waitForSelector('.seat-card', { timeout: 10_000 });

    await expect(page.locator('.seat-card[data-bed-key="S01"]')).toHaveClass(/seat--normal/);
    await expect(page.locator('.seat-card[data-bed-key="S01"]')).toContainText('正常');
    await expect(page.locator('.seat-card[data-bed-key="S02"]')).toHaveClass(/seat--abnormal/);
    await expect(page.locator('.seat-card[data-bed-key="S02"]')).toContainText('心跳呼吸異常');
    await expect(page.locator('.seat-card[data-bed-key="S03"]')).toHaveClass(/seat--absent/);
    await expect(page.locator('.seat-card[data-bed-key="S03"]')).toContainText('偵測不到人');

    const alert = page.locator('#demo-alert-banner');
    await expect(alert).toBeVisible();
    await expect(alert).toContainText('座位 S02');

    // Telegram-style preview carries the real evaluator output, DEMO-prefixed.
    const bubbles = page.locator('#demo-chat .chat-msg');
    await expect(bubbles).toHaveCount(3);
    await expect(page.locator('#demo-chat')).toContainText('🧪 DEMO・合成資料 ⚠️ S02 心跳呼吸異常');
    await expect(page.locator('#demo-chat')).toContainText('偵測不到人');
    await expect(page.locator('#demo-chat')).toContainText('巡房完成');

    // Idle state: last result + start hint; watermark always on.
    await expect(page.locator('#demo-seats-title')).toHaveText('上一次 Demo 結果');
    await expect(page.locator('#demo-idle-hint')).toHaveText('按下 Demo Run 開始');
    await expect(page.locator('.demo-watermark')).toBeVisible();
    await expect(page.locator('.demo-watermark')).toContainText('DEMO・合成資料');
    await expect(page.locator('#demo-progress-text')).toHaveText('3 / 3');
    await page.waitForTimeout(600); // let the progress bar transition settle

    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'demo-panel.png'), fullPage: true });
  });

  test('has no way into the real dashboard tabs', async ({ page }) => {
    await page.goto(`${BASE_URL}/demo/`);
    await page.waitForSelector('.seat-card', { timeout: 10_000 });
    await expect(page.locator('a[href]')).toHaveCount(0);
    await expect(page.locator('.tab-btn, [data-tab], [onclick*="switchTab"]')).toHaveCount(0);
    for (const label of ['Dashboard', '床位選擇', '位置設定', '歷史紀錄', 'Settings']) {
      await expect(page.getByText(label, { exact: true })).toHaveCount(0);
    }
  });

  test('disconnect banner follows the robot connection', async ({ page }) => {
    // Seeded server: the robot is unreachable → banner on.
    await page.goto(`${BASE_URL}/demo`);
    await page.waitForSelector('.seat-card', { timeout: 10_000 });
    const banner = page.locator('#demo-disconnect-banner');
    await expect(banner).toBeVisible();
    await expect(banner).toHaveText('連線中斷，等待機器人回應');

    // Robot back → banner off at the next 1 s poll.
    await forceRobot(page, true);
    await expect(banner).toBeHidden({ timeout: 5_000 });
  });

  test('seat drawer shows the synthetic trend and averages', async ({ page }) => {
    await forceRobot(page, true);
    await page.goto(`${BASE_URL}/demo`);
    await page.locator('.seat-card[data-bed-key="S02"]').click();
    const drawer = page.locator('#demo-drawer');
    await expect(drawer).toBeVisible();
    await expect(page.locator('#demo-drawer-title')).toHaveText('座位 S02');
    await expect(page.locator('#demo-avg-bpm')).not.toHaveText('--');
    await expect(page.locator('#demo-drawer-body svg')).toHaveCount(2);
    // 30 history days + today's abnormal reading; only today's point is out of band.
    await expect(page.locator('#demo-drawer-body')).toContainText('31 筆有效量測');
    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'demo-panel-drawer.png') });
    await page.keyboard.press('Escape');
    await expect(drawer).toBeHidden();
  });

  test('a running demo shows 量測中 with a countdown', async ({ page }) => {
    // The task store is in-memory, so the in-progress shape is served from a
    // fixture: S01 measuring with 9 s left, S02 waiting.
    await page.route('**/api/demo/live', (route) => route.fulfill({
      json: {
        task_id: 'e2e-running', task_status: 'in_progress', running: true,
        seats: [
          { bed_key: 'S01', scenario: 'normal', state: 'measuring', seconds: 15, remaining_seconds: 9 },
          { bed_key: 'S02', scenario: 'abnormal', state: 'pending', seconds: 15, remaining_seconds: null },
        ],
        progress: { done: 0, total: 2 },
        robot: { state: 'connected', connected: true, offline_pending: false, pose: null },
        thresholds: { hr_low: 50, hr_high: 120, rr_low: 10, rr_high: 30 },
      },
    }));
    await page.goto(`${BASE_URL}/demo`);
    const measuring = page.locator('.seat-card[data-bed-key="S01"]');
    await expect(measuring).toHaveClass(/seat--measuring/);
    await expect(measuring).toContainText(/量測中 \d+ 秒/);
    await expect(page.locator('.seat-card[data-bed-key="S02"]')).toContainText('待量測');
    await expect(page.locator('#demo-idle-hint')).toBeHidden();
    await expect(page.locator('#demo-disconnect-banner')).toBeHidden();
  });
});
