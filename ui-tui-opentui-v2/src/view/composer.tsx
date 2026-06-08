/**
 * Composer — the input row (spec v4 §2). A native <textarea> captured by ref;
 * Enter submits, the input clears imperatively, and a live slash-completion
 * dropdown renders ABOVE it as you type `/…` (spec §1 completions).
 *
 * Gotchas (§8 #3): `flexShrink:0` so it never collapses onto its rule; clear via
 * `.clear()` (NOT key-remount); a `submitting` re-entrancy guard.
 *
 * Completions: `onContentChange` reports the text → `onType` (entry boundary)
 * queries `complete.slash` and fills `completions()`. The textarea owns key input
 * (so live-refine-by-typing works), so we use Tab to accept the top match and Esc
 * to dismiss (arrow-nav would fight the textarea's cursor; a polish item).
 * `onSubmit`/`onType` are plain callbacks wired by the entry — no Effect here.
 */
import { type TextareaRenderable } from '@opentui/core'
import { useKeyboard } from '@opentui/solid'
import { For, onMount, Show } from 'solid-js'

import type { CompletionItem } from '../logic/store.ts'
import { useTheme } from './theme.tsx'

export function Composer(props: {
  onSubmit: (text: string) => void
  onType?: ((text: string) => void) | undefined
  completions?: (() => CompletionItem[]) | undefined
  onDismiss?: (() => void) | undefined
}) {
  const theme = useTheme()
  let ta: TextareaRenderable | undefined
  let submitting = false
  const completions = () => props.completions?.() ?? []

  const submit = () => {
    if (submitting || !ta) return
    const text = ta.plainText.trim()
    if (!text) return
    submitting = true
    props.onSubmit(text)
    ta.clear()
    props.onDismiss?.()
    submitting = false
  }

  // Tab accepts the top completion; Esc dismisses the dropdown. Only act while the
  // dropdown is open so normal Tab/Esc behaviour is unaffected otherwise.
  useKeyboard(key => {
    if (completions().length === 0) return
    if (key.name === 'tab') {
      const top = completions()[0]
      if (top && ta) {
        ta.clear()
        ta.insertText(top.text + ' ')
        props.onDismiss?.()
      }
    } else if (key.name === 'escape') {
      props.onDismiss?.()
    }
  })

  onMount(() => ta?.focus())

  return (
    <box style={{ flexDirection: 'column', flexShrink: 0, marginTop: 1 }}>
      <Show when={completions().length > 0}>
        <box
          style={{
            backgroundColor: theme().color.completionBg,
            flexDirection: 'column',
            paddingLeft: 1,
            paddingRight: 1
          }}
        >
          <For each={completions().slice(0, 8)}>
            {(c, i) => (
              <text fg={i() === 0 ? theme().color.accent : theme().color.text}>
                {c.display || c.text}
                {c.meta ? `  ${c.meta}` : ''}
              </text>
            )}
          </For>
          <text fg={theme().color.muted}>Tab complete · Esc dismiss</text>
        </box>
      </Show>
      <textarea
        ref={el => (ta = el)}
        style={{ height: 3, width: '100%' }}
        placeholder={theme().brand.welcome}
        placeholderColor={theme().color.muted}
        textColor={theme().color.text}
        cursorColor={theme().color.accent}
        focusedBackgroundColor={theme().color.statusBg}
        keyBindings={[{ action: 'submit', name: 'return' }]}
        onSubmit={submit}
        onContentChange={() => props.onType?.(ta?.plainText ?? '')}
      />
    </box>
  )
}
