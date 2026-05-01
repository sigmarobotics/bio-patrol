const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.BASE_URL || 'http://localhost:8001';

test.describe('bedGrid pure functions (via page.evaluate)', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(`${BASE_URL}/`);
  });

  test('classifyBedState — Valid (recent + is_valid)', async ({ page }) => {
    const result = await page.evaluate(() => {
      const now = new Date('2026-05-01T12:00:00Z').getTime();
      return window.bedGrid.classifyBedState(
        { bed_key: '101-1' },
        { is_valid: true, timestamp: '2026-05-01T11:55:00Z' },
        true, 24, now
      );
    });
    expect(result).toBe('valid');
  });

  test('classifyBedState — Stale (>= staleHours since last valid)', async ({ page }) => {
    const result = await page.evaluate(() => {
      const now = new Date('2026-05-02T13:00:00Z').getTime();
      return window.bedGrid.classifyBedState(
        { bed_key: '101-1' },
        { is_valid: true, timestamp: '2026-05-01T11:55:00Z' },
        true, 24, now
      );
    });
    expect(result).toBe('stale');
  });

  test('classifyBedState — Invalid (last record is_valid=false)', async ({ page }) => {
    const result = await page.evaluate(() => {
      const now = new Date('2026-05-01T12:00:00Z').getTime();
      return window.bedGrid.classifyBedState(
        { bed_key: '101-2' },
        { is_valid: false, timestamp: '2026-05-01T11:55:00Z' },
        true, 24, now
      );
    });
    expect(result).toBe('invalid');
  });

  test('classifyBedState — Unscheduled (regardless of latest)', async ({ page }) => {
    const result = await page.evaluate(() => {
      const now = new Date('2026-05-01T12:00:00Z').getTime();
      return window.bedGrid.classifyBedState(
        { bed_key: '101-3' },
        { is_valid: true, timestamp: '2026-05-01T11:55:00Z' },
        false, 24, now
      );
    });
    expect(result).toBe('unscheduled');
  });

  test('classifyBedState — never scanned → Stale', async ({ page }) => {
    const result = await page.evaluate(() => {
      return window.bedGrid.classifyBedState(
        { bed_key: '101-1' }, null, true, 24, Date.now()
      );
    });
    expect(result).toBe('stale');
  });
});

test.describe('formatRelativeTime', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(`${BASE_URL}/`);
  });

  test('< 1 minute → 剛剛', async ({ page }) => {
    const r = await page.evaluate(() => window.bedGrid.formatRelativeTime(
      '2026-05-01T11:59:30Z', new Date('2026-05-01T12:00:00Z').getTime()));
    expect(r).toBe('剛剛');
  });
  test('5 min → 5 分前', async ({ page }) => {
    const r = await page.evaluate(() => window.bedGrid.formatRelativeTime(
      '2026-05-01T11:55:00Z', new Date('2026-05-01T12:00:00Z').getTime()));
    expect(r).toBe('5 分前');
  });
  test('59 min → 59 分前', async ({ page }) => {
    const r = await page.evaluate(() => window.bedGrid.formatRelativeTime(
      '2026-05-01T11:01:00Z', new Date('2026-05-01T12:00:00Z').getTime()));
    expect(r).toBe('59 分前');
  });
  test('60 min → 1 小時前', async ({ page }) => {
    const r = await page.evaluate(() => window.bedGrid.formatRelativeTime(
      '2026-05-01T11:00:00Z', new Date('2026-05-01T12:00:00Z').getTime()));
    expect(r).toBe('1 小時前');
  });
  test('25 hours → 1 天前', async ({ page }) => {
    const r = await page.evaluate(() => window.bedGrid.formatRelativeTime(
      '2026-04-30T11:00:00Z', new Date('2026-05-01T12:00:00Z').getTime()));
    expect(r).toBe('1 天前');
  });
});
