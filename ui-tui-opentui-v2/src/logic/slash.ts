/**
 * Slash command system — the SOLID side (spec §1; mirrors Ink
 * `app/createSlashHandler.ts` + `domain/slash.ts`). Plain functions/data, NOT
 * Effect; the boundary injects a Promise-returning `request` so dispatch can call
 * `slash.exec` / `command.dispatch` / `commands.catalog`.
 *
 * Dispatch ladder (Ink parity):
 *   1. client-local command (the TUI-only set — handled in-process)
 *   2. `slash.exec {command, session_id}` → `{output, warning?}` → system line
 *   3. on reject → `command.dispatch {arg, name, session_id}` → typed action
 *      (exec/plugin → system · alias → re-dispatch · skill/send → submit a turn ·
 *       prefill → notice). Long output routes to the pager (Phase 5a).
 */
import type { CompletionItem, PickerItem, PickerState, SessionItem } from './store.ts'

export interface ParsedSlash {
  name: string
  arg: string
}

/** Parse `/name rest…` → {name, arg}; null if not a slash command. */
export function parseSlash(input: string): ParsedSlash | null {
  if (!input.startsWith('/')) return null
  const body = input.slice(1).trimStart()
  if (!body) return null
  const sp = body.indexOf(' ')
  return sp === -1 ? { arg: '', name: body } : { arg: body.slice(sp + 1).trim(), name: body.slice(0, sp) }
}

/** The host capabilities the dispatcher needs (wired by the entry boundary). */
export interface SlashContext {
  /** Server RPC (resolves with the result, rejects on GatewayError). */
  readonly request: (method: string, params: Record<string, unknown>) => Promise<unknown>
  readonly sessionId: () => string | undefined
  readonly pushSystem: (text: string) => void
  /** Open the full-screen pager (long output: /status, /logs, …). */
  readonly openPager: (title: string, text: string) => void
  /** Submit a user turn (skill/send dispatch results). */
  readonly submit: (text: string) => void
  /** Open a local Y/N confirm; `onConfirm` runs on Yes. */
  readonly confirm: (message: string, onConfirm: () => void) => void
  readonly clearTranscript: () => void
  readonly quit: () => void
  /** Recent log lines for `/logs` (the ring buffer). */
  readonly logTail: () => string[]
  /** Fetch the resumable sessions (`session.list`) for the switcher. */
  readonly listSessions: () => Promise<SessionItem[]>
  /** Open the session switcher with the given rows. */
  readonly openSwitcher: (sessions: SessionItem[]) => void
  /** Open a generic picker (model picker, skills hub). */
  readonly openPicker: (picker: PickerState) => void
}

function readStr(value: unknown, key: string): string | undefined {
  if (!value || typeof value !== 'object') return undefined
  const v = (value as { [k: string]: unknown })[key]
  return typeof v === 'string' ? v : undefined
}

const titleCase = (name: string) => name.charAt(0).toUpperCase() + name.slice(1)

/** Map a `complete.slash` result ({items:[{text,display,meta}]}) into completion candidates. */
export function mapCompletions(result: unknown): CompletionItem[] {
  if (!result || typeof result !== 'object') return []
  const items = (result as { items?: unknown }).items
  if (!Array.isArray(items)) return []
  const out: CompletionItem[] = []
  for (const it of items) {
    const text = readStr(it, 'text')
    if (!text) continue
    out.push({ display: readStr(it, 'display') ?? text, meta: readStr(it, 'meta') ?? '', text })
  }
  return out
}

/** Long output → the pager; short → a system line (Ink: >180 chars or >2 lines). */
function present(ctx: SlashContext, title: string, text: string): void {
  const long = text.length > 180 || text.split('\n').filter(Boolean).length > 2
  if (long) ctx.openPager(title, text)
  else ctx.pushSystem(text)
}

const CLIENT_HELP = [
  '/help — list commands',
  '/model [name] — switch model (picker if bare)',
  '/skills — browse skills',
  '/sessions, /resume — switch/resume a session',
  '/clear, /new — clear the transcript (confirm)',
  '/logs — recent engine log lines',
  '/quit, /exit — quit',
  '(other /commands run on the gateway)'
].join('\n')

type ClientHandler = (arg: string, ctx: SlashContext) => void | Promise<void>

/** Fetch sessions and open the switcher (shared by /sessions, /resume, /switch, /session). */
const openSwitcher: ClientHandler = async (_arg, ctx) => {
  const sessions = await ctx.listSessions()
  if (sessions.length) ctx.openSwitcher(sessions)
  else ctx.pushSystem('No sessions to resume.')
}

/** Flatten `model.options` (authenticated providers' models) into picker rows; mark the current. */
function mapModelOptions(opts: unknown): PickerItem[] {
  if (!opts || typeof opts !== 'object') return []
  const providers = (opts as { providers?: unknown }).providers
  if (!Array.isArray(providers)) return []
  const current = readStr(opts, 'model')
  const items: PickerItem[] = []
  for (const p of providers) {
    if (!p || typeof p !== 'object' || (p as { authenticated?: unknown }).authenticated !== true) continue
    const slug = readStr(p, 'slug') ?? readStr(p, 'name') ?? ''
    const models = (p as { models?: unknown }).models
    if (!Array.isArray(models)) continue
    for (const m of models) {
      if (typeof m === 'string') items.push({ description: slug, label: m === current ? `${m} ✓` : m, value: m })
    }
  }
  return items
}

/** Flatten `skills.manage {action:'list'}` ({skills: Record<category, names[]>}) into picker rows. */
function mapSkills(result: unknown): PickerItem[] {
  if (!result || typeof result !== 'object') return []
  const skills = (result as { skills?: unknown }).skills
  if (!skills || typeof skills !== 'object') return []
  const items: PickerItem[] = []
  for (const [category, names] of Object.entries(skills as { [k: string]: unknown })) {
    if (!Array.isArray(names)) continue
    for (const n of names) if (typeof n === 'string') items.push({ description: category, label: n, value: n })
  }
  return items
}

/** Switch the model via the server (shared by `/model <name>` and the picker pick). */
async function switchModel(ctx: SlashContext, name: string): Promise<void> {
  try {
    const r = await ctx.request('slash.exec', { command: `model ${name}`, session_id: ctx.sessionId() })
    ctx.pushSystem(readStr(r, 'output') || `→ ${name}`)
  } catch (error) {
    ctx.pushSystem(`/model ${name}: ${error instanceof Error ? error.message : 'switch failed'}`)
  }
}

/** `/model` — bare opens the model picker; `/model <name>` switches directly. */
const modelCmd: ClientHandler = async (arg, ctx) => {
  if (arg.trim()) {
    await switchModel(ctx, arg.trim())
    return
  }
  const items = mapModelOptions(await ctx.request('model.options', {}))
  if (!items.length) {
    ctx.pushSystem('No models available (no authenticated providers).')
    return
  }
  ctx.openPicker({ items, onPick: name => void switchModel(ctx, name), title: 'Switch model' })
}

/** `/skills` — open the skills hub; picking a skill shows its info in the pager. */
const skillsCmd: ClientHandler = async (_arg, ctx) => {
  const items = mapSkills(await ctx.request('skills.manage', { action: 'list' }))
  if (!items.length) {
    ctx.pushSystem('No skills found.')
    return
  }
  ctx.openPicker({
    items,
    onPick: name =>
      void ctx
        .request('skills.manage', { action: 'inspect', query: name })
        .then(info => ctx.openPager(`Skill: ${name}`, readStr(info, 'info') || JSON.stringify(info, null, 2)))
        .catch(() => ctx.pushSystem(`/skills: could not inspect ${name}`)),
    title: 'Skills'
  })
}

/** The TUI-only client commands (run in-process, never hit the gateway). */
const CLIENT: Record<string, ClientHandler> = {
  clear: (_arg, ctx) => ctx.confirm('Clear the transcript?', ctx.clearTranscript),
  exit: (_arg, ctx) => ctx.quit(),
  model: modelCmd,
  resume: openSwitcher,
  session: openSwitcher,
  sessions: openSwitcher,
  skills: skillsCmd,
  switch: openSwitcher,
  help: async (_arg, ctx) => {
    // Prefer the live catalog; fall back to the client list if it's unavailable.
    try {
      const cat = await ctx.request('commands.catalog', {})
      ctx.pushSystem(renderCatalog(cat) || CLIENT_HELP)
    } catch {
      ctx.pushSystem(CLIENT_HELP)
    }
  },
  logs: (_arg, ctx) => ctx.openPager('Logs', ctx.logTail().join('\n') || '(log empty)'),
  new: (_arg, ctx) => ctx.confirm('Start fresh? (clears the transcript)', ctx.clearTranscript),
  quit: (_arg, ctx) => ctx.quit()
}

/** Render the gateway `commands.catalog` into a help block (loose-typed read).
 *  The TUI catalog shape is `{ pairs: [["/name","desc"], …], canon, categories }`
 *  (tui_gateway/server.py `commands.catalog`). */
function renderCatalog(cat: unknown): string {
  if (!cat || typeof cat !== 'object') return ''
  const pairs = (cat as { pairs?: unknown }).pairs
  if (!Array.isArray(pairs)) return ''
  const lines = pairs
    .map(pair => {
      if (!Array.isArray(pair) || typeof pair[0] !== 'string') return null
      const desc = typeof pair[1] === 'string' ? pair[1] : ''
      return desc ? `${pair[0]} — ${desc}` : pair[0]
    })
    .filter((l): l is string => l !== null)
  return lines.length ? lines.join('\n') : ''
}

function handleDispatchResult(parsed: ParsedSlash, raw: unknown, ctx: SlashContext): void {
  const type = readStr(raw, 'type')
  const argTail = parsed.arg ? ` ${parsed.arg}` : ''
  switch (type) {
    case 'exec':
    case 'plugin':
      ctx.pushSystem(readStr(raw, 'output') || '(no output)')
      return
    case 'alias': {
      const target = readStr(raw, 'target')
      if (target) void dispatchSlash(`/${target}${argTail}`, ctx)
      return
    }
    case 'skill':
    case 'send': {
      const notice = readStr(raw, 'notice')
      if (notice) ctx.pushSystem(notice)
      const message = readStr(raw, 'message')
      if (message?.trim()) ctx.submit(message)
      else ctx.pushSystem(`/${parsed.name}: empty message`)
      return
    }
    case 'prefill': {
      // /undo etc. — composer prefill lands with the composer-ref plumbing; show it for now.
      const message = readStr(raw, 'message')
      ctx.pushSystem(message ? `(edit & resubmit) ${message}` : `/${parsed.name}: nothing to prefill`)
      return
    }
    default:
      ctx.pushSystem(`error: invalid response: command.dispatch`)
  }
}

/** Dispatch a `/command` through the ladder. Returns once the (async) work settles. */
export async function dispatchSlash(input: string, ctx: SlashContext): Promise<void> {
  const parsed = parseSlash(input)
  if (!parsed) return

  const client = CLIENT[parsed.name]
  if (client) {
    await client(parsed.arg, ctx)
    return
  }

  const sid = ctx.sessionId()
  try {
    const result = await ctx.request('slash.exec', { command: input.slice(1), session_id: sid })
    const output = readStr(result, 'output') || `/${parsed.name}: no output`
    const warning = readStr(result, 'warning')
    const text = warning ? `warning: ${warning}\n${output}` : output
    // Long output → pager (Ink: >180 chars or >2 non-empty lines), else a system line.
    present(ctx, titleCase(parsed.name), text)
  } catch {
    try {
      const raw = await ctx.request('command.dispatch', { arg: parsed.arg, name: parsed.name, session_id: sid })
      handleDispatchResult(parsed, raw, ctx)
    } catch (error) {
      ctx.pushSystem(`error: ${error instanceof Error ? error.message : String(error)}`)
    }
  }
}
