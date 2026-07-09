/**
 * Mirror a reactive list of "tiles" into layout-tree pane contributions:
 * register a pane per tile, refresh its title in place, and dispose panes whose
 * tile is gone. This is the shared bookkeeping — a keyed registry, a wanted-set
 * diff, a one-time pane closer — behind BOTH session tiles and route (page)
 * tiles; each supplies only what differs (key, title, render, close, edge).
 */

import type { ReadableAtom } from 'nanostores'
import type { ReactNode } from 'react'

import { registerPaneCloser, removeTreePane } from '@/components/pane-shell/tree/store'
import { registry } from '@/contrib/registry'
import type { SplitDir } from '@/store/session-states'

export interface PaneMirror<T> {
  /** Reactive source list. */
  source: ReadableAtom<T[]>
  /** Extra atoms whose changes should re-sync (e.g. titles living elsewhere). */
  also?: ReadableAtom<unknown>[]
  /** Stable key + pane-id seed for a tile. */
  key: (tile: T) => string
  /** Pane-id namespace — the id is `${prefix}:${key}`. */
  prefix: string
  /** Edge to dock against main on adoption (default right). */
  dir?: (tile: T) => SplitDir | undefined
  minWidth: string
  title: (key: string) => string
  render: (key: string) => ReactNode
  /** Wired as the pane's closer (tab Close). */
  close: (key: string) => void
}

/** Build a `watch*` fn: syncs once, then re-syncs on every source/also change.
 *  Module-level state lives in the returned closure, so call it once per app. */
export function paneMirror<T>(cfg: PaneMirror<T>): () => void {
  const registered = new Map<string, { dispose: () => void; title: string }>()
  const paneId = (key: string) => `${cfg.prefix}:${key}`

  const sync = () => {
    const tiles = cfg.source.get()
    const wanted = new Set(tiles.map(cfg.key))

    for (const tile of tiles) {
      const key = cfg.key(tile)
      const title = cfg.title(key)
      const current = registered.get(key)

      // register() replaces same-id in place — safe for live title refreshes.
      if (current && current.title === title) {
        continue
      }

      const dispose = registry.register({
        id: paneId(key),
        area: 'panes',
        title,
        data: { dock: { pane: 'workspace', pos: cfg.dir?.(tile) ?? 'right' }, minWidth: cfg.minWidth, placement: 'main' },
        render: () => cfg.render(key)
      })

      registered.set(key, { dispose, title })

      if (!current) {
        registerPaneCloser(paneId(key), () => cfg.close(key))
      }
    }

    for (const [key, entry] of registered) {
      if (!wanted.has(key)) {
        entry.dispose()
        registered.delete(key)
        removeTreePane(paneId(key))
      }
    }
  }

  return () => {
    sync()
    cfg.source.listen(sync)
    cfg.also?.forEach(atom => atom.listen(sync))
  }
}
