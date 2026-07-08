/**
 * Bundled-plugin discovery. Every `src/plugins/<name>/plugin.{ts,tsx}` that
 * default-exports a `HermesPlugin` is loaded and registered automatically —
 * drop a folder in, it shows up. No import list, no registry edit; the
 * publishing shape a third-party plugin will mirror through the runtime loader.
 */

import { createPluginContext, type HermesPlugin } from './plugin'

const modules = import.meta.glob<{ default: HermesPlugin }>('../plugins/*/plugin.{ts,tsx}', { eager: true })

// Registry.register replaces by id, so re-running (HMR) is naturally idempotent.
let loaded = false

export function discoverBundledPlugins(): void {
  if (loaded) {
    return
  }

  loaded = true

  for (const [path, mod] of Object.entries(modules)) {
    const plugin = mod.default

    if (!plugin?.id || typeof plugin.register !== 'function') {
      console.warn(`[plugins] ${path} has no valid default HermesPlugin export — skipped`)

      continue
    }

    try {
      plugin.register(createPluginContext(plugin.id))
    } catch (error) {
      console.error(`[plugins] ${plugin.id} failed to register`, error)
    }
  }
}
