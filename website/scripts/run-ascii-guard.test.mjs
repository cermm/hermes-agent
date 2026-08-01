import assert from 'node:assert/strict';
import test from 'node:test';

import { runAsciiGuard } from './run-ascii-guard.mjs';

test('uses the installed ascii-guard console entry point', () => {
  const calls = [];
  const status = runAsciiGuard(['lint', 'docs'], (command, args, options) => {
    calls.push({ command, args, options });
    return { status: 0, stdout: '', stderr: '' };
  });

  assert.equal(status, 0);
  assert.deepEqual(calls, [
    {
      command: 'ascii-guard',
      args: ['lint', 'docs'],
      options: { encoding: 'utf8', stdio: 'pipe', shell: false },
    },
  ]);
});