"""An append-only file whose every line carries the hash of the line before it.

A line also carries its own hash, so that a change to the last line is seen too. The
chain shows a change. It cannot show a whole chain written again, which is why the
database keeps the hash of the last line it applied.
"""

import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

# Every line begins so, as its keys are sorted. A line a crash cut short begins so too.
BEGINS = b'{"at":"'


class LogInvalid(Exception):
    """A log cannot be used. The message names the file, and the line where there is one."""


class Position(NamedTuple):
    lines: int
    hash: str


START = Position(0, "0" * 64)


class Line(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    at: StrictStr
    data: dict[str, Any]
    event: StrictStr
    hash: StrictStr
    line: StrictInt
    prev: StrictStr
    who: StrictStr

    def position(self) -> Position:
        return Position(self.line, self.hash)

    def written(self) -> bytes:
        return _canonical(self.model_dump()) + b"\n"


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _hash(body: dict) -> str:
    return hashlib.sha256(_canonical(body)).hexdigest()


def following(before: Position, event: str, who: str, at: str, data: dict) -> Line:
    """The line after a position. Its data is exactly what reading it back would give."""
    body = {
        "at": at,
        "data": json.loads(_canonical(data)),
        "event": event,
        "line": before.lines + 1,
        "prev": before.hash,
        "who": who,
    }
    return Line(**body, hash=_hash(body))


def end_of(lines: list[Line]) -> Position:
    return lines[-1].position() if lines else START


def _why_not(content: bytes, before: Position) -> tuple[Line | None, str]:
    try:
        line = Line.model_validate(json.loads(content))
        as_written = line.written() == content + b"\n"
    except (ValueError, TypeError, RecursionError):
        return None, "is not a whole line"
    if not as_written or _hash(line.model_dump(exclude={"hash"})) != line.hash:
        return None, "has been changed: its hash does not hold"
    if (line.line, line.prev) != (before.lines + 1, before.hash):
        return None, "does not follow the line before it"
    return line, ""


def flush_folder(folder: Path) -> None:
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_all(descriptor: int, content: bytes) -> None:
    while content:
        content = content[os.write(descriptor, content) :]


@dataclass(frozen=True)
class _Found:
    lines: list[Line]
    size: int  # of the whole lines
    unended: bytes  # what follows them: a line cut short, or a whole one with no line end
    whole: bool


class ChainLog:
    """One log, held by one backend at a time. `key` is its name in the database."""

    def __init__(self, key: str, path: Path, *, create: bool = False):
        self.key = key
        self.name = path.name
        self._path = path
        self._create = create
        self._descriptor: int | None = None
        self._found: _Found | None = None
        self._size = 0
        self._uncertain = False
        self.tail = START

    def open(self) -> list[Line]:
        """Lock and verify the log. Nothing in it is changed until `repair`."""
        flags = os.O_RDWR | os.O_APPEND
        try:
            if self._create:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                flags |= os.O_CREAT
            self._descriptor = os.open(self._path, flags, 0o600)
            if self._create:
                flush_folder(self._path.parent)
        except OSError:
            raise LogInvalid(f"{self.name} cannot be opened") from None
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LogInvalid(f"{self.name} is held by another backend") from None
        self._found = self._verified()
        self.tail = end_of(self._found.lines)
        return self._found.lines

    def repair(self) -> None:
        """Finish what a crash left: drop a line cut short, end a whole one."""
        assert self._descriptor is not None and self._found is not None
        found, self._found = self._found, None
        self._size = found.size + len(found.unended)
        if found.whole:
            write_all(self._descriptor, b"\n")
            self._size += 1
        elif found.unended:
            os.ftruncate(self._descriptor, found.size)
            self._size = found.size
        # Also for a line whose writer was killed before it could flush it.
        os.fsync(self._descriptor)

    def read(self) -> list[Line]:
        """Every line, verified again."""
        lines = self._verified().lines
        if end_of(lines) != self.tail:
            raise LogInvalid(f"{self.name} changed while the backend ran: start it again")
        return lines

    def check_writable(self) -> None:
        if self._descriptor is None or self._uncertain:
            raise LogInvalid(f"{self.name} cannot be written: start the backend again")
        held = os.fstat(self._descriptor)
        try:
            named = os.stat(self._path)
        except OSError:
            named = None
        if named is None or not os.path.samestat(held, named) or named.st_size != self._size:
            raise LogInvalid(f"{self.name} changed while the backend ran: start it again")

    def write(self, line: Line) -> None:
        """Append a line and flush it to disk. When this returns, the move has happened."""
        self.check_writable()
        assert self._descriptor is not None
        content = line.written()
        try:
            write_all(self._descriptor, content)
            os.fsync(self._descriptor)
        except BaseException:
            try:
                os.ftruncate(self._descriptor, self._size)
                os.fsync(self._descriptor)
            except OSError:
                # The line may be there. The next start finds out, and nothing is added before.
                self._uncertain = True
            raise
        self._size += len(content)
        self.tail = line.position()

    def close(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None

    def _verified(self) -> _Found:
        try:
            content = self._path.read_bytes()
        except OSError:
            raise LogInvalid(f"{self.name} cannot be read") from None
        *ended, unended = content.split(b"\n")
        lines: list[Line] = []
        for number, each in enumerate(ended, start=1):
            line, why = _why_not(each, end_of(lines))
            if line is None:
                raise LogInvalid(f"{self.name} line {number} {why}")
            lines.append(line)
        size = len(content) - len(unended)
        if not unended:
            return _Found(lines, size, unended, whole=False)

        before = end_of(lines)
        last, _ = _why_not(unended, before)
        if last is not None:
            return _Found([*lines, last], size, unended, whole=True)
        # A crash leaves the start of a line. A whole line with another byte at its end, or
        # anything that is not the start of one, was put there.
        ending_changed = _why_not(unended[:-1], before)[0] is not None
        if ending_changed or unended[: len(BEGINS)] != BEGINS[: len(unended)]:
            raise LogInvalid(f"{self.name} line {len(lines) + 1} has been changed at its end")
        return _Found(lines, size, unended, whole=False)
