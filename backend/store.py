"""The playbook folder: the desks, a file for each version, and the ledger.

The folder is the durable record and the database follows it. A version's file is written
once and never again. Which versions are in use is what the ledger's lines add up to. The
ledger also holds what happens to firm sources, corrections, gap reports, curator runs
and proposals, and those lines leave the playbook set as it is.
"""

import json
import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import Field, StrictInt, StringConstraints, TypeAdapter
from sqlalchemy import Connection, func, insert, select, update

from backend import database as db
from backend import ledger_events, matching
from backend.chainlog import ChainLog, Line, flush_folder, write_all
from backend.journal import CannotApply, Kept, moved_one
from backend.playbook_format import Fixed, Playbook, Slug, Step, content_hash, parse
from backend.vocabulary import OWNER

log = logging.getLogger(__name__)

LEDGER = "ledger.jsonl"
DESKS = "desks.yaml"
FOUNDED = "playbooks_founded"
PUT_IN_USE = "versions_put_in_use"
# The playbook owner's approval of the founding versions, which the first line left unapproved.
FOUNDING_APPROVED = "founding_approved"
RETIRED = "version_retired"

DeskName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z &-]{0,63}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
RecordId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
VersionNumber = Annotated[StrictInt, Field(ge=1)]
# A closed list, as no check can know personal data in free text, and the ledger is permanent.
RetireReason = Literal[
    "source_withdrawn", "wrong_action", "no_longer_applies", "put_in_use_in_error"
]
VersionState = Literal["in_use", "superseded", "retired"]
_VERSION_FILE = re.compile(r"[1-9][0-9]*\.yaml")
_PLAYBOOK_FOLDER = re.compile(r"[a-z]+(-[a-z]+)*")


class StoreInvalid(Exception):
    """The playbook folder cannot be used. The message names the file."""


class Overlap(CannotApply):
    """Two steps in use would fit one exception."""


class StepInUse(Fixed):
    """A step in use, with what a note shows of it. A run keeps a copy of those it was given."""

    playbook: Slug
    version: StrictInt
    desk: DeskName
    step: Step
    # Who approved the version. None says that no approval is recorded, as for a founding one.
    approved_by: str | None = None


class VersionInUse(Fixed):
    id: Slug
    version: StrictInt
    sha256: str


class RestsOn(Fixed):
    """A firm source or gap report a version rests on, by its reference and its hash."""

    id: Slug
    sha256: Sha256


class VersionWritten(Fixed):
    """A version as the ledger names it: its file is known by its hash."""

    id: Slug
    version: VersionNumber
    sha256: Sha256
    rests_on: list[RestsOn]

    def file(self) -> str:
        return _file(self.id, self.version)


class Founding(Fixed):
    id: Slug
    desks_sha256: Sha256
    # None says that nobody approved them. Such a version gives out no action.
    approved_by: Literal["playbook_owner"] | None
    versions: list[VersionWritten]


class Ruling(Fixed):
    """The playbook owner's ruling on one disagreement among sources, for one change."""

    # The change's number in the proposal. None: a disagreement no change rests on.
    change: StrictInt | None
    between: list[Slug]
    ruled_for: Slug


class ApprovedProposal(Fixed):
    """The approval in the line that puts versions in use: of what, on which gate run."""

    proposal_id: RecordId
    sha256: Sha256
    gate_run_id: RecordId
    rulings: list[Ruling]
    # The gap reports it resolves.
    resolved: list[RecordId]


class PutInUse(Fixed):
    versions: list[VersionWritten]
    approval: ApprovedProposal | None = None


class FoundingLine(Fixed):
    line: Literal[1]
    hash: Sha256


class FoundingApproval(Fixed):
    """The founding versions approved, named exactly as the first line names them."""

    founding: FoundingLine
    versions: list[VersionWritten]
    desks_sha256: Sha256


class Retirement(Fixed):
    playbook: Slug
    version: VersionNumber
    reason: RetireReason
    restored: VersionNumber | None


@dataclass(frozen=True)
class PlaybookSet:
    id: str
    version: int
    hash: str
    versions: tuple[VersionInUse, ...]
    steps: tuple[StepInUse, ...]
    # The playbooks in use whose version has a recorded approval: the version's own, which a
    # version with no step has too.
    approved: frozenset[str] = frozenset()

    def has_approval(self, playbook: str) -> bool:
        return playbook in self.approved

    def with_code(self, code: str) -> list[StepInUse]:
        return [each for each in self.steps if each.step.code == code]


def steps_of(row: Any) -> list[StepInUse]:
    """A version's steps as kept, each with the version's own approval, which can come later."""
    return [StepInUse.model_validate(each | {"approved_by": row.approved_by}) for each in row.steps]


def _file(playbook: str, version: int) -> str:
    return f"{playbook}/{version}.yaml"


def _read(folder: Path, shown: str) -> bytes:
    try:
        return (folder / shown).read_bytes()
    except OSError:
        raise StoreInvalid(f"{shown} cannot be read") from None


def _refuse_overlap(steps: Iterable[StepInUse]) -> None:
    for first, second in combinations(steps, 2):
        if matching.overlap(first.step, second.step):
            raise Overlap(
                f"{first.playbook} version {first.version} step {first.step.id} and "
                f"{second.playbook} version {second.version} step {second.step.id} overlap"
            )


def _is(playbook: str, version: int) -> tuple:
    return db.versions.c.playbook == playbook, db.versions.c.version == version


def _written_in(line: Line) -> list[VersionWritten]:
    if line.event == FOUNDED:
        return Founding.model_validate(line.data).versions
    if line.event == PUT_IN_USE:
        return PutInUse.model_validate(line.data).versions
    return []


class Store:
    def __init__(self, folder: Path):
        self._folder = folder
        self._desks: dict[str, str] = {}
        ledger = ChainLog("ledger", folder / LEDGER)
        self.kept = Kept(ledger, self._apply, self._check, self._tidy)

    @property
    def desks(self) -> Mapping[str, str]:
        return self._desks

    # Starting

    def _check(self, lines: list[Line]) -> None:
        """Every file the ledger names is there, as it was written."""
        if not lines or lines[0].event != FOUNDED:
            raise StoreInvalid(f"{LEDGER} line 1 does not name the playbook set")
        try:
            founding = Founding.model_validate(lines[0].data)
        except ValueError:
            raise StoreInvalid(f"{LEDGER} line 1 is not a founding of playbooks") from None
        content = _read(self._folder, DESKS)
        if content_hash(content) != founding.desks_sha256:
            raise StoreInvalid(f"{DESKS} has been changed: its hash is not the ledger's")
        try:
            desks = TypeAdapter(dict[Slug, DeskName]).validate_python(yaml.safe_load(content))
        except (ValueError, yaml.YAMLError):
            raise StoreInvalid(f"{DESKS} is not a valid list of desks") from None
        self._desks = desks
        for line in lines:
            try:
                written = _written_in(line)
            except ValueError:
                raise StoreInvalid(f"{LEDGER} line {line.line} names no valid versions") from None
            for each in written:
                self._steps_of(each)

    def _steps_of(self, written: VersionWritten, approved_by: str | None = None) -> list[StepInUse]:
        shown = written.file()
        content = _read(self._folder, shown)
        if content_hash(content) != written.sha256:
            raise StoreInvalid(f"{shown} has been changed: its hash is not the ledger's")
        try:
            playbook = parse(content)
        except (ValueError, yaml.YAMLError):
            raise StoreInvalid(f"{shown} is not a valid playbook") from None
        if (playbook.id, playbook.version) != (written.id, written.version):
            raise StoreInvalid(f"{shown} names another playbook or version")
        return self._as_used(playbook, approved_by)

    def _as_used(self, playbook: Playbook, approved_by: str | None = None) -> list[StepInUse]:
        used = []
        for step in playbook.steps:
            if step.tell not in self._desks:
                shown = _file(playbook.id, playbook.version)
                raise StoreInvalid(f"{shown} tells a desk that is not in {DESKS}")
            used.append(
                StepInUse(
                    playbook=playbook.id,
                    version=playbook.version,
                    desk=self._desks[step.tell],
                    step=step,
                    approved_by=approved_by,
                )
            )
        return used

    def _tidy(self, lines: list[Line]) -> None:
        self._remove_unnamed({each.file() for line in lines for each in _written_in(line)})

    def _remove_unnamed(self, named: set[str]) -> None:
        """A version file that no line names was left by a move that did not happen. So was
        the folder it leaves empty, of a playbook that no line names.
        """
        playbooks = {each.partition("/")[0] for each in named}
        for folder in list(self._folder.iterdir()):
            # Never through a link, which could lead out of the playbook folder.
            if folder.is_symlink() or not folder.is_dir():
                continue
            if not _PLAYBOOK_FOLDER.fullmatch(folder.name):
                continue
            for file in folder.iterdir():
                shown = f"{folder.name}/{file.name}"
                if _VERSION_FILE.fullmatch(file.name) and file.is_file() and shown not in named:
                    file.unlink()
                    flush_folder(folder)
                    log.warning("removed %s: no ledger line names it", shown)
            if folder.name not in playbooks and not any(folder.iterdir()):
                folder.rmdir()
                flush_folder(self._folder)

    # What each line does

    def _apply(self, connection: Connection, line: Line) -> None:
        if move := ledger_events.MOVES.get(line.event):
            move(connection, line)
            # No new set: the runs working under this one go on settling their notes.
            self._enter(connection, line)
            return
        approval = None
        if line.event == FOUNDED:
            founding = Founding.model_validate(line.data)
            if line.line != 1:
                raise CannotApply("playbooks are founded once")
            set_id = founding.id
            self._add(connection, founding.versions, founding.approved_by)
        elif line.event == PUT_IN_USE:
            set_id = self._set_row(connection).id
            put = PutInUse.model_validate(line.data)
            approval = put.approval
            self._add(connection, put.versions, line.who)
        elif line.event == RETIRED:
            set_id = self._set_row(connection).id
            self._retire(connection, line.line, Retirement.model_validate(line.data))
        elif line.event == FOUNDING_APPROVED:
            set_id = self._set_row(connection).id
            self._approve_founding(connection, FoundingApproval.model_validate(line.data), line.who)
        else:
            raise CannotApply("there is no such event")
        self._enter(connection, line)
        self._write_set(connection, line.line, set_id)
        if approval:
            ledger_events.approved(
                connection, line.line, approval.proposal_id, list(approval.resolved)
            )

    def _enter(self, connection: Connection, line: Line) -> None:
        connection.execute(
            insert(db.ledger_entries).values(
                line=line.line,
                at=line.at,
                who=line.who,
                event=line.event,
                data=line.data,
                hash=line.hash,
            )
        )

    def _add(
        self, connection: Connection, written: list[VersionWritten], approved_by: str | None
    ) -> None:
        for each in written:
            highest = self._highest(connection, each.id)
            if each.version <= highest:
                raise CannotApply("a version's number is above every one its playbook has had")
            connection.execute(
                update(db.versions)
                .where(db.versions.c.playbook == each.id, db.versions.c.state == "in_use")
                .values(state="superseded")
            )
            connection.execute(
                insert(db.versions).values(
                    playbook=each.id,
                    version=each.version,
                    sha256=each.sha256,
                    state="in_use",
                    steps=[
                        step.model_dump(mode="json") for step in self._steps_of(each, approved_by)
                    ],
                    approved_by=approved_by,
                )
            )

    def _retire(self, connection: Connection, line: int, retired: Retirement) -> None:
        moved = connection.execute(
            update(db.versions)
            .where(*_is(retired.playbook, retired.version), db.versions.c.state == "in_use")
            .values(state="retired", retired_line=line)
        )
        moved_one(moved.rowcount)
        if retired.restored is not None:
            back = connection.execute(
                update(db.versions)
                .where(
                    *_is(retired.playbook, retired.restored), db.versions.c.state == "superseded"
                )
                .values(state="in_use")
            )
            moved_one(back.rowcount)

    def _approve_founding(
        self, connection: Connection, approval: FoundingApproval, who: str
    ) -> None:
        first = connection.execute(
            select(db.ledger_entries.c.data, db.ledger_entries.c.hash).where(
                db.ledger_entries.c.line == 1
            )
        ).one()
        founding = Founding.model_validate(first.data)
        named = (approval.founding.hash, approval.versions, approval.desks_sha256)
        if named != (first.hash, founding.versions, founding.desks_sha256):
            raise CannotApply("it names other than the founding line names")
        if not approval.versions:
            raise CannotApply("it names no version")
        if who != OWNER:
            raise CannotApply("only the playbook owner approves the founding versions")
        if founding.approved_by is not None:
            raise CannotApply("the founding line was approved already")
        earlier = connection.execute(
            select(db.ledger_entries.c.line).where(db.ledger_entries.c.event == FOUNDING_APPROVED)
        ).first()
        if earlier is not None:
            raise CannotApply("the founding versions were approved already")
        for each in approval.versions:
            moved = connection.execute(
                update(db.versions)
                .where(
                    *_is(each.id, each.version),
                    db.versions.c.sha256 == each.sha256,
                    db.versions.c.state == "in_use",
                    db.versions.c.approved_by.is_(None),
                )
                .values(approved_by=who)
            )
            moved_one(moved.rowcount)

    def _write_set(self, connection: Connection, line: int, set_id: str) -> None:
        in_use, steps, _ = self._in_use(connection)
        _refuse_overlap(steps)
        version = connection.execute(
            select(func.coalesce(func.max(db.playbook_sets.c.version), 0) + 1)
        ).scalar_one()
        founded = connection.execute(
            select(db.ledger_entries.c.data).where(db.ledger_entries.c.line == 1)
        ).scalar_one()
        identity = {
            "id": set_id,
            "version": version,
            "in_use": [each.model_dump() for each in in_use],
            # A note shows a desk's name, so the names are part of what a run was triaged under.
            "desks": founded["desks_sha256"],
        }
        content = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        connection.execute(
            insert(db.playbook_sets).values(
                version=version, id=set_id, hash=content_hash(content), line=line
            )
        )

    # What is read

    def _set_row(self, connection: Connection):
        return connection.execute(
            select(db.playbook_sets).order_by(db.playbook_sets.c.version.desc()).limit(1)
        ).one()

    def _in_use(
        self, connection: Connection
    ) -> tuple[list[VersionInUse], list[StepInUse], frozenset[str]]:
        found = connection.execute(
            select(db.versions)
            .where(db.versions.c.state == "in_use")
            .order_by(db.versions.c.playbook)
        ).all()
        return (
            [VersionInUse(id=r.playbook, version=r.version, sha256=r.sha256) for r in found],
            [step for row in found for step in steps_of(row)],
            frozenset(r.playbook for r in found if r.approved_by is not None),
        )

    def _highest(self, connection: Connection, playbook: str) -> int:
        return connection.execute(
            select(func.coalesce(func.max(db.versions.c.version), 0)).where(
                db.versions.c.playbook == playbook
            )
        ).scalar_one()

    def current(self, connection: Connection) -> PlaybookSet:
        latest = self._set_row(connection)
        in_use, steps, approved = self._in_use(connection)
        return PlaybookSet(
            id=latest.id,
            version=latest.version,
            hash=latest.hash,
            versions=tuple(in_use),
            steps=tuple(steps),
            approved=approved,
        )

    # The moves

    def put_in_use(
        self,
        connection: Connection,
        changed: Mapping[str, Sequence[Step]],
        *,
        who: str,
        at: str,
        rests_on: Mapping[str, Sequence[RestsOn]],
        approval: ApprovedProposal | None,
    ) -> Line:
        """Write each changed playbook's next version, then the line that puts them in use.

        That one line is also the approval: of the proposal, with its ruling, and resolving
        the gap reports it cites. Everything before the line can be undone by the next start.
        """
        now, steps, _ = self._in_use(connection)
        versions = {each.id: each.version for each in now}
        playbooks = [
            Playbook(
                id=name,
                version=self._highest(connection, name) + 1,
                replaces=versions.get(name),
                steps=list(new),
            )
            for name, new in sorted(changed.items())
        ]
        after = [each for each in steps if each.playbook not in changed]
        for playbook in playbooks:
            after.extend(self._as_used(playbook))
        # Everything that can refuse does so before a file is written.
        _refuse_overlap(after)
        # A line that may be there may name a file: none is removed until that is known.
        self.kept.log.check_writable()
        known = connection.execute(select(db.versions.c.playbook, db.versions.c.version))
        self._remove_unnamed({_file(row.playbook, row.version) for row in known})
        written = []
        for playbook in playbooks:
            content = yaml.safe_dump(
                playbook.model_dump(mode="json"), sort_keys=False, allow_unicode=True, width=200
            ).encode()
            self._write_new(playbook, content)
            written.append(
                VersionWritten(
                    id=playbook.id,
                    version=playbook.version,
                    sha256=content_hash(content),
                    rests_on=list(rests_on[playbook.id]),
                )
            )
        data = PutInUse(versions=written, approval=approval).model_dump(mode="json")
        return self.kept.record(connection, PUT_IN_USE, who, at, data)

    def _write_new(self, playbook: Playbook, content: bytes) -> None:
        folder = self._folder / playbook.id
        folder.mkdir(exist_ok=True)
        path = self._folder / _file(playbook.id, playbook.version)
        # Exclusive: a file that is there is never written over.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            write_all(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        flush_folder(folder)
        flush_folder(self._folder)

    def founding(self, connection: Connection) -> tuple[Founding, str]:
        """The first line's founding, and that line's hash."""
        first = connection.execute(
            select(db.ledger_entries.c.data, db.ledger_entries.c.hash).where(
                db.ledger_entries.c.line == 1
            )
        ).one()
        return Founding.model_validate(first.data), first.hash

    def file_hash(self, shown: str) -> str | None:
        """The hash of a file of the playbook folder as it is now, or None if it cannot be read."""
        try:
            return content_hash((self._folder / shown).read_bytes())
        except OSError:
            return None

    def file_text(self, shown: str) -> str:
        return _read(self._folder, shown).decode()

    def approve_founding(
        self, connection: Connection, approval: FoundingApproval, *, who: str, at: str
    ) -> Line:
        """Record the approval of the founding versions. Nothing that can refuse may follow."""
        self.kept.log.check_writable()
        data = approval.model_dump(mode="json")
        return self.kept.record(connection, FOUNDING_APPROVED, who, at, data)

    def retire(
        self,
        connection: Connection,
        playbook: str,
        version: int,
        reason: RetireReason,
        *,
        who: str,
        at: str,
    ) -> Line:
        """Retire the version in use. The latest superseded one is in use again."""
        restored = connection.execute(
            select(func.max(db.versions.c.version)).where(
                db.versions.c.playbook == playbook, db.versions.c.state == "superseded"
            )
        ).scalar_one()
        data = Retirement(playbook=playbook, version=version, reason=reason, restored=restored)
        return self.kept.record(connection, RETIRED, who, at, data.model_dump(mode="json"))
