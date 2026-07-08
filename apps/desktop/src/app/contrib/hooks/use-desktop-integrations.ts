import { useEffect, useRef } from 'react'

import { isFocusWithin } from '@/lib/keybinds/combo'
import { storedSessionIdForNotification } from '@/lib/session-ids'
import { respondToApprovalAction } from '@/store/native-notifications'
import { $filePreviewTarget, $previewTarget, closeActiveRightRailTab } from '@/store/preview'
import { getRememberedSessionId, setRememberedSessionId } from '@/store/session'
import { onSessionsChanged } from '@/store/session-sync'
import { openUpdatesWindow, startUpdatePoller, stopUpdatePoller } from '@/store/updates'
import { isSecondaryWindow } from '@/store/windows'

import { requestComposerFocus, requestComposerInsert } from '../../chat/composer/focus'
import { closeActiveTerminal } from '../../right-sidebar/terminal/terminals'
import { NEW_CHAT_ROUTE, sessionRoute } from '../../routes'

interface DesktopIntegrationsParams {
  chatOpen: boolean
  hasPreview: boolean
  locationPathname: string
  navigate: (to: string, options?: { replace?: boolean }) => void
  refreshSessions: () => Promise<unknown> | unknown
  resumeExhaustedSessionId: null | string
  routedSessionId: null | string
  runtimeIdByStoredSessionId: { readonly current: Map<string, string> }
}

/**
 * All the Electron-main / OS / cross-window integrations the shell listens for:
 * update polling, the ⌘W close shortcut, deep links, native-notification
 * navigation, preview-shortcut enablement, remembered-session restore, and
 * cross-window session-list sync. Kept out of the wiring controller so the
 * "talks to the desktop shell" surface reads as one unit.
 */
export function useDesktopIntegrations({
  chatOpen,
  hasPreview,
  locationPathname,
  navigate,
  refreshSessions,
  resumeExhaustedSessionId,
  routedSessionId,
  runtimeIdByStoredSessionId
}: DesktopIntegrationsParams): void {
  // Update polling — populates $desktopVersion/$updateStatus, which feed the
  // statusbar version pill and the update toasts. Also honors the main
  // process's "open updates" menu request.
  useEffect(() => {
    startUpdatePoller()
    const unsubscribe = window.hermesDesktop?.onOpenUpdatesRequested?.(() => openUpdatesWindow())

    return () => {
      unsubscribe?.()
      stopUpdatePoller()
    }
  }, [])

  // Main-process preview shortcut (⌘W menu item enablement).
  useEffect(() => {
    window.hermesDesktop?.setPreviewShortcutActive?.(Boolean(chatOpen && hasPreview))
  }, [chatOpen, hasPreview])

  // Remember the open chat so a relaunch reopens it instead of an empty
  // new-chat; restore once on cold start; a dead id self-clears.
  useEffect(() => {
    if (routedSessionId) {
      setRememberedSessionId(routedSessionId)
    }
  }, [routedSessionId])

  const restoredLastSessionRef = useRef(false)

  useEffect(() => {
    if (restoredLastSessionRef.current) {
      return
    }

    restoredLastSessionRef.current = true
    const last = getRememberedSessionId()

    if (last && locationPathname === NEW_CHAT_ROUTE) {
      navigate(sessionRoute(last), { replace: true })
    }
  }, [locationPathname, navigate])

  useEffect(() => {
    if (resumeExhaustedSessionId && getRememberedSessionId() === resumeExhaustedSessionId) {
      setRememberedSessionId(null)
    }
  }, [resumeExhaustedSessionId])

  // Native-notification click -> jump to the session (runtime id translated to
  // the stored id the chat route is keyed by); action buttons resolve in place.
  useEffect(() => {
    const unsubscribe = window.hermesDesktop?.onFocusSession?.(sessionId => {
      if (sessionId) {
        navigate(sessionRoute(storedSessionIdForNotification(sessionId, runtimeIdByStoredSessionId.current)))
      }
    })

    return () => unsubscribe?.()
  }, [navigate, runtimeIdByStoredSessionId])

  useEffect(() => {
    const unsubscribe = window.hermesDesktop?.onNotificationAction?.(({ actionId, sessionId }) => {
      void respondToApprovalAction(sessionId ?? null, actionId)
    })

    return () => unsubscribe?.()
  }, [])

  // hermes:// deep links -> a reviewable /blueprint command in the composer.
  useEffect(() => {
    const unsubscribe = window.hermesDesktop?.onDeepLink?.(payload => {
      if (!payload || payload.kind !== 'blueprint' || !payload.name) {
        return
      }

      const slots = Object.entries(payload.params || {})
        .map(([k, v]) => {
          const sval = /\s/.test(v) ? `"${v.replace(/"/g, '\\"')}"` : v

          return `${k}=${sval}`
        })
        .join(' ')

      const command = `/blueprint ${payload.name}${slots ? ' ' + slots : ''}`
      requestComposerInsert(command, { mode: 'block', target: 'main' })
      requestComposerFocus('main')
    })

    void window.hermesDesktop?.signalDeepLinkReady?.()

    return () => unsubscribe?.()
  }, [])

  // ⌘W: close the focused terminal, else the active preview tab.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.altKey || event.shiftKey || event.key.toLowerCase() !== 'w' || (!event.metaKey && !event.ctrlKey)) {
        return
      }

      if (isFocusWithin('[data-terminal]')) {
        if (event.metaKey && !event.ctrlKey) {
          event.preventDefault()
          event.stopPropagation()
          closeActiveTerminal()
        }

        return
      }

      if ($filePreviewTarget.get() || $previewTarget.get()) {
        event.preventDefault()
        event.stopPropagation()
        closeActiveRightRailTab()
      }
    }

    const unsubscribe = window.hermesDesktop?.onClosePreviewRequested?.(closeActiveRightRailTab)

    window.addEventListener('keydown', onKeyDown, { capture: true })

    return () => {
      unsubscribe?.()
      window.removeEventListener('keydown', onKeyDown, { capture: true })
    }
  }, [])

  // Another window mutated the shared session list -> re-pull the sidebar.
  useEffect(() => {
    if (isSecondaryWindow()) {
      return
    }

    return onSessionsChanged(() => void refreshSessions())
  }, [refreshSessions])
}
