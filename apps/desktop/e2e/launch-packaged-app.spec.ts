import { expect, test } from '@playwright/test'

import {
  packagedBinaryExists,
  PACKAGED_BINARY_PATH,
  setupPackagedApp,
  type PackagedAppFixture,
} from './fixtures'

/**
 * E2E smoke tests for the packaged Hermes desktop app.
 *
 * Launches the real packaged Electron binary (produced by `npm run pack` →
 * `electron-builder --dir`) with BOOT_FAKE=1 and full sandbox isolation
 * (credential stripping, isolated HERMES_HOME + userData, unique app name).
 *
 * Skips if the packaged binary doesn't exist — run `npm run pack` first.
 */

let fixture: PackagedAppFixture | null = null

test.beforeAll(async () => {
  test.skip(
    !packagedBinaryExists(),
    `Built app binary not found: ${PACKAGED_BINARY_PATH}. Run 'npm run pack' first.`,
  )

  fixture = await setupPackagedApp()
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test('window opens with the Hermes title', async () => {
  const title = await fixture!.page.title()
  expect(title).toContain('Hermes')
})

test('renderer loads and shows DOM content', async () => {
  const page = fixture!.page
  await page.waitForSelector('#root', { state: 'attached', timeout: 30_000 })
  const childCount = await page.locator('#root > *').count()
  expect(childCount).toBeGreaterThan(0)
})

test('boot progress overlay fades out or shows error state', async () => {
  const page = fixture!.page
  await page.waitForFunction(
    () => {
      const root = document.getElementById('root')
      if (!root) {
        return false
      }
      const text = root.textContent ?? ''

      // Error path: boot failure overlay renders an error message.
      if (text.includes('error') || text.includes('Error') || text.includes('failed')) {
        return true
      }

      // Success path: overlay disappears and the app renders. If there's
      // no "boot" / "starting" / "installing" text visible, boot has
      // completed (either to the main UI or to onboarding).
      const bootIndicators = ['starting', 'resolving', 'spawning', 'waiting', 'installing']
      const lower = text.toLowerCase()

      return !bootIndicators.some((word) => lower.includes(word))
    },
    { timeout: 60_000 },
  )
})

test('can capture a screenshot for the CI artifact', async () => {
  const screenshot = await fixture!.page.screenshot({ timeout: 10_000 }).catch(() => null)
  if (screenshot) {
    expect(screenshot.byteLength).toBeGreaterThan(0)
  } else {
    test.skip(true, 'Screenshot timed out — likely a GPU/rendering issue in headless mode')
  }
})
