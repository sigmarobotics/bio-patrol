/**
 * IT-9 post-deploy: verify mapView handles size-diverse maps correctly.
 *
 * Picks 3 maps spanning the available size range (smallest / median / largest)
 * and switches between them via the display-only /api/maps/active endpoint.
 * For each map, captures the mini frame + modal screenshot so reviewers can
 * eyeball that:
 *   - The auto-fit keeps the entire map inside the mini canvas regardless of
 *     native dimensions.
 *   - The robot icon stays at MIN_ICON_SCREEN_SCALE (16x10 px) on the mini
 *     auto-fit, regardless of how small/large the underlying map is.
 *   - The modal opens correctly for each map.
 *
 * Test isolation: saves the current active_map at start, restores at end.
 */
const { test, expect } = require('@playwright/test');
const path = require('path');
const fs = require('fs');

const BASE_URL = process.env.BASE_URL || 'http://localhost:8001';
const SCREENSHOT_DIR = path.join(__dirname, 'screenshots');

if (!fs.existsSync(SCREENSHOT_DIR)) fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });

let originalActiveMap = '';
let pickedMaps = [];

test.describe.configure({ mode: 'serial' });

test.describe('IT-9 post-deploy: map-switch with size-diverse maps', () => {
  test.beforeAll(async ({ request }) => {
    const listRes = await request.get(`${BASE_URL}/api/maps`);
    expect(listRes.ok(), 'GET /api/maps should succeed').toBeTruthy();
    const list = await listRes.json();
    originalActiveMap = list.active_map || '';

    const withSize = (list.maps || []).filter(m => m.width && m.height);
    if (withSize.length < 2) {
      test.skip(true, `Only ${withSize.length} maps with dimensions — need at least 2 for size-diversity test`);
      return;
    }

    withSize.sort((a, b) => (a.width * a.height) - (b.width * b.height));
    if (withSize.length >= 3) {
      pickedMaps = [
        { ...withSize[0], label: 'smallest' },
        { ...withSize[Math.floor(withSize.length / 2)], label: 'median' },
        { ...withSize[withSize.length - 1], label: 'largest' },
      ];
    } else {
      pickedMaps = [
        { ...withSize[0], label: 'smaller' },
        { ...withSize[withSize.length - 1], label: 'larger' },
      ];
    }
    console.log('Picked maps:', pickedMaps.map(m => `${m.label}: ${m.name} ${m.width}x${m.height}`).join(' | '));
  });

  test.afterAll(async ({ request }) => {
    if (originalActiveMap) {
      await request.post(`${BASE_URL}/api/maps/active`, { data: { map_id: originalActiveMap } });
    }
  });

  for (let i = 0; i < 3; i++) {
    test(`map ${i + 1}/3 — auto-fit + clamped icon`, async ({ page, request }) => {
      if (!pickedMaps[i]) test.skip(true, 'No map for this slot');
      const mapInfo = pickedMaps[i];

      const setRes = await request.post(`${BASE_URL}/api/maps/active`, { data: { map_id: mapInfo.id } });
      expect(setRes.ok(), 'POST /api/maps/active should succeed').toBeTruthy();

      await page.goto(`${BASE_URL}/`);
      await page.waitForFunction(() => window.mapView?.getState?.().img !== null, { timeout: 10000 });
      await page.waitForTimeout(400);

      const state = await page.evaluate(() => {
        const st = window.mapView.getState();
        const miniCanvas = document.getElementById('robot-mini-canvas');
        return {
          imgW: st.img?.naturalWidth,
          imgH: st.img?.naturalHeight,
          gMapDescW: st.gMapDesc.w,
          gMapDescH: st.gMapDesc.h,
          miniCanvasW: miniCanvas?.width,
          miniCanvasH: miniCanvas?.height,
        };
      });

      expect(state.imgW, `image width matches map metadata (${mapInfo.label})`).toBe(mapInfo.width);
      expect(state.imgH, `image height matches map metadata (${mapInfo.label})`).toBe(mapInfo.height);
      expect(state.gMapDescW).toBe(mapInfo.width);
      expect(state.gMapDescH).toBe(mapInfo.height);
      expect(state.miniCanvasW).toBeGreaterThan(0);
      expect(state.miniCanvasH).toBeGreaterThan(0);

      const miniSlug = `it9-mapswitch-mini-${String(i + 1).padStart(2, '0')}-${mapInfo.label}-${mapInfo.width}x${mapInfo.height}.png`;
      await page.locator('#robot-frame').screenshot({ path: path.join(SCREENSHOT_DIR, miniSlug) });

      await page.locator('#robot-frame').click();
      await expect(page.locator('#robot-modal')).toBeVisible();
      await page.waitForTimeout(300);
      const modalSlug = `it9-mapswitch-modal-${String(i + 1).padStart(2, '0')}-${mapInfo.label}-${mapInfo.width}x${mapInfo.height}.png`;
      await page.locator('.robot-modal-map').screenshot({ path: path.join(SCREENSHOT_DIR, modalSlug) });
      await page.keyboard.press('Escape');
      await expect(page.locator('#robot-modal')).toBeHidden();
    });
  }
});
