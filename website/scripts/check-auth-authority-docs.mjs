#!/usr/bin/env node

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const websiteDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const docs = [
  "docs/getting-started/nix-setup.md",
  "i18n/zh-Hans/docusaurus-plugin-content-docs/current/getting-started/nix-setup.md",
  "docs/guides/auth-authority.md",
  "i18n/zh-Hans/docusaurus-plugin-content-docs/current/guides/auth-authority.md",
  "docs/user-guide/profiles.md",
  "i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-guide/profiles.md",
  "docs/reference/profile-commands.md",
  "i18n/zh-Hans/docusaurus-plugin-content-docs/current/reference/profile-commands.md",
  "docs/reference/slash-commands.md",
  "i18n/zh-Hans/docusaurus-plugin-content-docs/current/reference/slash-commands.md",
];

const authorityGuides = docs.slice(2, 4);
const requiredContracts = [
  "auth.authority",
  "hermes auth migrate-shared --profile coder --dry-run",
  "hermes auth migrate-recover --plan-id <id>",
  "--auth-mode include-encrypted",
  "--auth-action restore-shared",
  "--auth-action restore-profile",
  "hermes profile create NAME --auth-mode profile",
  "scripts/docker_auth_authority.py",
  "scripts/docker_rebootstrap_nous_session.py",
  "services.hermes-agent.authAuthority",
  "hermes_cli.main",
  "hermes_cli.models",
  "gateway.run",
  "tools.xai_http",
];

const surfaceContracts = new Map([
  [docs[4], ["auth.authority: shared", "--auth-mode profile", "hermes auth migrate-shared"]],
  [docs[5], ["auth.authority: shared", "--auth-mode profile", "hermes auth migrate-shared"]],
  [docs[6], ["--auth-mode <shared\\|profile>", "hermes auth status", "hermes auth migrate-shared"]],
  [docs[7], ["--auth-mode <shared\\|profile>", "hermes auth status", "hermes auth migrate-shared"]],
  [docs[8], ["Plain `restore <id>` always skips auth", "--include-auth", "--auth-action restore-shared\\|restore-profile"]],
  [docs[9], ["普通 `restore <id>` 始终跳过认证数据", "--include-auth", "--auth-action restore-shared\\|restore-profile"]],
]);

const forbidden = [
  {
    pattern: /authFileForceOverwrite\s*=\s*true/g,
    reason: "docs must not recommend repeated OAuth credential overwrite",
  },
  {
    pattern: /(?:\/home\/[^/\s]+|[A-Za-z]:\\Users\\[^\\\s]+)[/\\][^\s`]*auth\.json/g,
    reason: "docs must show normalized, operator-independent auth paths",
  },
];

const errors = [];
for (const relativePath of docs) {
  const text = readFileSync(resolve(websiteDir, relativePath), "utf8");
  for (const rule of forbidden) {
    for (const match of text.matchAll(rule.pattern)) {
      const line = text.slice(0, match.index).split("\n").length;
      errors.push(`${relativePath}:${line}: ${rule.reason}: ${match[0]}`);
    }
  }
}

for (const relativePath of authorityGuides) {
  const text = readFileSync(resolve(websiteDir, relativePath), "utf8");
  for (const contract of requiredContracts) {
    if (!text.includes(contract)) {
      errors.push(`${relativePath}: missing authority contract: ${contract}`);
    }
  }
}

for (const [relativePath, contracts] of surfaceContracts) {
  const text = readFileSync(resolve(websiteDir, relativePath), "utf8");
  for (const contract of contracts) {
    if (!text.includes(contract)) {
      errors.push(`${relativePath}: missing surface contract: ${contract}`);
    }
  }
}

if (errors.length > 0) {
  console.error(errors.join("\n"));
  process.exit(1);
}

console.log("Authentication-authority documentation checks passed.");
