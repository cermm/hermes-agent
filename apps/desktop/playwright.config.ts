import { defineConfig } from '@playwright/test'

export default defineConfig({
  /* Test files live under e2e/ so they never collide with the vitest suite
   * under src/ or the node:test files under electron/. */
  testDir: './e2e',
  /* The desktop app can take a while to bootstrap on cold CI runners — 90 s
   * per test gives us headroom without masking real hangs. */
  timeout: 90_000,
  retries: process.env.CI ? 1 : 0,
  /* Each test gets its own worker so the Electron process is fully isolated. */
  fullyParallel: false,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  use: {
    screenshot: 'on',
    trace: 'on',
  },
})