"""Intake of firm sources.

A memo is the file as it was sent: its front matter's fields and its text, under the hash
of its bytes. The playbook owner takes it in, and so sets its scope. A memo that breaks a
rule is never a firm source: a ledger line keeps the rule and the hash, and none of it.
"""

import re
from datetime import date
from typing import Annotated, Literal

import yaml
from pydantic import Strict, StringConstraints
from sqlalchemy import Connection, select

from backend import clock, models, personal_data
from backend import database as db
from backend.journal import Journal
from backend.ledger_events import SOURCE_REFUSED, SOURCE_TAKEN_IN
from backend.playbook_format import Fixed, Scope, Slug, content_hash
from backend.refusals import Refusal
from backend.store import FOUNDED, Founding, Store
from backend.vocabulary import OWNER

_MEMO = re.compile(r"\A---\n(?P<front>.*?)^---\n(?P<body>.*)\Z", re.DOTALL | re.MULTILINE)
MALFORMED = (
    "The memo does not fit: it needs id, date, desk, kind and scope between two lines of "
    "three hyphens, each in its closed form, and then its text."
)


class _Memo(Fixed):
    # The prefix keeps a memo from taking a correction's or a gap report's reference.
    id: Annotated[str, StringConstraints(pattern=r"^memo(-[a-z]+)+$", max_length=64)]
    date: Annotated[date, Strict()]
    desk: Slug
    kind: Literal["memo"]
    scope: Scope | Literal["none"]


class _NoAliases(yaml.SafeLoader):
    """An alias could make a short text a huge structure, and a memo needs none."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("a memo has no aliases")
        return super().compose_node(parent, index)


def _parsed(content: str) -> tuple[_Memo, str] | None:
    found = _MEMO.fullmatch(content)
    if found is None or not found["body"].strip():
        return None
    try:
        front = yaml.load(found["front"], Loader=_NoAliases)
        return _Memo.model_validate(front), found["body"].strip()
    except Exception:
        # The parser raises errors of many kinds on a tag it cannot build. Each is a memo
        # out of form, and none is passed on: what was wrong would repeat what was sent.
        return None


def parse_memo(content: str) -> tuple[_Memo, str] | None:
    """A memo's fields and its words, as intake reads them, or None where it is out of form."""
    return _parsed(content)


def refuse_once(
    connection: Connection, store: Store, kind: str, rule: str, sha256: str, who: str
) -> None:
    """One line for a refused source, however often it is sent."""
    data = {"kind": kind, "rule": rule, "sha256": sha256}
    found = connection.execute(
        select(db.ledger_entries.c.data).where(db.ledger_entries.c.event == SOURCE_REFUSED)
    )
    if data not in found.scalars().all():
        at = clock.stamp(clock.now())
        store.kept.record(connection, SOURCE_REFUSED, who, at, data)


def _founded_on(connection: Connection) -> dict[str, str]:
    data = connection.execute(
        select(db.ledger_entries.c.data).where(db.ledger_entries.c.event == FOUNDED)
    ).scalar_one()
    founding = Founding.model_validate(data)
    return {each.id: each.sha256 for version in founding.versions for each in version.rests_on}


class Sources:
    def __init__(self, journal: Journal, store: Store):
        self._journal = journal
        self._store = store

    def intake(self, sent: models.Intake) -> models.FirmSource:
        content, sha256 = sent.content, content_hash(sent.content.encode())
        limit = personal_data.MEMO_LENGTH
        rule = personal_data.broken_rule(content, limit, multiline=True)
        parsed = None if rule else _parsed(content)
        if rule is None:
            if parsed is None:
                raise Refusal(422, MALFORMED)
            # Again as parsed: the front matter's escapes can spell what its text does not.
            fields = parsed[0].model_dump(mode="json")
            rule = personal_data.broken_rule(fields, limit)
        with self._journal.transaction() as connection:
            if rule is not None:
                refuse_once(connection, self._store, "memo", rule, sha256, OWNER)
            else:
                assert parsed is not None
                return self._take_in(connection, *parsed, sha256)
        # Raised after the transaction, so that the refusal's line stands.
        raise Refusal(422, personal_data.refusal("memo", rule))

    def _take_in(
        self, connection: Connection, memo: _Memo, text: str, sha256: str
    ) -> models.FirmSource:
        before = connection.execute(select(db.sources).where(db.sources.c.id == memo.id))
        if (row := before.mappings().first()) is not None:
            if row["sha256"] == sha256:
                return _shown(row)
            raise Refusal(
                409, "A memo with that ID is taken in with other content: a new one needs its own."
            )
        if _founded_on(connection).get(memo.id, sha256) != sha256:
            raise Refusal(
                409, "The founding versions rest on a memo with that ID and other content."
            )
        if memo.desk not in self._store.desks:
            raise Refusal(422, "The memo's desk is not on the list of desks.")
        taken = {"source": {**memo.model_dump(mode="json"), "text": text, "sha256": sha256}}
        at = clock.stamp(clock.now())
        self._store.kept.record(connection, SOURCE_TAKEN_IN, OWNER, at, taken)
        found = connection.execute(select(db.sources).where(db.sources.c.id == memo.id))
        return _shown(found.mappings().one())

    def listed(self) -> models.FirmSourceList:
        with self._journal.transaction() as connection:
            found = connection.execute(select(db.sources).order_by(db.sources.c.line))
            return models.FirmSourceList(sources=[_shown(row) for row in found.mappings()])


def _shown(row) -> models.FirmSource:
    return models.FirmSource.model_validate(
        {name: row[name] for name in models.FirmSource.model_fields}
    )
