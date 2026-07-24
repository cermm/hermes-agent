import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import { seedConfigFrom } from '../../scripts/perf/lib/launch.mjs'

const roots: string[] = []

function tempRoot(): string {
  const root = mkdtempSync(join(tmpdir(), 'hermes-desktop-auth-isolation-'))
  roots.push(root)

  return root
}

afterEach(() => {
  for (const root of roots.splice(0)) {
    rmSync(root, { recursive: true, force: true })
  }
})

describe('desktop isolated perf launch', () => {
  it('copies only non-secret config and leaves auth acquisition to the backend', () => {
    const root = tempRoot()
    const source = join(root, 'source')
    const target = join(root, 'target')
    mkdirSync(source)
    mkdirSync(target)
    writeFileSync(join(source, 'config.yaml'), 'model:\n  provider: nous\n')
    writeFileSync(join(source, '.env'), 'NOUS_API_KEY=secret\n')
    writeFileSync(join(source, 'auth.json'), '{"access_token":"secret"}\n')

    seedConfigFrom(source, target)

    expect(readFileSync(join(target, 'config.yaml'), 'utf8')).toBe(
      'model:\n  provider: nous\n'
    )
    expect(existsSync(join(target, '.env'))).toBe(false)
    expect(existsSync(join(target, 'auth.json'))).toBe(false)
  })
})
