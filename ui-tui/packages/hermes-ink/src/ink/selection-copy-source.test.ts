import { describe, expect, it } from 'vitest'

import {
  CellWidth,
  CharPool,
  CopySourcePool,
  createScreen,
  HyperlinkPool,
  markCopySourceRegion,
  setCellAt,
  StylePool
} from './screen.js'
import { createSelectionState, getSelectedText, startSelection, updateSelection } from './selection.js'

// Set up a screen rendered with **bold** stripped to "bold" + a copy-source
// region covering those cells. The on-screen render is `bold` (4 cells); the
// copy-source pool entry is the raw markdown `**bold**` (8 chars).
function screenWithCopySource(rendered: string, source: string, atCol = 0, atRow = 0) {
  const styles = new StylePool()
  const charPool = new CharPool()
  const hyperlinkPool = new HyperlinkPool()
  const copySourcePool = new CopySourcePool()
  const screen = createScreen(20, 4, styles, charPool, hyperlinkPool, copySourcePool)

  for (let i = 0; i < rendered.length; i++) {
    setCellAt(screen, atCol + i, atRow, {
      char: rendered[i]!,
      hyperlink: undefined,
      styleId: screen.emptyStyleId,
      width: CellWidth.Narrow
    })
  }

  const id = copySourcePool.intern(source)
  markCopySourceRegion(screen, atCol, atRow, rendered.length, 1, id)

  return { screen, source, rendered, copySourcePool }
}

describe('getSelectedText copy-source override', () => {
  it('falls back to rendered text when no copy source is set', () => {
    const styles = new StylePool()
    const screen = createScreen(10, 1, styles, new CharPool(), new HyperlinkPool())

    setCellAt(screen, 0, 0, {
      char: 'a',
      hyperlink: undefined,
      styleId: screen.emptyStyleId,
      width: CellWidth.Narrow
    })
    setCellAt(screen, 1, 0, {
      char: 'b',
      hyperlink: undefined,
      styleId: screen.emptyStyleId,
      width: CellWidth.Narrow
    })

    const sel = createSelectionState()
    startSelection(sel, 0, 0)
    updateSelection(sel, 9, 0)

    expect(getSelectedText(sel, screen)).toBe('ab')
  })

  it('substitutes the source string when the selection fully covers the region', () => {
    // rendered "bold" at cols 0..3, source "**bold**"
    const { screen } = screenWithCopySource('bold', '**bold**')

    const sel = createSelectionState()
    startSelection(sel, 0, 0)
    updateSelection(sel, 3, 0)

    expect(getSelectedText(sel, screen)).toBe('**bold**')
  })

  it('substitutes when the selection rect is wider than the region (still fully covers it)', () => {
    const { screen } = screenWithCopySource('bold', '**bold**', 2)

    const sel = createSelectionState()
    startSelection(sel, 0, 0)
    updateSelection(sel, 19, 0)

    // Selection covers cols 0..19 of row 0; region lives at cols 2..5.
    // All region cells inside selection → substitute.
    expect(getSelectedText(sel, screen)).toBe('**bold**')
  })

  it('falls back to rendered text when only part of the region is selected', () => {
    // Source `**hello**` rendered as `hello` at cols 0..4.
    // Select only cols 1..3 (the inside of the rendered word).
    const { screen } = screenWithCopySource('hello', '**hello**')

    const sel = createSelectionState()
    startSelection(sel, 1, 0)
    updateSelection(sel, 3, 0)

    // Region's leftmost cell (col 0) is OUTSIDE the selection → partial,
    // fall back to rendered cells. Behavior intentional in v1: there's no
    // safe sub-mapping from rendered "ell" back to the markdown source.
    expect(getSelectedText(sel, screen)).toBe('ell')
  })

  it('concatenates multiple fully-covered regions on different rows', () => {
    const styles = new StylePool()
    const charPool = new CharPool()
    const copySourcePool = new CopySourcePool()
    const screen = createScreen(20, 4, styles, charPool, new HyperlinkPool(), copySourcePool)

    // Row 0: rendered "bold" / source "**bold**"
    for (let i = 0; i < 4; i++) {
      setCellAt(screen, i, 0, {
        char: 'bold'[i]!,
        hyperlink: undefined,
        styleId: screen.emptyStyleId,
        width: CellWidth.Narrow
      })
    }

    const id1 = copySourcePool.intern('**bold**')
    markCopySourceRegion(screen, 0, 0, 4, 1, id1)

    // Row 1: rendered "italic" / source "*italic*"
    for (let i = 0; i < 6; i++) {
      setCellAt(screen, i, 1, {
        char: 'italic'[i]!,
        hyperlink: undefined,
        styleId: screen.emptyStyleId,
        width: CellWidth.Narrow
      })
    }

    const id2 = copySourcePool.intern('*italic*')
    markCopySourceRegion(screen, 0, 1, 6, 1, id2)

    const sel = createSelectionState()
    startSelection(sel, 0, 0)
    updateSelection(sel, 19, 1)

    expect(getSelectedText(sel, screen)).toBe('**bold**\n*italic*')
  })

  it('emits each region exactly once even though it spans multiple rows', () => {
    const styles = new StylePool()
    const charPool = new CharPool()
    const copySourcePool = new CopySourcePool()
    const screen = createScreen(20, 4, styles, charPool, new HyperlinkPool(), copySourcePool)

    // Multi-row region: source spans rows 0..2, rendered as "abc" on each
    // row. Source string is the original markdown block, e.g. a code fence.
    const source = '```js\nconst x = 1\n```'
    const id = copySourcePool.intern(source)

    for (let row = 0; row < 3; row++) {
      for (let col = 0; col < 3; col++) {
        setCellAt(screen, col, row, {
          char: 'abc'[col]!,
          hyperlink: undefined,
          styleId: screen.emptyStyleId,
          width: CellWidth.Narrow
        })
      }
    }

    markCopySourceRegion(screen, 0, 0, 3, 3, id)

    const sel = createSelectionState()
    startSelection(sel, 0, 0)
    updateSelection(sel, 19, 2)

    // Source emitted ONCE — not three times despite spanning 3 rows.
    expect(getSelectedText(sel, screen)).toBe(source)
  })

  it('mixes regions and unmarked cells in the same selection', () => {
    const styles = new StylePool()
    const charPool = new CharPool()
    const copySourcePool = new CopySourcePool()
    const screen = createScreen(20, 4, styles, charPool, new HyperlinkPool(), copySourcePool)

    // Row 0: plain text "hi" at cols 0..1 (no copy source)
    setCellAt(screen, 0, 0, {
      char: 'h',
      hyperlink: undefined,
      styleId: screen.emptyStyleId,
      width: CellWidth.Narrow
    })
    setCellAt(screen, 1, 0, {
      char: 'i',
      hyperlink: undefined,
      styleId: screen.emptyStyleId,
      width: CellWidth.Narrow
    })

    // Row 1: rendered "bold" / source "**bold**"
    for (let i = 0; i < 4; i++) {
      setCellAt(screen, i, 1, {
        char: 'bold'[i]!,
        hyperlink: undefined,
        styleId: screen.emptyStyleId,
        width: CellWidth.Narrow
      })
    }

    const id = copySourcePool.intern('**bold**')
    markCopySourceRegion(screen, 0, 1, 4, 1, id)

    const sel = createSelectionState()
    startSelection(sel, 0, 0)
    updateSelection(sel, 19, 1)

    expect(getSelectedText(sel, screen)).toBe('hi\n**bold**')
  })

  it('treats mixed copy-source IDs in a single row as fall-back to rendered', () => {
    const styles = new StylePool()
    const charPool = new CharPool()
    const copySourcePool = new CopySourcePool()
    const screen = createScreen(20, 4, styles, charPool, new HyperlinkPool(), copySourcePool)

    // Two different regions on the same row, side by side.
    for (let i = 0; i < 4; i++) {
      setCellAt(screen, i, 0, {
        char: 'abcd'[i]!,
        hyperlink: undefined,
        styleId: screen.emptyStyleId,
        width: CellWidth.Narrow
      })
    }

    const idA = copySourcePool.intern('**ab**')
    const idB = copySourcePool.intern('**cd**')
    markCopySourceRegion(screen, 0, 0, 2, 1, idA)
    markCopySourceRegion(screen, 2, 0, 2, 1, idB)

    const sel = createSelectionState()
    startSelection(sel, 0, 0)
    updateSelection(sel, 3, 0)

    // Both regions fully covered → both substitute, joined with newline
    // (each region starts a new logical line in copy output).
    expect(getSelectedText(sel, screen)).toBe('**ab**\n**cd**')
  })

  it('skips substitution when a region extends outside the selection rect', () => {
    const styles = new StylePool()
    const charPool = new CharPool()
    const copySourcePool = new CopySourcePool()
    const screen = createScreen(20, 4, styles, charPool, new HyperlinkPool(), copySourcePool)

    // Region spans rows 0..1, but the user selects only row 0.
    for (let row = 0; row < 2; row++) {
      for (let col = 0; col < 3; col++) {
        setCellAt(screen, col, row, {
          char: 'abc'[col]!,
          hyperlink: undefined,
          styleId: screen.emptyStyleId,
          width: CellWidth.Narrow
        })
      }
    }

    const id = copySourcePool.intern('source-spans-2-rows')
    markCopySourceRegion(screen, 0, 0, 3, 2, id)

    const sel = createSelectionState()
    startSelection(sel, 0, 0)
    updateSelection(sel, 19, 0)

    // Region's row 1 is outside selection → partial → fall back to cells.
    expect(getSelectedText(sel, screen)).toBe('abc')
  })

  // ── Nested regions (msg-level + per-block) ──
  // The TUI wraps each message in <Box copySource={msg.text}> AND each
  // markdown block in <Box copySource={blockSource}>. Children render
  // AFTER parents in render-node-to-output, so child cells overwrite the
  // parent's copySource ID — only padding/gaps keep the parent ID. The
  // parent's bounding rect (computed from its remaining cells) still
  // approximates its visual extent so containment-based shadowing works.
  it('emits only the outer source when both outer and inner regions are fully covered', () => {
    const styles = new StylePool()
    const charPool = new CharPool()
    const copySourcePool = new CopySourcePool()
    const screen = createScreen(20, 4, styles, charPool, new HyperlinkPool(), copySourcePool)

    for (let i = 0; i < 5; i++) {
      setCellAt(screen, 2 + i, 1, {
        char: 'hello'[i]!,
        hyperlink: undefined,
        styleId: screen.emptyStyleId,
        width: CellWidth.Narrow
      })
    }

    // Outer: msg-level wrapper covers the whole screen rect.
    const outerId = copySourcePool.intern('# msg context\n\n**hello**\n\nmore msg')
    markCopySourceRegion(screen, 0, 0, 20, 4, outerId)

    // Inner: block-level wrapper for "hello", overwrites outer cells in
    // its rect — same order as render-node-to-output (parent first).
    const innerId = copySourcePool.intern('**hello**')
    markCopySourceRegion(screen, 2, 1, 5, 1, innerId)

    const sel = createSelectionState()
    startSelection(sel, 0, 0)
    updateSelection(sel, 19, 3)

    // Both fully covered, but outer strictly contains inner → emit outer only.
    expect(getSelectedText(sel, screen)).toBe('# msg context\n\n**hello**\n\nmore msg')
  })

  it('emits only the inner source when the selection covers just one block', () => {
    const styles = new StylePool()
    const charPool = new CharPool()
    const copySourcePool = new CopySourcePool()
    const screen = createScreen(20, 4, styles, charPool, new HyperlinkPool(), copySourcePool)

    // Two "blocks" rendered as plain text, both within an outer msg region.
    for (let i = 0; i < 5; i++) {
      setCellAt(screen, i, 0, {
        char: 'hello'[i]!,
        hyperlink: undefined,
        styleId: screen.emptyStyleId,
        width: CellWidth.Narrow
      })
    }

    for (let i = 0; i < 5; i++) {
      setCellAt(screen, i, 2, {
        char: 'world'[i]!,
        hyperlink: undefined,
        styleId: screen.emptyStyleId,
        width: CellWidth.Narrow
      })
    }

    // Outer covers rows 0..3 (incl. gap rows 1, 3).
    const outerId = copySourcePool.intern('**hello**\n\n*world*')
    markCopySourceRegion(screen, 0, 0, 20, 4, outerId)

    // Inner blocks
    const helloId = copySourcePool.intern('**hello**')
    const worldId = copySourcePool.intern('*world*')
    markCopySourceRegion(screen, 0, 0, 5, 1, helloId)
    markCopySourceRegion(screen, 0, 2, 5, 1, worldId)

    // Selection covers only the "hello" block on row 0.
    const sel = createSelectionState()
    startSelection(sel, 0, 0)
    updateSelection(sel, 4, 0)

    // helloId fully covered (its on-screen cells all in the selection).
    // outerId NOT fully covered (rows 1-3 outside selection).
    // worldId NOT fully covered (its row outside selection).
    expect(getSelectedText(sel, screen)).toBe('**hello**')
  })

  it('emits multiple inner blocks when outer is partially selected but inners are fully covered', () => {
    const styles = new StylePool()
    const charPool = new CharPool()
    const copySourcePool = new CopySourcePool()
    const screen = createScreen(20, 5, styles, charPool, new HyperlinkPool(), copySourcePool)

    // Three blocks on rows 0, 2, 4 — outer covers rows 0..4
    for (const row of [0, 2, 4]) {
      for (let i = 0; i < 3; i++) {
        setCellAt(screen, i, row, {
          char: 'abc'[i]!,
          hyperlink: undefined,
          styleId: screen.emptyStyleId,
          width: CellWidth.Narrow
        })
      }
    }

    const outerId = copySourcePool.intern('OUTER')
    markCopySourceRegion(screen, 0, 0, 20, 5, outerId)

    const block1Id = copySourcePool.intern('# h1')
    const block2Id = copySourcePool.intern('# h2')
    const block3Id = copySourcePool.intern('# h3')
    markCopySourceRegion(screen, 0, 0, 3, 1, block1Id)
    markCopySourceRegion(screen, 0, 2, 3, 1, block2Id)
    markCopySourceRegion(screen, 0, 4, 3, 1, block3Id)

    // Selection covers rows 0..2 → blocks 1 and 2 fully covered; outer NOT
    // (row 4 cells outside selection); block3 NOT (row 4 outside).
    const sel = createSelectionState()
    startSelection(sel, 0, 0)
    updateSelection(sel, 19, 2)

    expect(getSelectedText(sel, screen)).toBe('# h1\n# h2')
  })

  it('keeps the single fully-covered region (no shadowing partner exists)', () => {
    // Sanity: containment filter must be a no-op when there's only one
    // fully-covered region.
    const { screen } = screenWithCopySource('hi', '# hi heading')

    const sel = createSelectionState()
    startSelection(sel, 0, 0)
    updateSelection(sel, 19, 0)

    expect(getSelectedText(sel, screen)).toBe('# hi heading')
  })

  // ── Drag-to-scroll fallback ──
  // captureScrolledRows caches scrolled rows as RENDERED text. If we
  // also emit a region's source for the on-screen part, the result
  // contains both the rendered scrolled-off rows AND the source string,
  // duplicating the region's content. When scrolledOff buffers are
  // non-empty, source substitution is suppressed entirely so the output
  // matches what the user visually selected.
  it('falls back to rendered cells when there are scrolled-off rows', () => {
    const { screen } = screenWithCopySource('bold', '**bold**')

    const sel = createSelectionState()
    startSelection(sel, 0, 0)
    updateSelection(sel, 3, 0)

    // Simulate a previous drag-scroll having captured some rows.
    sel.scrolledOffAbove.push('previous block')
    sel.scrolledOffAboveSW.push(false)

    // No source substitution: gets "previous block" + the RENDERED row
    // text, not "previous block" + "**bold**".
    expect(getSelectedText(sel, screen)).toBe('previous block\nbold')
  })

  it('still substitutes source on a static (non-scrolled) selection', () => {
    // Sanity: scrolledOff fallback is a no-op when the buffers are empty.
    const { screen } = screenWithCopySource('bold', '**bold**')

    const sel = createSelectionState()
    startSelection(sel, 0, 0)
    updateSelection(sel, 3, 0)

    expect(getSelectedText(sel, screen)).toBe('**bold**')
  })
})
