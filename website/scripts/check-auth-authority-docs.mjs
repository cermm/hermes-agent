#!/usr/bin/env node

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const websiteDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const docs = [
  "docs/getting-started/nix-setup.md",
  "i18n/zh-Hans/docusaurus-plugin-content-docs/current/getting-started/nix-setup.md",
];

const forbidden = [
  {
    pattern: /authFileForceOverwrite/g,
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

if (errors.length > 0) {
  console.error(errors.join("\n"));
  process.exit(1);
}

console.log("Authentication-authority documentation checks passed.");
