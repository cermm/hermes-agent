import { closeActiveTerminal } from '@/app/right-sidebar/terminal/terminals'
import { closeFocusedTreeTab, focusedTreePane } from '@/components/pane-shell/tree/store'
import { isFocusWithin } from '@/lib/keybinds/combo'
import { PREVIEW_PANE_ID } from '@/store/layout'
import { closeActiveRightRailTab } from '@/store/preview'

/**
 * ⌘W — close whatever "tab" the interacted zone holds, by precedence:
 *   1. a focused terminal → its active terminal tab,
 *   2. the preview zone (its OWN per-target strip) → its active target tab,
 *   3. any other zone → its active tree tab (session tile / files…).
 * The zone is resolved from the active-zone tracker (last click/focus), so it
 * works even when nothing is DOM-focused. Returns false when nothing is
 * closeable, so the caller falls back (close the window). Shared by the
 * keyboard path (Win/Linux) and the macOS menu-accelerator IPC.
 */
export function closeActiveTab(): boolean {
  if (isFocusWithin('[data-terminal]')) {
    closeActiveTerminal()

    return true
  }

  if (focusedTreePane() === PREVIEW_PANE_ID) {
    closeActiveRightRailTab()

    return true
  }

  return closeFocusedTreeTab()
}
