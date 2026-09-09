---
title: "Semantic Code Intelligence — Navigate code and verify edits with semantic evidence"
sidebar_label: "Semantic Code Intelligence"
description: "Navigate code and verify edits with semantic evidence"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Semantic Code Intelligence

Navigate code and verify edits with semantic evidence.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/software-development/semantic-code-intelligence` |
| Version | `0.1.0` |
| Author | Michal, Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `coding`, `navigation`, `diagnostics`, `lsp`, `review` |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Semantic Code Intelligence Skill

Navigate unfamiliar code and verify changes using the capabilities offered in this session.
This workflow uses already provisioned tools; it does not install language servers or change
profile, project, permission, or model settings.

## When to Use

- Find definitions, callers, declarations, or implementations across unfamiliar source files.
- Make a code change and distinguish current diagnostics from errors introduced by that edit.
- Review code without writing, or verify changes produced by a shell command or generator.

## Prerequisites

Use only tools offered to this worker. Serena navigation is optional: the provisioned
`serena_semantic_navigation` MCP server may expose the names below, but another session may
have different names or none. Follow the actual offered schema and permission scope.
No MCP tool is required to load this skill. Preserve disabled skills and restricted toolsets.

## How to Run

Start with the task's actual worktree and the available source-reading/navigation tools.
When `tool_search` is offered, search the session catalog for relevant symbol navigation;
call a result only if offered. If unavailable, use permitted `search_files` and `read_file`.
Do not infer semantic access merely from the presence of the catalog bridge.

For explicit verification **only when `terminal` is permitted**, and its local command/source
environment corresponds to the task files, use the existing CLI through `terminal`:

```text
hermes lsp check --root <task-worktree> --json <explicit-file> [<explicit-file> ...]
```

Pass at most 16 explicit files. Quote concrete paths for the actual shell. Do not use a no-op
write to request diagnostics. If the command is absent, nonlocal, denied, or unavailable,
use permitted repository checks or report the verification limit. Never substitute
`execute_code` or another capability to bypass a terminal denial.

## Quick Reference

These are examples from the provisioned server, not a promise of current availability:

| Offered tool | Purpose |
|---|---|
| `mcp__serena_semantic_navigation__get_symbols_overview` | Outline one source file. |
| `mcp__serena_semantic_navigation__find_symbol` | Locate a symbol and optionally its body. |
| `mcp__serena_semantic_navigation__find_referencing_symbols` | Inspect callers or references. |
| `mcp__serena_semantic_navigation__find_declaration` | Resolve a declaration when supported. |
| `mcp__serena_semantic_navigation__find_implementations` | Find implementations when supported. |
| `mcp__serena_semantic_navigation__get_diagnostics_for_file` | Consult backend diagnostics with its stated limits. |

## Procedure

1. **Bind evidence to the task.** Confirm the actual root, file, profile and commit when supplied
   by the task or result. A result from a fixed project or related control repository does not
   prove anything about the active worktree. Report missing provenance; do not invent it.
2. **Read the relevant code.** Prefer scoped symbol navigation for relationships, and literal
   search for text. Inspect the returned source before editing. An unsupported implementation
   lookup is a capability limit; use permitted source evidence instead of repeating it.
3. **Make the applicable change.** Use `patch` or `write_file` only when offered and authorized.
   Preserve review-only restrictions. Do not change project activation or repository `.serena`
   files to make a lookup work; the provisioned launcher owns its metadata and task binding.
4. **Read the complete verification result.** `verified` confirms written content, syntax lint
   checks syntax, and `lsp_verification` reports semantic evidence; none replaces repository
   tests. For each file, check status/reason, current source attribution and full `total` counts.
   `delta` is introduced diagnostics only when the pre-write baseline is available. Existing
   errors remain errors even when the introduced delta is zero. An unavailable baseline with
   null delta cannot establish which diagnostics this edit introduced.
5. **Repair and verify.** Correct task-caused errors and inspect fresh evidence for the repair.
   For shell/generator edits or read-only review, use the explicit check above only if permitted.
   Run applicable repository checks through allowed tools. Do not repeat unchanged checks to
   manufacture a green outcome or increase diagnostic budgets after a timeout.
6. **Report evidence and limits.** Name the checked files and actual checks. Distinguish clean,
   diagnostics present and unverified outcomes. With denied execution, report source-bound
   review findings and state which executable checks could not be performed.

## Pitfalls

- `no_verdict`, timeout, disabled, excluded, and not-checked outcomes do not mean clean.
- An empty backend diagnostics array or a successful MCP connection is not fresh clean proof.
- The explicit CLI returns 0 for complete clean evidence, 1 for complete evidence containing
  diagnostics, 2 for incomplete/invalid/unavailable evidence, and 130 for cancellation. Read
  per-file statuses and omitted-file counts as well as the exit code; do not call exit 2 clean.
- A skill describes a workflow; it never expands permissions or guarantees a tool is available.
- Do not install providers, alter timeouts, reload another session's tools, restart services,
  change SOUL/model/profile settings, or bypass denied tools as part of this coding workflow.

## Verification

Every claimed semantic result must match the task source and report its actual freshness.
A repair needs fresh clean evidence or a clearly stated unverified result plus the permitted
checks performed. Preserve full-current versus introduced counts, unavailable baselines,
read-only restrictions, and relevant repository-test results in the final report.
