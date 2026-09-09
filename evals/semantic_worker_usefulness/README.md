# Semantic worker usefulness: six retained attempts

[Issue 41](https://github.com/cermm/hermes-agent/issues/41) compared three synthetic coding tasks once per arm. The result supports optional, narrowly scoped Python navigation and honest unavailable fallback. It does **not** support automatic TypeScript expansion: both TypeScript task reports were incomplete, and the candidate also acquired an unintended package.

[results.json](results.json) contains exact metrics, thresholds, source/workload/harness hashes and evidence fingerprints. Four of six model reports completed; all six final patches passed the independent correctness oracles. Those are different outcomes. Failed attempts remain in every denominator. Independent measurement review passed the protocol and this scope decision, rather than declaring all samples successful.

## Protocol

The historical installed baseline was Hermes `78b0188daaa477a67014d75e99ee4d45a18e0778`; the candidate was `b446d3f47f8f883f59b9201b5506a47ddcd4aca9` with infrastructure `06f8d9bcdef9d920c351dc037f14c3923d979a9f`. This is not a comparison against the later published main. Later source and controlled-worker integration need separate acceptance.

All runs used the same configured builder-medium role, `gpt-5.6-sol`, `openai-codex`, medium effort, equal paired fixture bytes and prompts, and fresh direct CLI processes. Baselines ran P, T, F; candidates ran T, F, P. No sample was retried or supplemented. Both arms used the same existing Pyright 1.1.409, TypeScript language server 5.3.0, TypeScript 6.0.3 and Node 22.22.3, with managed acquisition disabled and the existing five-second native diagnostic budget. Candidate navigation used Serena 1.7.0. F deliberately disabled native and MCP semantics in both arms.

Separate disposable profiles retained original configuration, instructions and skill policy; candidate owned skills were delivered through the supported synchronizer. The coding surface was symmetrically narrowed to exclude operational effects and auxiliary generation. This direct CLI experiment does not measure Kanban lifecycle behavior or every production grant. Oracles and reference solutions were outside the model-writable fixtures; reports were added only after all fixture snapshots and measured runs. Private profiles, endpoints and raw transcripts are not published here. This sanitized report is an evidence index, not a turnkey public benchmark bundle.

## Results

Wall time covers supervised worker startup through exit, including tool work. JSON also retains first-request and conversation timings, cached-input/reasoning usage, tool counts and serialized input bytes.

| Task / arm | Model report | Final code oracle | Wall seconds | Physical calls | Input tokens | Output tokens |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| Python baseline | Complete | Pass | 128.522529 | 6 | 227392 | 1745 |
| Python candidate | Complete | Pass | 124.330366 | 6 | 182535 | 1952 |
| TypeScript baseline | Incomplete | Pass | 179.283996 | 7 | 233112 | 1770 |
| TypeScript candidate | Incomplete; acquisition-invalid | Pass | 269.167098 | 7 | 252793 | 3284 |
| Fallback baseline | Complete, qualified | Pass | 78.973285 | 6 | 176081 | 1093 |
| Fallback candidate | Complete, qualified | Pass | 91.084725 | 7 | 170802 | 1219 |

Totals: **39 physical requests, 1,242,715 observed input tokens and 11,063 observed output tokens**. All response usage was observed. Actual monetary cost is unknown; no zero-cost or dollar-saving inference is supported.

P required the active invoice line calculation to multiply price by quantity while preserving real imports, callers and similarly named archive/analytics decoys. Both arms made the correct one-file fix. The candidate used two declaration calls (import and use site) and one reference call, all bound to the task. Its edit-time Pyright result was fresh zero with available baseline and zero delta; its separate CLI check was fresh zero at the current source hash with baseline not requested and null delta. Two model-selected canonical tests and representative quantity/multiple-line/shipping assertions passed; the independent oracle separately passed. The candidate used slightly less wall time and input, but more output and tools (24 versus 21). The prompt named the file and multiplication, so this does not establish unassisted source-location benefit.

F repaired the inclusive clamp and preserved reversed-bound behavior. Both completed reports accurately disclosed unavailable semantics. Model-selected tests and corrected boundary assertions passed, as did the independent oracle. Initial failures and quoting errors remain retained. The candidate was slower, used an extra request and more output, despite slightly less input. Passing its expansion guardrails is not a speed improvement.

T required a readonly Date interface refactor and date arithmetic repair while preserving `a5_fixture/legacy_audit.ts:2:14`, TS2322, and its exact source hash. Both independent final oracles found exactly that pre-existing error, no task errors, and correct behavior. Baseline T never completed its report or a successful model-selected postrepair compiler/behavior check. Candidate T eventually ran the intended installed compiler and behavior checks, but never completed its report either. Twelve candidate MCP calls included eight backend diagnostic calls across four files before and after editing; that is not a tool-quota violation. Native patch results included one timeout and two fresh-clean results; the separate CLI returned three timeouts and the fresh legacy error. Empty backend objects were never promoted to fresh-native clean verdicts.

Candidate T also ran `npx tsc`, downloaded and executed **tsc 2.0.4**, then encountered its wrong-compiler banner. Exact npm logs and package metadata establish acquisition. This was a model-selected command and external measurement-boundary failure, not the reviewed native LSP installer. Later correct checks do not erase it. Its raw usage remains in the table, but **no valid normalized TypeScript performance comparison or automatic activation follows**.

## Guardrails and retained corrections

Each attempt allowed at most eight physical requests, 480 seconds and 1,200,000 cumulative serialized request bytes. Observed input/output thresholds were 225,000/16,384 tokens. These stop subsequent requests; they are not hard per-request token limits. Final in-flight input overshoots were 2,392 (B-P), 8,112 (B-T) and 27,793 (C-T), all retained in the totals. Each incomplete T run then had three local refusals, not extra physical requests. No model or timeout budget was increased.

Prospective candidate thresholds were frozen from completed baselines: wall `min(480, max(2*B, B+30))`, input `min(225000, max(1.5*B, B+20000))`, output `min(16384, max(2*B, B+1024))`. P limits were 257.04505717600114 seconds / 225000 input / 3490 output; F limits were 157.9465705580078 / 225000 / 2186. Both candidates met all three. T relative thresholds were unavailable because its baseline report was incomplete.

After C-T, only the remaining C-F/C-P environments gained prospective `npm_config_offline=true`, `npm_config_yes=false` and separate empty owned caches. Tasks, models, prompts, profiles, budgets, order and thresholds stayed fixed. Actual terminal-child probes established ordinary npm/npx rejection for absent and cached-but-not-installed synthetic packages. Two earlier cache-seeding failures remain retained. These defaults can be overridden and are not general shell/network containment. Independent postrun audits found no acquisition command, unchanged empty owned caches and absent default npm caches in C-F/C-P. The baseline P/F traces did not invoke npm; this prospective difference still limits causal interpretation.

Earlier preflight, full-skill-inventory and source-backed verification-cache corrections retain their failed receipts and predecessor hashes. Cache bytecode equivalence was checked before independent oracle imports; tracked source/tests/configuration never received an ignored-file exemption. The separate canonical characterization observer failure was recovered read-only without inventing missing receipt fields or warming measured roots. Numerical gate drafts with rounding/remaining-call wording errors were corrected before candidate dispatch. None of these preparations adds a successful model sample.

## Scoped decision

This is one pair per task, measuring the combined stack, with provider/order variation and no causal or universal performance claim. Retain native diagnostic fixes and truthful fallback. Optional Python navigation is supported for the measured builder-medium task; automatic TypeScript expansion is excluded by this evidence.

The initial proposal remains **pending integration and live review**: optional Python-only scoped MCP for builder-medium (Sol/medium), plus reviewer (Astra/high) based separately on A4's functional read-only Python review. There is no A5 reviewer performance sample. Preserve the existing two bound profiles, defer automatic MCP enablement for the other five original roles, and preserve current native settings including TypeScript. Owned skill/guidance delivery is a separate concern.

The existing provisioner selects both languages, so a concrete reviewed per-profile language option is required before any Python-only rollout claim: [infrastructure issue 557](https://github.com/cermm/wc-infrastructure/issues/557). It must preserve legacy defaults and profile bindings. This report changes no product runtime, profile or live configuration.
