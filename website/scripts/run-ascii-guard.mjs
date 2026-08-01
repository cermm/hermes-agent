import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

const ASCII_GUARD_SPEC = 'ascii-guard==2.3.0';
const PY_YAML_SPEC = 'pyyaml==6.0.3';
export function runAsciiGuard(args, spawn = spawnSync) {
  for (const candidate of [
    ['ascii-guard', args],
    ['uvx', ['--from', ASCII_GUARD_SPEC, '--with', PY_YAML_SPEC, 'ascii-guard', ...args]],
    ['uv', ['tool', 'run', '--from', ASCII_GUARD_SPEC, '--with', PY_YAML_SPEC, 'ascii-guard', ...args]],
  ]) {
    const [command, commandArgs] = candidate;
    const result = spawn(command, commandArgs, {
      encoding: 'utf8',
      stdio: 'pipe',
      shell: false,
    });
    if (commandMissing(result)) {
      continue;
    }
    flush(result);
    return result.status ?? 1;
  }

  console.error(
    'Unable to run ascii-guard. Install ascii-guard==2.3.0, or install uv/uvx so the pinned fallback can bootstrap it.'
  );
  return 1;
}

function flush(result) {
  if (result.stdout) {
    process.stdout.write(result.stdout);
  }
  if (result.stderr) {
    process.stderr.write(result.stderr);
  }
}

function commandMissing(result) {
  return result.error?.code === 'ENOENT';
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exit(runAsciiGuard(process.argv.slice(2)));
}
