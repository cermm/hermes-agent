# Issue tracker: GitHub

Core-source issues and specs for this fork live in https://github.com/cermm/hermes-agent. Use explicit `gh --repo cermm/hermes-agent` (or `gh api repos/cermm/hermes-agent/...`) for every tracker operation. Push campaign branches to the existing `fork` remote; open PRs against this fork's `main`. Preserve the NousResearch `origin` as upstream. Automatic origin inference and the canonical allocator's source namespace are not fork-publication authorization.

Read the issue, comments and existing labels before work. Use body files for multiline issues, comments and PR descriptions. Publish specifications as issues; PRs as a request surface: no. Keep existing campaign/control-plane relationships rather than inventing a tracker. Use native sub-issues and issue dependencies with numeric database IDs and read back mutations. Use a documented body fallback only after recording an actual unsupported/permission response.

Wayfinding maps use only `wayfinder:map`; decision children use only `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling` or `wayfinder:task`. Claim a decision with its assignee before work; query open unclaimed children with no open blockers. Resolve through a comment, closure and named context pointer in the map. Decisions remain distinct from implementation delivery.

Committed reviews use an explicit verified base and originating spec/issue. Review dirty work separately; HEAD-only comparison excludes it. Preserve repository and branch-protection controls, required checks, independent technical gates and fresh remote content proof. Use the canonical test runner. Integration, installed acceptance and issue closure are separate facts; never bypass protection or invent approval/deployment evidence.
