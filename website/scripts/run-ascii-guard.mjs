import { spawnSync } from 'node:child_process';

const ASCII_GUARD_SPEC = 'ascii-guard==2.3.0';
const PY_YAML_SPEC = 'pyyaml==6.0.3';
const args = process.argv.slice(2);

function run(command, commandArgs) {
  return spawnSync(command, commandArgs, {
    encoding: 'utf8',
    stdio: 'pipe',
    shell: false,
  });
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

for (const candidate of [
  ['ascii-guard', args],
  ['uvx', ['--from', ASCII_GUARD_SPEC, '--with', PY_YAML_SPEC, 'ascii-guard', ...args]],
  ['uv', ['tool', 'run', '--from', ASCII_GUARD_SPEC, '--with', PY_YAML_SPEC, 'ascii-guard', ...args]],
]) {
  const [command, commandArgs] = candidate;
  const result = run(command, commandArgs);
  if (commandMissing(result)) {
    continue;
  }
  flush(result);
  process.exit(result.status ?? 1);
}

console.error(
  'Unable to run ascii-guard. Install python3 with ascii-guard==2.3.0, or install uv/uvx so the pinned fallback can bootstrap it.'
);
process.exit(1);
