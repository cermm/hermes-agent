"""Healing of empty-name tool calls when loading a persisted session (#4662).

A session persisted by an older build (before the write-side guard in
``_flush_messages_to_session_db``) could carry an assistant ``tool_call`` whose
``function.name`` is empty. Replaying that nameless call makes strict
OpenAI-compatible providers 400 ("Tool '' does not exist" / invalid function
call), and on the Responses adapter the nameless call is silently dropped while
its result is still emitted, orphaning the call_id. Once stored, retries don't
help — the same poison is replayed on every request.

``get_messages_as_conversation`` drops empty-name tool calls at load time so a
poisoned session stays resumable. Empty *arguments* are intentionally left
intact — the pre-call repair pass normalizes them to ``"{}"`` rather than
dropping the whole call.
"""
import uuid
from pathlib import Path

from hermes_state import SessionDB


def _tc_name(tc: dict) -> str:
    return tc.get("name") or (tc.get("function") or {}).get("name") or ""


def test_empty_name_tool_call_dropped_on_load(tmp_path: Path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = db.create_session(session_id=str(uuid.uuid4()), source="cli")
    db.append_message(sid, role="user", content="hi")
    db.append_message(
        sid,
        role="assistant",
        content=None,
        tool_calls=[
            {"id": "bad", "type": "function",
             "function": {"name": "", "arguments": '{"x": 1}'}},
            {"id": "ok", "type": "function",
             "function": {"name": "terminal", "arguments": '{"cmd": "ls"}'}},
        ],
    )
    db.append_message(sid, role="tool", tool_call_id="ok", content="listing")

    conv = db.get_messages_as_conversation(sid)
    asst = [m for m in conv if m.get("role") == "assistant"][0]
    names = [_tc_name(tc) for tc in asst.get("tool_calls", [])]
    assert names == ["terminal"]


def test_all_empty_name_yields_no_tool_calls_on_load(tmp_path: Path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = db.create_session(session_id=str(uuid.uuid4()), source="cli")
    db.append_message(
        sid,
        role="assistant",
        content="text",
        tool_calls=[
            {"id": "c1", "type": "function",
             "function": {"name": "", "arguments": "{}"}},
        ],
    )

    conv = db.get_messages_as_conversation(sid)
    asst = [m for m in conv if m.get("role") == "assistant"][0]
    assert asst.get("tool_calls", []) == []


def test_empty_arguments_call_survives_load(tmp_path: Path):
    # Empty args are repaired downstream, not dropped here.
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = db.create_session(session_id=str(uuid.uuid4()), source="cli")
    db.append_message(
        sid,
        role="assistant",
        content=None,
        tool_calls=[
            {"id": "c9", "type": "function",
             "function": {"name": "terminal", "arguments": ""}},
        ],
    )

    conv = db.get_messages_as_conversation(sid)
    asst = [m for m in conv if m.get("role") == "assistant"][0]
    assert len(asst.get("tool_calls", [])) == 1
    assert _tc_name(asst["tool_calls"][0]) == "terminal"


def test_flat_dict_empty_name_dropped_on_load(tmp_path: Path):
    # Some persisted rows used the flat {"name": ...} shape rather than nested.
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = db.create_session(session_id=str(uuid.uuid4()), source="cli")
    db.append_message(
        sid,
        role="assistant",
        content=None,
        tool_calls=[
            {"name": "", "arguments": "{}"},
            {"name": "read_file", "arguments": '{"path": "a"}'},
        ],
    )

    conv = db.get_messages_as_conversation(sid)
    asst = [m for m in conv if m.get("role") == "assistant"][0]
    names = [_tc_name(tc) for tc in asst.get("tool_calls", [])]
    assert names == ["read_file"]
