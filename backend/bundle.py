"""What the curator may read: assembled, listed and hashed here, and by nothing else.

Four kinds of item: the firm sources taken in, the open gap reports, the playbooks in use
and the triage outcome summary. A source or report that was refused is no row, so it is
in no bundle. Before a curator is asked, each item is compared by hash with every file of
the held-out cases and with every file that grades them.

The comparison is by hash, of a file's bytes and of its words. It sees a whole file that
was placed among the sources, laid out again or not. It cannot see a part of one.
"""

import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, select

from backend import database as db
from backend import gap_reports, references, triage
from backend.playbook_format import content_hash
from backend.store import Store

FIRM_SOURCE = "firm_source"
GAP_REPORT = "gap_report"
PLAYBOOKS_IN_USE = "playbooks_in_use"
OUTCOME_SUMMARY = "triage_outcome_summary"
# What a proposal may rest on.
CITABLE = (FIRM_SOURCE, GAP_REPORT)

_BACKEND = Path(__file__).resolve().parent
# The files that grade a case. The curator is told nothing of how it is graded.
GRADING = (_BACKEND / "cases.py", _BACKEND / "matching.py", _BACKEND / "gate")


class CannotCheck(Exception):
    """The held-out cases cannot be read, so no bundle can be shown free of them."""


@dataclass(frozen=True)
class Bundle:
    items: list[dict]
    sha256: str
    # Which reports each reference stands for. The curator is not given it.
    gap_report_ids: dict[str, list[str]]


def _item(kind: str, reference: str, content: dict, sha256: str | None = None) -> dict:
    return {
        "kind": kind,
        "reference": reference,
        # A source's is the one its intake recorded: for a memo, the hash of the file's bytes.
        "sha256": sha256 or references.content_sha256(content),
        "content": content,
    }


def _firm_sources(connection: Connection) -> Iterator[dict]:
    found = connection.execute(select(db.sources).order_by(db.sources.c.id)).mappings()
    for row in found:
        content = {name: row[name] for name in ("kind", "desk", "scope", "text")}
        if row["kind"] == "memo":
            # A memo's own date says which memo replaces which.
            content["date"] = row["date"]
        else:
            # A correction's date is that of the day triaged. The curator is shown none.
            content["correction"] = row["correction"]
        yield _item(FIRM_SOURCE, row["id"], content, row["sha256"])


def _playbooks(connection: Connection, store: Store) -> dict:
    playbooks = store.current(connection)
    shown = []
    for version in playbooks.versions:
        approved = playbooks.has_approval(version.id)
        steps = [
            {
                "step": each.step.id,
                "code": each.step.code,
                "when": each.step.when.model_dump(),
                # An action is given out from a version with a recorded approval, and no other.
                "tell": each.step.tell if approved else None,
                "by": each.step.by if approved else None,
                "hold_report": each.step.hold_report if approved else None,
                "line": each.step.line,
            }
            for each in playbooks.steps
            if each.playbook == version.id
        ]
        shown.append(
            {
                "playbook": version.id,
                "version": version.version,
                "approval_recorded": approved,
                "steps": steps,
            }
        )
    content = {"desks": sorted(store.desks), "playbooks": shown}
    return _item(PLAYBOOKS_IN_USE, "playbooks-in-use", content)


def assemble(connection: Connection, store: Store) -> Bundle:
    reports = gap_reports.curators_view(connection)
    items = [
        *_firm_sources(connection),
        *(_item(GAP_REPORT, each.reference, each.content) for each in reports),
        _playbooks(connection, store),
        _item(OUTCOME_SUMMARY, "triage-outcome-summary", triage.outcome_summary(connection)),
    ]
    return Bundle(
        items=items,
        sha256=references.content_sha256({"items": items}),
        gap_report_ids={each.reference: each.ids for each in reports},
    )


def citable(items: Sequence[Mapping[str, Any]]) -> set[str]:
    return {item["reference"] for item in items if item["kind"] in CITABLE}


# The comparison with the held-out cases


def _words_hash(text: str) -> str | None:
    words = " ".join(text.split())
    return content_hash(words.encode()) if words else None


def _files(path: Path) -> Iterator[Path]:
    if path.is_symlink():
        raise CannotCheck  # what a link leads to can change after it was read
    if path.is_file():
        yield path
    elif path.is_dir():
        try:
            for inner in sorted(os.listdir(path)):
                yield from _files(path / inner)
        except OSError:
            raise CannotCheck from None
    else:
        raise CannotCheck


def _sealed(cases: Path, grading: Sequence[Path]) -> set[str]:
    if not cases.is_dir():
        raise CannotCheck
    found = set()
    for root in [cases, *(each for each in grading if each.exists())]:
        for file in _files(root):
            try:
                content = file.read_bytes()
            except OSError:
                raise CannotCheck from None
            found.add(content_hash(content))
            if words := _words_hash(content.decode("utf-8", errors="replace")):
                found.add(words)
    return found


def _texts(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for inner in value.values():
            yield from _texts(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _texts(inner)


def held_out(items: Sequence[Mapping[str, Any]], cases: Path) -> Mapping[str, Any] | None:
    """The first item that is a held-out case or a file that grades them, or nothing."""
    sealed = _sealed(cases, GRADING)
    for item in items:
        hashes = {item["sha256"], *map(_words_hash, _texts(item["content"]))}
        if hashes & sealed:
            return item
    return None
