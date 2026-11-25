"""The triage log: each move of a triage run as a line in a file of its own.

It names accounts and households, so it is never in the playbook folder. `apply` is the
only code that writes a triage table, for a move as it is made and for a replay alike.
"""

from pathlib import Path

from sqlalchemy import Connection, delete, exists, insert, select, update

from backend import database as db
from backend.chainlog import ChainLog, Line
from backend.journal import CannotApply, Kept, moved_one

KEY = "triage_log"
RUN_STARTED = "run_started"
RUN_UNDONE = "run_undone"
STARTED_AGAIN = "run_started_again"
RESTART_UNDONE = "restart_undone"
TOOL_CALLS = "tool_calls"
FELL_BACK = "note_fell_back"
COMPLETED = "run_completed"
FAILED = "run_failed"
DIGEST_KEY_MADE = "digest_key_made"


def kept(path: Path) -> Kept:
    return Kept(ChainLog(KEY, path, create=True), apply)


def _working(data: dict) -> tuple:
    return (
        db.runs.c.id == data["run_id"],
        db.runs.c.state == "working",
        db.runs.c.attempt == data["attempt"],
    )


def _update_run(connection: Connection, where: tuple, **values) -> None:
    moved_one(connection.execute(update(db.runs).where(*where).values(**values)).rowcount)


def _run_started(connection: Connection, at: str, data: dict) -> None:
    run_id, used = data["run_id"], data["playbook_set"]
    connection.execute(
        insert(db.runs).values(
            id=run_id,
            attempt=data["attempt"],
            state="working",
            date=data["date"],
            cutoff=data["cutoff"],
            set_id=used["id"],
            set_version=used["version"],
            set_hash=used["hash"],
            started_at=at,
            heard_at=at,
        )
    )
    for read in data["reads"]:
        connection.execute(insert(db.reads).values(run_id=run_id, **read))
    for each in data["exceptions"]:
        found = {name: value for name, value in each.items() if name != "state"}
        connection.execute(insert(db.exceptions).values(run_id=run_id, **found))
        connection.execute(
            insert(db.notes).values(
                exception_id=each["id"],
                run_id=run_id,
                state=each["state"],
                settled_at=None if each["state"] == "pending" else at,
            )
        )
    for notice in data["notices"]:
        connection.execute(insert(db.notices).values(run_id=run_id, **notice))


def _run_undone(connection: Connection, _: str, data: dict) -> None:
    removed = connection.execute(
        delete(db.runs).where(*_working(data), ~exists().where(db.steps.c.run_id == data["run_id"]))
    )
    moved_one(removed.rowcount)
    for table in (db.notes, db.exceptions, db.reads, db.notices):
        connection.execute(delete(table).where(table.c.run_id == data["run_id"]))


def _started_again(connection: Connection, at: str, data: dict) -> None:
    where = (
        db.runs.c.id == data["run_id"],
        db.runs.c.state == "working",
        db.runs.c.attempt == data["before"],
        db.runs.c.heard_at == data["last_heard"],
    )
    _update_run(connection, where, attempt=data["attempt"], heard_at=at)


def _restart_undone(connection: Connection, _: str, data: dict) -> None:
    # Any move the agent made would have been heard. The attempt's number is not given back.
    where = (*_working(data), db.runs.c.heard_at == data["asked_at"])
    _update_run(connection, where, heard_at=data["last_heard"])


def _settle(connection: Connection, at: str, data: dict) -> None:
    settled = data["settled"]
    moved = connection.execute(
        update(db.notes)
        .where(
            db.notes.c.run_id == data["run_id"],
            db.notes.c.exception_id == data["note_id"],
            db.notes.c.state == "pending",
            exists().where(*_working(data)),
        )
        .values(state=settled["state"], chosen=settled["chosen"], settled_at=at)
    )
    moved_one(moved.rowcount)


def add_steps(connection: Connection, at: str, data: dict) -> None:
    """A reply's calls as steps on its run. The ledger's lines use it too."""
    common = {
        "run_id": data["run_id"],
        "attempt": data["attempt"],
        "exception_id": data["note_id"],
        "turn": data["turn"],
        "move": data["move"],
        "at": at,
    }
    connection.execute(insert(db.steps), [common | each for each in data["calls"]])


def _tool_calls(connection: Connection, at: str, data: dict) -> None:
    if data["heard"]:
        _update_run(connection, _working(data), heard_at=at)
    add_steps(connection, at, data)
    if data["settled"] is not None:
        _settle(connection, at, data)


def _fell_back(connection: Connection, at: str, data: dict) -> None:
    _update_run(connection, _working(data), heard_at=at)
    _settle(connection, at, data)


def _completed(connection: Connection, at: str, data: dict) -> None:
    where = (
        *_working(data),
        ~exists().where(db.notes.c.run_id == data["run_id"], db.notes.c.state == "pending"),
    )
    _update_run(connection, where, state="completed", heard_at=at, finished_at=at)


def _failed(connection: Connection, at: str, data: dict) -> None:
    where, reason = _working(data), data["reason"]
    _update_run(connection, where, state="failed", failure=reason, heard_at=at, finished_at=at)


_MOVES = {
    RUN_STARTED: _run_started,
    RUN_UNDONE: _run_undone,
    STARTED_AGAIN: _started_again,
    RESTART_UNDONE: _restart_undone,
    TOOL_CALLS: _tool_calls,
    FELL_BACK: _fell_back,
    COMPLETED: _completed,
    FAILED: _failed,
}


def apply(connection: Connection, line: Line) -> None:
    if line.event == DIGEST_KEY_MADE:
        if connection.execute(select(db.digest_keys.c.line)).first():
            raise CannotApply("the digest's secret is made once")
        secret = line.data["secret"]
        connection.execute(insert(db.digest_keys).values(line=line.line, secret=secret))
        return
    if line.event not in _MOVES:
        raise CannotApply("there is no such event")
    _MOVES[line.event](connection, line.at, line.data)
