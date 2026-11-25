"""A firm's playbook folder and its records, as the tests make and compare them."""

import shutil
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

import yaml

from backend import chainlog, triage_log
from backend.journal import Journal
from backend.playbook_format import Step, content_hash
from backend.store import FOUNDED, RestsOn, Store

ROOT = Path(__file__).resolve().parent.parent
PLAYBOOKS = ROOT / "playbooks"
OWNER = "playbook_owner"
AT = "2026-11-22T07:00:00.000000Z"
A_MEMO = RestsOn(id="memo-for-this-test", sha256="a" * 64)
ANY_WHEN = {"source_family": "any", "custodian": "any", "feed_status": "any"}


def copy(folder: Path) -> Path:
    shutil.copytree(PLAYBOOKS, folder)
    return folder


def found(folder: Path) -> Path:
    """Found the playbook set anew on a test's own files, as they are now."""
    versions = [
        {
            "id": file.parent.name,
            "version": 1,
            "sha256": content_hash(file.read_bytes()),
            "rests_on": [A_MEMO.model_dump()],
        }
        for file in sorted(folder.glob("*/1.yaml"))
    ]
    data = {
        "id": "wealthcontext-playbooks",
        "approved_by": OWNER,
        "desks_sha256": content_hash((folder / "desks.yaml").read_bytes()),
        "versions": versions,
    }
    line = chainlog.following(chainlog.START, FOUNDED, "founding", AT, data)
    (folder / "ledger.jsonl").write_bytes(line.written())
    return folder


def steps(playbook: str, **changes) -> list[Step]:
    """A playbook's founding steps, with these fields changed on each."""
    found = yaml.safe_load((PLAYBOOKS / playbook / "1.yaml").read_text())["steps"]
    return [Step.model_validate(each | changes) for each in found]


def put_in_use(journal: Journal, store: Store, changed: Mapping[str, Sequence[Step]]) -> None:
    with journal.transaction() as connection:
        rests_on = {playbook: [A_MEMO] for playbook in changed}
        store.put_in_use(
            connection, changed, who="playbook_owner", at=AT, rests_on=rests_on, approval=None
        )


@contextmanager
def opened(playbooks: Path, data: Path) -> Iterator[tuple[Journal, Store]]:
    """The store and the journal as the backend opens them, with no app around them."""
    store = Store(playbooks)
    journal = Journal.start(
        data / "playbooks.sqlite", store.kept, triage_log.kept(data / "triage-log.jsonl")
    )
    try:
        yield journal, store
    finally:
        journal.close()


def in_use(journal: Journal, store: Store) -> list[tuple[str, int]]:
    with journal.transaction() as connection:
        return [(each.id, each.version) for each in store.current(connection).versions]


def records(database: Path) -> dict[str, list[tuple]]:
    """Every row of every table, as it is stored."""
    with closing(sqlite3.connect(database)) as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {
            name: sorted(connection.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)
            for (name,) in tables.fetchall()
        }


def applied_lines(found: dict[str, list[tuple]]) -> int:
    """How many lines of both logs these records have applied."""
    return sum(lines for _, lines, _ in found["applied"])


def files(folder: Path) -> dict[str, bytes]:
    return {
        str(file.relative_to(folder)): file.read_bytes()
        for file in sorted(folder.rglob("*"))
        if file.is_file()
    }


def unapproved(folder: Path) -> Path:
    """The founding as it stood before the playbook owner approved it: the ledger's first line."""
    ledger = folder / "ledger.jsonl"
    first = ledger.read_bytes().splitlines(keepends=True)[0]
    ledger.write_bytes(first)
    return folder


# The committed ledger's lines: the founding, and the playbook owner's approval of it.
FOUNDING_EVENTS = ["playbooks_founded", "founding_approved"]
# The playbook set each puts in use: version 1 founded, version 2 approved.
APPROVED_SET = 2
