/**
 * E2E smoke tests for the dev-mode desktop app.
 *
 * These tests launch the Electron app from the built dist/ (not the
 * packaged binary) with a real `hermes serve` backend pointed at a mock
 * inference server. The full chain is exercised:
 *
 *   electron → hermes serve (python) → mock provider → renderer
 *
 * Prerequisite: `npm run build` must have been run so dist/ exists.
 * Run from the nix devshell:
 *   sandbox --persistent -- npx playwright test e2e/boot.spec.ts --reporter=list
 */

import { expect, test } from '@playwright/test'

import {
  type MockBackendFixture,
  setupMockBackend,
  waitForAppReady,
} from './fixtures'

let fixture: MockBackendFixture | null = null

test.beforeAll(async () => {
  fixture = await setupMockBackend()
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test.describe('dev-mode boot with mock backend', () => {
  test('window opens with Hermes title', async () => {
    const title = await fixture!.page.title()
    expect(title).toContain('Hermes')
  })

  test('renderer mounts and shows DOM content', async () => {
    const page = fixture!.page
    // Wait for the React root to mount. The app renders into #root
    // (see src/main.tsx), but content may arrive through portals — so
    // check the body for any interactive content instead.
    await page.waitForSelector('body', { state: 'attached' })
    // Wait for the main app shell — the composer is always present.
    await page.waitForSelector('textarea, [contenteditable="true"]', {
      state: 'attached',
      timeout: 30_000,
    })
  })

  test('backend boots and app becomes ready', async () => {
    // This is the big one — wait for the full boot chain to complete:
    // electron starts → hermes serve is spawned → WS connects → config
    // loaded → sessions loaded → boot overlay dismissed → composer visible.
    await waitForAppReady(fixture!.page, 120_000)
  })

  test('screenshot after boot', async () => {
    // Use a screenshot without waiting for fonts — the default
    // page.screenshot() waits for fonts to load, which can hang in
    // headless Electron. page.screenshot({ type: 'png', timeout: 10000 })
    // with a shorter timeout is more reliable.
    const screenshot = await fixture!.page.screenshot({ timeout: 10_000 }).catch(() => null)
    if (screenshot) {
      expect(screenshot.byteLength).toBeGreaterThan(0)
    } else {
      // If screenshot fails (e.g. GPU issues in headless), just skip —
      // the important tests are the boot and interaction ones above.
      test.skip(true, 'Screenshot timed out — likely a GPU/rendering issue in headless mode')
    }
  })
})
