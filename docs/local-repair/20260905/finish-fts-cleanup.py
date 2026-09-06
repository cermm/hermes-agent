"""Finish the supported FTS optimizer while the gateway is stopped."""
import sys
import time
import subprocess

sys.path.insert(0, "/home/michal/.hermes/hermes-agent")
from hermes_state import SessionDB

assert subprocess.run(
    ["systemctl", "--user", "is-active", "--quiet", "hermes-gateway.service"]
).returncode != 0, "Stop the gateway before offline maintenance"
db = SessionDB()
# The normal duty cycle yields to a live gateway; this is offline maintenance.
db._FTS_REBUILD_DUTY_FACTOR = 0.0
db._FTS_REBUILD_MIN_PAUSE = 0.0
db._FTS_REBUILD_CHUNK_ROWS = 10000
last_report = 0.0


def progress(info):
    global last_report
    now = time.monotonic()
    if now - last_report >= 30 or info["phase"] == "done":
        markers = db._conn.execute(
            "SELECT key,value FROM state_meta WHERE key LIKE 'fts_teardown_%'"
        ).fetchall()
        print(info, [tuple(row) for row in markers], flush=True)
        last_report = now


try:
    assert db.get_meta("fts_rebuild_high_water") is None, "Backfill must finish first"
    assert not db._fts_external_index_empty_with_messages(db._conn), "New index must contain data"
    before = tuple(db._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                   for table in ("sessions", "messages"))
    allowed = {"fts_v22_trash_messages_fts_trigram_" + suffix
               for suffix in ("data", "idx", "content", "docsize", "config")}
    trash = db._conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name LIKE 'fts_v22_trash_%'"
    ).fetchall()
    assert all(name in allowed and sql.startswith("CREATE TABLE") for name, sql in trash)
    assert not db._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') AND sql LIKE '%fts_v22_trash_%'"
    ).fetchone(), "Old index still referenced"
    for table, _ in trash:
        print("Dropping obsolete table", table, flush=True)

        def drop(conn):
            conn.execute(f'DROP TABLE "{table}"')
            conn.execute("DELETE FROM state_meta WHERE key=?", (f"fts_teardown_{table}_progress",))

        db._execute_write(drop)
        print("Dropped", table, flush=True)
    result = db.optimize_fts_storage(progress_cb=progress, vacuum=False)
    after = tuple(db._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("sessions", "messages"))
    assert before == after, (before, after)
    print("Preserved sessions/messages:", after, flush=True)
    print("RESULT", result, flush=True)
    if not result.get("ok"):
        raise SystemExit(1)
finally:
    db.close()
