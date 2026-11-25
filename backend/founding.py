"""The playbook owner's approval of the founding versions, and what the owner reads first.

The first ledger line founded the playbook set with no approval, and is never written again.
The approval is a line of its own that names exactly what the first line names: each
version's ID, number and hash, the founding memo it rests on by hash, and the desks' hash.
It is refused, changing nothing, where any of those is not what the first line and the
files hold, where a founding version is no longer in use, and once it is recorded.
"""

from pathlib import Path

from sqlalchemy import Connection, select

from backend import clock, models, sources
from backend import database as db
from backend.journal import Journal
from backend.playbook_format import content_hash
from backend.refusals import Refusal
from backend.store import (
    DESKS,
    FOUNDING_APPROVED,
    FoundingApproval,
    FoundingLine,
    Store,
    VersionInUse,
)
from backend.vocabulary import OWNER

IN_THE_LINE = (
    "The founding versions were approved in the founding line itself, so there is nothing "
    "to approve."
)
APPROVED = "The founding versions are approved already, so nothing changes."
NOT_THE_LINE = "The hash named is not the founding line's, so nothing changes."


def memos_in(folder: Path) -> dict[str, tuple[str, str]]:
    """Each memo under `memos/` by its ID: the hash of the file's bytes, and its words."""
    found = {}
    for file in sorted((folder / "memos").glob("*.md")) if (folder / "memos").is_dir() else []:
        content = file.read_bytes()
        try:
            parsed = sources.parse_memo(content.decode())
        except UnicodeDecodeError:
            continue
        if parsed is not None:
            memo, words = parsed
            found[memo.id] = (content_hash(content), words)
    return found


def _approval_line(connection: Connection) -> tuple[int, str, str] | None:
    """The line that approved the founding versions: its number, who and when, if there is one."""
    found = connection.execute(
        select(db.ledger_entries.c.line, db.ledger_entries.c.who, db.ledger_entries.c.at).where(
            db.ledger_entries.c.event == FOUNDING_APPROVED
        )
    ).first()
    return (found.line, found.who, found.at) if found else None


class FoundingVersions:
    def __init__(self, journal: Journal, store: Store, firm_sources: Path):
        self._journal = journal
        self._store = store
        self._sources = firm_sources

    def read(self) -> models.FoundingShown:
        memos = memos_in(self._sources)
        with self._journal.transaction() as connection:
            founding, line_hash = self._store.founding(connection)
            approved = _approval_line(connection)
            states = {
                (row.playbook, row.version): row.state
                for row in connection.execute(select(db.versions))
            }
        versions = []
        for each in founding.versions:
            shown = each.file()
            versions.append(
                models.FoundingVersionShown(
                    id=each.id,
                    version=each.version,
                    sha256=each.sha256,
                    state=states[each.id, each.version],
                    as_named=self._store.file_hash(shown) == each.sha256,
                    text=self._store.file_text(shown),
                    rests_on=[
                        models.FoundingMemoShown(
                            id=memo.id,
                            sha256=memo.sha256,
                            as_named=memos.get(memo.id, ("", ""))[0] == memo.sha256,
                            words=memos[memo.id][1] if memo.id in memos else None,
                        )
                        for memo in each.rests_on
                    ],
                )
            )
        return models.FoundingShown(
            hash=line_hash,
            approved_by=founding.approved_by or (approved[1] if approved else None),
            approved_at=approved[2] if approved else None,
            versions=versions,
            desks=models.DesksShown(
                sha256=founding.desks_sha256,
                as_named=self._store.file_hash(DESKS) == founding.desks_sha256,
                text=self._store.file_text(DESKS),
            ),
        )

    def approve(self, sent: models.ApproveFounding) -> models.FoundingApproved:
        memos = memos_in(self._sources)
        with self._journal.transaction() as connection:
            founding, line_hash = self._store.founding(connection)
            if founding.approved_by is not None:
                raise Refusal(409, IN_THE_LINE)
            if before := _approval_line(connection):
                # The same move sent again returns what it did the first time.
                if sent.sha256 != line_hash:
                    raise Refusal(409, APPROVED)
                return _approved(connection, founding, before[0])
            if sent.sha256 != line_hash:
                raise Refusal(409, NOT_THE_LINE)
            for each in founding.versions:
                state = connection.execute(
                    select(db.versions.c.state).where(
                        db.versions.c.playbook == each.id, db.versions.c.version == each.version
                    )
                ).scalar_one()
                named = f"{each.id} version {each.version}"
                if state != "in_use":
                    raise Refusal(
                        409, f"The founding version {named} is {state}, so it is not approved."
                    )
                if self._store.file_hash(each.file()) != each.sha256:
                    raise Refusal(409, f"The file of {named} is not as the founding line names it.")
                for memo in each.rests_on:
                    if memos.get(memo.id, ("", ""))[0] != memo.sha256:
                        raise Refusal(
                            409,
                            f"The founding memo {memo.id} is not here as the founding line "
                            "names it.",
                        )
            if self._store.file_hash(DESKS) != founding.desks_sha256:
                raise Refusal(409, "The desks are not as the founding line names them.")
            approval = FoundingApproval(
                founding=FoundingLine(line=1, hash=line_hash),
                versions=founding.versions,
                desks_sha256=founding.desks_sha256,
            )
            at = clock.stamp(clock.now())
            line = self._store.approve_founding(connection, approval, who=OWNER, at=at)
            return _approved(connection, founding, line.line)


def _approved(connection: Connection, founding, line: int) -> models.FoundingApproved:
    then = connection.execute(select(db.playbook_sets).where(db.playbook_sets.c.line == line)).one()
    return models.FoundingApproved(
        line=line,
        versions=[
            VersionInUse(id=each.id, version=each.version, sha256=each.sha256)
            for each in founding.versions
        ],
        playbook_set=models.ReceiptSet(id=then.id, version=then.version, hash=then.hash),
    )
