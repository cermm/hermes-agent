/**
 * MULTI-SESSION VIEW STATE — the reactive face of the per-runtime session
 * cache (`sessionStateByRuntimeIdRef` in use-session-state-cache).
 *
 * The cache already ingests EVERY session's gateway events; only the view
 * was single-session ($messages + the active-id gate). This store mirrors
 * the cache per runtime id so any number of surfaces (session tiles, future
 * pane windows) can each subscribe to one session's state without touching
 * the main chat's `$messages` pipeline — same pattern as `useSessionSlice`
 * over `$todosBySession`, applied to whole `ClientSessionState`s.
 *
 * TILES are the first consumer: sessions opened side-by-side with the main
 * thread, each in its own layout-tree pane. `$sessionTiles` holds the
 * stored-session ids (persisted — tiles survive restarts); the wiring layer
 * owns resume/submit (it has the gateway + cache internals) and registers
 * itself here as the delegate so tile UI stays dependency-light.
 */

import { atom } from 'nanostores'

import type { ClientSessionState } from '@/app/types'
import { readJson, writeJson } from '@/lib/storage'

// ---------------------------------------------------------------------------
// Reactive per-runtime session state (view mirror of the wiring cache).
// ---------------------------------------------------------------------------

export const $sessionStates = atom<Record<string, ClientSessionState>>({})

/** Publish one session's state (immutable per-key — slices stay stable). */
export function publishSessionState(runtimeId: string, state: ClientSessionState) {
  $sessionStates.set({ ...$sessionStates.get(), [runtimeId]: state })
}

export function dropSessionState(runtimeId: string) {
  const current = $sessionStates.get()

  if (!(runtimeId in current)) {
    return
  }

  const { [runtimeId]: _dropped, ...rest } = current
  $sessionStates.set(rest)
}

// ---------------------------------------------------------------------------
// Session tiles.
// ---------------------------------------------------------------------------

/** Edge a tile docks against main when it first joins the tree. Shared by
 *  session tiles and route (page) tiles. */
export type SplitDir = 'bottom' | 'left' | 'right' | 'top'

export interface SessionTile {
  /** Stored session id — the durable identity (runtime ids are ephemeral). */
  storedSessionId: string
  /** Edge to dock against main on adoption (default right). */
  dir?: SplitDir
  /** Live runtime id once the tile's resume has bound one. */
  runtimeId?: string
  /** Resume failed terminally (shown in the tile; retryable). */
  error?: string
}

const TILES_KEY = 'hermes.desktop.sessionTiles.v1'

function loadTiles(): SessionTile[] {
  const parsed = readJson<unknown>(TILES_KEY)

  // Runtime ids are process-scoped — never trust a persisted one.
  return Array.isArray(parsed)
    ? parsed
        .filter((t): t is SessionTile => Boolean(t && typeof (t as SessionTile).storedSessionId === 'string'))
        .map(t => ({ dir: t.dir, storedSessionId: t.storedSessionId }))
    : []
}

export const $sessionTiles = atom<SessionTile[]>(loadTiles())

function saveTiles(tiles: SessionTile[]) {
  $sessionTiles.set(tiles)
  writeJson(TILES_KEY, tiles.length === 0 ? null : tiles.map(t => ({ dir: t.dir, storedSessionId: t.storedSessionId })))
}

export function patchSessionTile(storedSessionId: string, patch: Partial<SessionTile>) {
  saveTiles($sessionTiles.get().map(t => (t.storedSessionId === storedSessionId ? { ...t, ...patch } : t)))
}

// ---------------------------------------------------------------------------
// Delegate — the wiring layer (which owns the gateway + session cache) plugs
// its actions in; tile UI calls through here. Same inversion as the tree
// store's pane closers.
// ---------------------------------------------------------------------------

export interface SessionTileDelegate {
  /** Run a slash command against a tile's session (app-level effects — e.g.
   *  branch/handoff — act on the main surface, as they should). */
  executeSlash(rawCommand: string, sessionId: string): Promise<void>
  /** Interrupt a tile's running turn. */
  interruptSession(runtimeId: string): Promise<void>
  /** Bind a live runtime id for a stored session (resume without touching
   *  the main view). Returns the runtime id, or throws. */
  resumeTile(storedSessionId: string): Promise<string>
  /** Submit a prompt to a tile's live session. */
  submitToSession(runtimeId: string, text: string): Promise<void>
  /** THE session-state write path — routes through the wiring cache so the
   *  cache, the primary view (when active), and every tile mirror agree. */
  updateSession(runtimeId: string, updater: (state: ClientSessionState) => ClientSessionState): ClientSessionState
}

let delegate: SessionTileDelegate | null = null

export function setSessionTileDelegate(next: SessionTileDelegate) {
  delegate = next
}

export function sessionTileDelegate(): SessionTileDelegate | null {
  return delegate
}

/** Open (or front) a tile for a stored session, docked on `dir` (default
 *  right). Idempotent — an already-open tile keeps its original edge. */
export function openSessionTile(storedSessionId: string, dir: SplitDir = 'right') {
  const tiles = $sessionTiles.get()

  if (!tiles.some(t => t.storedSessionId === storedSessionId)) {
    saveTiles([...tiles, { dir, storedSessionId }])
  }
}

export function closeSessionTile(storedSessionId: string) {
  saveTiles($sessionTiles.get().filter(t => t.storedSessionId !== storedSessionId))
}

// Dev hook for automation (mirrors __HERMES_LAYOUT_TREE__).
if (import.meta.env.DEV && typeof window !== 'undefined') {
  ;(window as unknown as Record<string, unknown>).__HERMES_SESSION_TILES__ = {
    close: closeSessionTile,
    open: openSessionTile,
    patch: patchSessionTile,
    publish: publishSessionState,
    states: () => $sessionStates.get(),
    tiles: () => $sessionTiles.get()
  }
}
