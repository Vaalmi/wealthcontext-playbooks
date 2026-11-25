"""How a durable move is written, and how the database follows the logs.

A move is one line in a log, written inside the transaction that makes it: begin, make
the conditional update, append the line and flush it, commit. The line is the commit
point. The live move and a replay run the same `apply`, so the database can always be
made again from the logs.
"""

import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, Engine, exc, insert, select, update

from backend import database as db
from backend.chainlog import START, ChainLog, Line, LogInvalid, Position, end_of, following


class CannotApply(Exception):
    """A move does not hold from where the records are."""


def moved_one(count: int) -> None:
    if count != 1:
        raise CannotApply("the record is not in the state this move starts from")


def _nothing(_: list[Line]) -> None:
    pass


@dataclass(frozen=True)
class Kept:
    """A log and what its lines do to the database."""

    log: ChainLog
    apply: Callable[[Connection, Line], None]
    # Refuses lines that cannot be used. It changes nothing.
    check: Callable[[list[Line]], None] = _nothing
    # Removes what an interrupted move left beside the log.
    tidy: Callable[[list[Line]], None] = _nothing

    def record(self, connection: Connection, event: str, who: str, at: str, data: dict) -> Line:
        """Make a move and write its line. Nothing that can refuse may follow it."""
        line = following(self.log.tail, event, who, at, data)
        self.apply(connection, line)
        _set_applied(connection, self.log.key, line.position())
        self.log.write(line)
        return line


def _applied(connection: Connection, key: str) -> Position:
    row = connection.execute(select(db.applied).where(db.applied.c.log == key)).first()
    return Position(row.lines, row.hash) if row else START


def _set_applied(connection: Connection, key: str, position: Position) -> None:
    values = position._asdict()
    moved = connection.execute(update(db.applied).where(db.applied.c.log == key).values(**values))
    if moved.rowcount == 0:
        connection.execute(insert(db.applied).values(log=key, **values))


def _refuse_if_ahead(connection: Connection, kept: Kept, lines: list[Line]) -> None:
    applied = _applied(connection, kept.log.key)
    if applied.lines > len(lines):
        raise LogInvalid(
            f"{kept.log.name} line {len(lines) + 1} is missing: "
            f"the database has applied {applied.lines} lines"
        )
    if end_of(lines[: applied.lines]) != applied:
        raise LogInvalid(f"{kept.log.name} line {applied.lines} is not the one the database has")


def _refuse_if_unlogged(connection: Connection) -> None:
    if connection.execute(select(db.applied).limit(1)).first():
        return
    for table in db.metadata.sorted_tables:
        if connection.execute(select(table).limit(1)).first():
            raise LogInvalid(
                "The database holds records that are in no log. "
                "Move it aside and start again with an empty one"
            )


def _bring_up(connection: Connection, kept: Kept, lines: list[Line]) -> None:
    for line in lines[_applied(connection, kept.log.key).lines :]:
        where = f"{kept.log.name} line {line.line} cannot be applied"
        try:
            kept.apply(connection, line)
        except CannotApply as refused:
            raise LogInvalid(f"{where}: {refused}") from None
        except (KeyError, TypeError, ValueError, exc.IntegrityError):
            raise LogInvalid(where) from None  # what was wrong could name an account
        _set_applied(connection, kept.log.key, line.position())


class Journal:
    """The database and the logs it follows. Every read and move goes through `transaction`."""

    def __init__(self, engine: Engine, kept: Sequence[Kept]):
        self._engine = engine
        self._kept = kept
        # One transaction at a time, as SQLite allows, and none across a rebuild.
        self._lock = threading.RLock()

    @classmethod
    def start(cls, database: Path, *kept: Kept) -> "Journal":
        """Verify everything first. Only then mend, remove and apply what is owed."""
        engine = None
        try:
            found = []
            for each in kept:
                lines = each.log.open()
                each.check(lines)
                found.append((each, lines))
            engine = db.open_database(database)
            with engine.begin() as connection:
                db.create(connection)
                _refuse_if_unlogged(connection)
                for each, lines in found:
                    _refuse_if_ahead(connection, each, lines)
                if db.outdated(connection):
                    db.remake(connection)
                for each, lines in found:
                    each.log.repair()
                    each.tidy(lines)
                    _bring_up(connection, each, lines)
        except BaseException:
            if engine is not None:
                engine.dispose()
            for each in kept:
                each.log.close()
            raise
        return cls(engine, kept)

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self._lock, self._engine.begin() as connection:
            # A line whose commit failed still happened, so it is applied before anything else.
            for each in self._kept:
                if _applied(connection, each.log.key) != each.log.tail:
                    lines = each.log.read()
                    _refuse_if_ahead(connection, each, lines)
                    _bring_up(connection, each, lines)
            yield connection

    def rebuild(self) -> dict[str, int]:
        """Make the whole database again from the logs, or change nothing."""
        with self._lock:
            found = []
            for each in self._kept:
                lines = each.log.read()
                each.check(lines)
                found.append((each, lines))
            # The file may have been deleted under the connections that are open.
            self._engine.dispose()
            with self._engine.begin() as connection:
                db.create(connection)
                _refuse_if_unlogged(connection)
                for each, lines in found:
                    _refuse_if_ahead(connection, each, lines)
                db.remake(connection)
                for each, lines in found:
                    _bring_up(connection, each, lines)
        return {each.log.key: len(lines) for each, lines in found}

    def close(self) -> None:
        with self._lock:
            self._engine.dispose()
            for each in self._kept:
                each.log.close()
