# Local Hermes Repair And Astra Routing

This records Michal's authorized September 5, 2026 repair and model-policy deployment. No conversation databases, credentials, private configuration backups, or authentication files are committed.

## Runtime Repair

- Bounded the startup integrity scan to prevent scan/watchdog/restart loops on large databases. Incomplete diagnostics remain distinct from corruption.
- Corrected the external completion-dispatch plugin import. The exact already-applied change is recorded in `kanban-completion-import.patch`; paths are relative to the Hermes home, not this repository.
- Finished the pre-existing search-index migration and compacted state.db from 41,112.7 MiB to 10,467.4 MiB. All 20,469 existing sessions and 787,646 messages were preserved. Full quick_check, foreign-key checks, and search passed; the WAL was empty afterward. Later smoke tests created new diagnostic sessions.
- The historical cleanup helper and repair report in this directory are records of a completed offline operation. Do not run the helper against a live gateway.
- Refreshed the gateway service definition, removed temporary maintenance/restart holds, and restored its normal systemd restart policy. Windows legacy watchdog/autostart scripts remain disabled. A hidden `wsl.exe -d Ubuntu -u michal --exec /bin/sleep infinity` process keeps this Windows session's WSL runtime alive.

## Model Policy

The applied non-secret configuration is `config/local-astra-routing.json`. Reapply it with:

```sh
venv/bin/python scripts/apply_local_astra_routing.py --apply
```

The script renders every selected configuration before applying changes, backs up private originals under the Hermes home, preserves credentials and unrelated settings, and is idempotent. It requires the installed ruamel.yaml package. Restart the gateway to load code and model defaults after applying.

| Role | Default Model |
| --- | --- |
| Main, planner, final reviewer | GPT-6 Astra |
| Builder-mini and generic builder | GPT-5.6 Luna |
| Builder-low | GPT-5.6 Terra |
| Builder-medium and reviewersol | GPT-5.6 Sol |
| Builder-high | GPT-6 Astra |

Workers use ascending fallbacks ending at Astra. Main/planner/reviewer delegation starts on Luna with Terra, Sol, and Astra as explicit child fallbacks. Explicit child fallback configuration works even with a pinned provider; absent configuration retains the previous pin-isolation behavior.

`delegation.escalate_on_validation_failure: true` enables automatic escalation when a delegated result fails its caller-provided output_schema. Retries carry validation errors and conversation history, remain on the escalated runtime across retry turns, and stop after at most three higher-tier attempts. An interrupted or terminally failed run is not treated as a formatting-repair request. Without opt-in, the original one-retry behavior remains.

Test or reviewer failures in the Kanban workflow are handled by the committed orchestrator instructions: send rework, with the actual evidence and existing work, to the next builder tier. This is distinct from machine-enforced output-schema validation. It does not invent a generic quality oracle: tasks still need explicit acceptance criteria, actual tests, and independent review. Explicit task pins and permission/dependency/review gates remain intact; a schema pass alone is not proof that the work is correct.

## Verification

- 133 lifecycle, delegation, output-schema, and policy tests passed.
- Live probes succeeded for Astra, Luna, Terra, and Sol using the existing openai-codex provider.
- A default-model smoke test, without a model override, returned ASTRA_DEFAULT_OK; its session record confirmed gpt-6-astra.
- A live delegate_task smoke test returned DELEGATION_OK. Session records confirmed an Astra parent and a Luna child with an output-schema contract.
- Telegram and Discord reconnected after deployment. The local dashboard returned HTTP 200 after the repair.
- All 18 valid Kanban databases passed integrity checks. An old corrupt-seed recovery snapshot is excluded by normal board discovery and was preserved.

Email remains disabled by the existing configuration. Optional unconfigured integrations and pre-existing skill command collisions may still emit warnings.

Official model guidance checked for the requested target: https://developers.openai.com/api/docs/models/gpt-6-astra and https://developers.openai.com/api/docs/guides/latest-model. The provider already uses Responses transport; Astra and tool routing were verified locally rather than changing authentication or endpoints.
