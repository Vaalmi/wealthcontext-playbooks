"""The gap report as a state machine.

A report's key is the trace ID of the quality report it came from, the warning code and a
keyed digest of the subject. The digest only tells two reports apart: it is never shown.
So a stored report holds no client identifier. The digest's secret is kept in the triage
log, the one file that is backed up beside the playbook folder, so a rebuild keeps it.
"""

import hashlib
import hmac
import json
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, RowMapping, select

from backend import clock, models, references, triage_log
from backend import database as db
from backend.journal import Journal, Kept
from backend.ledger_events import GAP_REPORT_DISMISSED
from backend.refusals import Refusal
from backend.store import Store
from backend.vocabulary import BACKEND, OWNER

# What a report's reference is the hash of, and all the curator is shown of one.
SHOWN_TO_THE_CURATOR = ("playbook", "version", "code", "feed", "summary")


@dataclass(frozen=True)
class Viewed:
    """Open reports with one content, as the curator sees them: once, with a count."""

    reference: str
    content: dict
    ids: list[str]


def curators_view(connection: Connection) -> list[Viewed]:
    found = connection.execute(
        select(db.gap_reports)
        .where(db.gap_reports.c.state == "open")
        .order_by(db.gap_reports.c.reference, db.gap_reports.c.line)
    )
    alike: dict[str, list[RowMapping]] = {}
    for row in found.mappings():
        alike.setdefault(row["reference"], []).append(row)
    return [
        Viewed(
            reference=reference,
            content={name: rows[0][name] for name in SHOWN_TO_THE_CURATOR} | {"count": len(rows)},
            ids=[row["id"] for row in rows],
        )
        for reference, rows in alike.items()
    ]


def cited_by(connection: Connection, proposal_id: str) -> list[str]:
    """The reports a proposal still has cited, which its end resolves or opens again."""
    found = connection.execute(
        select(db.gap_reports.c.id).where(
            db.gap_reports.c.proposal_id == proposal_id, db.gap_reports.c.state == "cited"
        )
    )
    return sorted(found.scalars())


def _key(connection: Connection) -> str | None:
    return connection.execute(select(db.digest_keys.c.secret)).scalar_one_or_none()


def _trace(connection: Connection, run_id: str) -> str:
    """The trace of the quality report a run read."""
    return connection.execute(
        select(db.reads.c.trace_id).where(
            db.reads.c.run_id == run_id, db.reads.c.operation == "get_data_quality_report"
        )
    ).scalar_one()


def _digest(secret: str, trace: str, note: Mapping[Any, Any]) -> str:
    # With the trace in it, one subject's digests in two reports cannot be linked.
    told = json.dumps([trace, note["code"], sorted(note["subjects"])]).encode()
    return hmac.new(bytes.fromhex(secret), told, hashlib.sha256).hexdigest()


class GapReports:
    def __init__(self, journal: Journal, log: Kept, store: Store):
        self._journal = journal
        self._log = log
        self._store = store

    def make_key(self) -> None:
        """Make the digest's secret once, as a move of its own before the first filing."""
        with self._journal.transaction() as connection:
            if _key(connection) is None:
                at = clock.stamp(clock.now())
                made = {"secret": secrets.token_hex(32)}
                self._log.record(connection, triage_log.DIGEST_KEY_MADE, BACKEND, at, made)

    def first(
        self, connection: Connection, run_id: str, note: Mapping[Any, Any]
    ) -> RowMapping | None:
        """The report there is for this trace, warning code and subject, if there is one."""
        if (secret := _key(connection)) is None:
            return None
        trace = _trace(connection, run_id)
        found = connection.execute(
            select(db.gap_reports).where(
                db.gap_reports.c.trace_id == trace,
                db.gap_reports.c.code == note["code"],
                db.gap_reports.c.subject_digest == _digest(secret, trace, note),
            )
        )
        return found.mappings().first()

    def drafted(
        self,
        connection: Connection,
        run_id: str,
        note: Mapping[Any, Any],
        summary: str,
        *,
        playbook: str | None,
        version: int | None,
        correction_id: str | None,
    ) -> dict:
        """A new report as its ledger line holds it. All but the summary is the backend's."""
        secret, trace = _key(connection), _trace(connection, run_id)
        assert secret is not None  # `make_key` ran before this move began
        shown = {
            "playbook": playbook,
            "version": version,
            "code": note["code"],
            "feed": note["facts"],
            "summary": summary,
        }
        return {
            "id": uuid.uuid4().hex,
            "reference": references.reference("gap-report", shown),
            **shown,
            "trace_id": trace,
            "subject_digest": _digest(secret, trace, note),
            "run_id": run_id,
            "note_id": note["id"],
            "correction_id": correction_id,
        }

    def dismiss(self, report_id: str, sent: models.Dismiss) -> models.GapReport:
        with self._journal.transaction() as connection:
            report = _report(connection, report_id)
            if (report["state"], report["dismissed"]) == ("dismissed", sent.reason):
                return _shown(report)
            if report["state"] != "open":
                raise Refusal(409, f"The gap report is {report['state']}.")
            moved = {"gap_report_id": report_id, "reason": sent.reason}
            at = clock.stamp(clock.now())
            self._store.kept.record(connection, GAP_REPORT_DISMISSED, OWNER, at, moved)
            return _shown(_report(connection, report_id))

    def listed(self) -> models.GapReportList:
        with self._journal.transaction() as connection:
            found = connection.execute(select(db.gap_reports).order_by(db.gap_reports.c.line))
            return models.GapReportList(gap_reports=[_shown(row) for row in found.mappings()])


def _report(connection: Connection, report_id: str) -> RowMapping:
    found = connection.execute(select(db.gap_reports).where(db.gap_reports.c.id == report_id))
    row = found.mappings().first()
    if row is None:
        raise Refusal(404, "There is no such gap report.")
    return row


def _shown(row: RowMapping) -> models.GapReport:
    # Everything but the digest, which is never shown.
    shown = {name: row[name] for name in models.GapReport.model_fields}
    return models.GapReport.model_validate(shown)
