# Hermes Post-Update Repair, 2026-09-05

## Outcome

Hermes v0.21.0 (2026.8.31), code b0ab2e163a, is running on the existing update. The configured model remains GPT-5.5 via openai-codex. No rollback was performed.

## Findings And Changes

- The gateway stack trace showed startup blocked in gateway/lifecycle_ledger.py at PRAGMA quick_check(1), following an unclean exit. The unrestricted scan outlasted the startup watchdog and produced a repeated exit/start cycle.
- Added a 10-second SQLite progress-handler budget and a bounded connection lock wait. Incomplete or failed checks are recorded distinctly from detected corruption. The full offline integrity check is still available.
- Corrected the external kanban_completion_dispatch_wake plugin's moved import to hermes_cli.kanban_db_dispatch.
- Finished the interrupted FTS storage migration. The backfill had already completed; five verified obsolete, unreferenced plain shadow tables remained. Removed those tables transactionally while the gateway was stopped, then finalized through Hermes's optimizer.
- Ran the supported sessions optimize command to merge both active FTS indexes, VACUUM, checkpoint, and truncate the WAL.
- Startup restored the missing Telegram and Discord dependencies. The installed environment passed dependency validation afterward.
- Removed temporary maintenance holds and restored normal systemd service recovery. The legacy Windows watchdog and autostart scripts remain disabled, as previously requested. Started a hidden WSL keep-alive process for this Windows session.

## Verification

- 17 lifecycle regression tests passed, including healthy/corrupt databases and the new timeout behavior.
- A two-second diagnostic budget against the original live database returned check-incomplete after 2.07 seconds.
- Final full SQLite quick_check: ok. Foreign-key check: no violations. Full-text search returned a result.
- Cleanup and compaction preserved 20,469 sessions and 787,646 messages. The subsequent local diagnostic created its own new session.
- Database shrank from 41,112.7 MiB to 10,467.4 MiB, reclaiming 30,645.4 MiB. WAL size was zero after compaction and validation. WSL free space was approximately 116 GiB.
- All 18 valid Kanban board database files passed quick_check. An old corrupt-seed snapshot is excluded by Hermes's board-name validation and was preserved.
- Current gateway PID at verification: 2593; service active, runtime running, zero restarts, and zero new ERROR log entries since startup at 22:30:29 CEST.
- Current-process Telegram and Discord status: connected. Telegram polling confirmed healthy. Both bot authentication probes succeeded.
- Local GPT-5.5 diagnostic returned HERMES_OK.
- Dashboard at http://127.0.0.1:9119/ returned HTTP 200.

## Configuration Notes

Email remains explicitly disabled in the existing configuration. Optional integrations without requirements/configuration and existing skill command-name collisions still generate warnings; these were not treated as active connector failures. Historical platform entries may remain in gateway_state.json; current ownership is determined by writer_pid and writer_start_time, not the stale state field alone.

The startup fix is a local source patch, saved alongside this report as hermes-startup-integrity-timeout-20260905.patch. Preserve or reapply it during a future update until an equivalent upstream fix is installed. The one-time cleanup helper is scripts/finish-fts-cleanup-20260905.py.
