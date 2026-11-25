"""The tables, and the triggers that keep records from being rewritten or removed.

The tables follow the ledger and the triage log, and can be made again from the two.

A triage run and what belongs to it can be removed in one case only: undoing a run the
triage agent never worked. The run goes first, in the same transaction; only then can
the records that name it. A curator run is removed in that one case too.
"""

import zlib
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Connection,
    Engine,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable

metadata = MetaData()

# How far into each log the database is: the last line applied, and that line's hash.
applied = Table(
    "applied",
    metadata,
    Column("log", String, primary_key=True),
    Column("lines", Integer, nullable=False),
    Column("hash", String, nullable=False),
)
ledger_entries = Table(
    "ledger_entries",
    metadata,
    Column("line", Integer, primary_key=True, autoincrement=False),
    Column("at", String, nullable=False),
    Column("who", String, nullable=False),
    Column("event", String, nullable=False),
    Column("data", JSON, nullable=False),
    Column("hash", String, nullable=False),
)
playbook_sets = Table(
    "playbook_sets",
    metadata,
    Column("version", Integer, primary_key=True, autoincrement=False),
    Column("id", String, nullable=False),
    Column("hash", String, nullable=False),
    # The ledger line that put this set in use.
    Column("line", Integer, nullable=False, unique=True),
)
versions = Table(
    "playbook_versions",
    metadata,
    Column("playbook", String, primary_key=True),
    Column("version", Integer, primary_key=True, autoincrement=False),
    Column("sha256", String, nullable=False),
    Column("state", String, nullable=False),
    # Its steps as read from its file, whose hash was checked, with each desk's name.
    Column("steps", JSON, nullable=False),
    Column("retired_line", Integer),
    # Who approved it, as the line that put it in use says. None: no approval is recorded, as
    # for a founding version. A version with no step has an approval of its own all the same.
    Column("approved_by", String),
)


def _run() -> Column:
    return Column(
        "run_id",
        String,
        ForeignKey("triage_runs.id", deferrable=True, initially="DEFERRED"),
        nullable=False,
        index=True,
    )


runs = Table(
    "triage_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("attempt", Integer, nullable=False),
    Column("state", String, nullable=False),
    Column("date", String, nullable=False),
    Column("cutoff", String, nullable=False),
    Column("set_id", String, nullable=False),
    Column("set_version", Integer, nullable=False),
    Column("set_hash", String, nullable=False),
    Column("started_at", String, nullable=False),
    Column("heard_at", String, nullable=False),
    Column("finished_at", String),
    Column("failure", String),
)
reads = Table(
    "triage_reads",
    metadata,
    Column("id", Integer, primary_key=True),
    _run(),
    Column("operation", String, nullable=False),
    Column("result_id", String, nullable=False),
    Column("trace_id", String, nullable=False),
    Column("feed_run_id", String),
)
exceptions = Table(
    "triage_exceptions",
    metadata,
    Column("id", String, primary_key=True),
    _run(),
    Column("position", Integer, nullable=False),
    Column("code", String, nullable=False),
    Column("subjects", JSON, nullable=False),
    Column("facts", JSON(none_as_null=True)),
    Column("feed_run", JSON(none_as_null=True)),
    # The steps in use with this warning code when the run started: what a choice is checked by.
    Column("steps", JSON, nullable=False),
)
notices = Table(
    "triage_notices",
    metadata,
    Column("id", Integer, primary_key=True),
    _run(),
    Column("code", String, nullable=False),
    Column("subject", String, nullable=False),
)
notes = Table(
    "triage_notes",
    metadata,
    Column(
        "exception_id",
        String,
        ForeignKey("triage_exceptions.id", deferrable=True, initially="DEFERRED"),
        primary_key=True,
    ),
    _run(),
    Column("state", String, nullable=False),
    Column("chosen", JSON(none_as_null=True)),
    Column("settled_at", String),
)
steps = Table(
    "triage_steps",
    metadata,
    # No foreign key and no number of its own: the ledger and the triage log both write
    # steps, and the two are replayed one after the other.
    Column("run_id", String, primary_key=True),
    Column("attempt", Integer, primary_key=True, autoincrement=False),
    Column("exception_id", String, primary_key=True),
    Column("turn", Integer, primary_key=True, autoincrement=False),
    Column("call", Integer, primary_key=True, autoincrement=False),
    # The hash of the move as sent, so that the same move sent again is known.
    Column("move", String, nullable=False),
    Column("tool", String),
    Column("option", String),
    Column("outcome", String, nullable=False),
    Column("refused_status", Integer),
    Column("line", String),
    Column("model", String, nullable=False),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("cost_usd", String),
    Column("at", String, nullable=False),
)
# The secret of the gap reports' subject digest, from the triage log.
digest_keys = Table(
    "digest_keys",
    metadata,
    Column("line", Integer, primary_key=True, autoincrement=False),
    Column("secret", String, nullable=False),
)
# From here on, what the ledger says of firm knowledge. Nothing here refers to a triage
# table: those follow the other log.
sources = Table(
    "firm_sources",
    metadata,
    Column("id", String, primary_key=True),
    Column("kind", String, nullable=False),
    Column("date", String, nullable=False),
    Column("desk", String),
    Column("scope", JSON, nullable=False),
    Column("correction", JSON(none_as_null=True)),
    Column("text", String, nullable=False),
    Column("sha256", String, nullable=False),
    Column("line", Integer, nullable=False),
)
corrections = Table(
    "corrections",
    metadata,
    Column("id", String, primary_key=True),
    Column("source_id", String, ForeignKey("firm_sources.id"), nullable=False),
    Column("run_id", String, nullable=False),
    Column("note_id", String, nullable=False),
    Column("state", String, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("recorded_at", String, nullable=False),
    # None: nobody is working its review.
    Column("heard_at", String),
    Column("stopped", String),
    Column("outcome", String),
    Column("gap_report_id", String),
    Column("reviewed_at", String),
)
review_steps = Table(
    "review_steps",
    metadata,
    Column("correction_id", String, ForeignKey("corrections.id"), primary_key=True),
    Column("attempt", Integer, primary_key=True, autoincrement=False),
    Column("turn", Integer, primary_key=True, autoincrement=False),
    Column("call", Integer, primary_key=True, autoincrement=False),
    Column("move", String, nullable=False),
    Column("tool", String),
    Column("option", String),
    Column("outcome", String, nullable=False),
    Column("refused_status", Integer),
    Column("line", String),
    Column("model", String, nullable=False),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("cost_usd", String),
    Column("at", String, nullable=False),
)
gap_reports = Table(
    "gap_reports",
    metadata,
    Column("id", String, primary_key=True),
    Column("reference", String, nullable=False),
    Column("state", String, nullable=False),
    Column("playbook", String),
    Column("version", Integer),
    Column("code", String, nullable=False),
    Column("feed", JSON(none_as_null=True)),
    Column("summary", String, nullable=False),
    Column("trace_id", String, nullable=False),
    Column("subject_digest", String, nullable=False),
    Column("run_id", String, nullable=False),
    Column("note_id", String, nullable=False),
    Column("correction_id", String),
    Column("dismissed", String),
    # The proposal that cites it. No foreign key: a report is there before any proposal.
    Column("proposal_id", String),
    Column("filed_at", String, nullable=False),
    Column("line", Integer, nullable=False),
    # At most one report for the same trace, warning code and subject.
    UniqueConstraint("trace_id", "code", "subject_digest"),
)
curator_runs = Table(
    "curator_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("attempt", Integer, nullable=False),
    Column("state", String, nullable=False),
    # The curator's configured model, as it was when this attempt was asked.
    Column("model", String, nullable=False),
    # The bundle whole, as the curator is given it and as the ledger holds it.
    Column("items", JSON, nullable=False),
    Column("bundle_sha256", String, nullable=False),
    Column("gap_report_ids", JSON, nullable=False),
    Column("started_at", String, nullable=False),
    Column("heard_at", String, nullable=False),
    Column("finished_at", String),
    Column("failure", String),
    Column("line", Integer, nullable=False),
)
curator_steps = Table(
    "curator_steps",
    metadata,
    Column("run_id", String, ForeignKey("curator_runs.id"), primary_key=True),
    Column("attempt", Integer, primary_key=True, autoincrement=False),
    Column("turn", Integer, primary_key=True, autoincrement=False),
    Column("call", Integer, primary_key=True, autoincrement=False),
    Column("move", String, nullable=False),
    Column("tool", String),
    Column("option", String),
    Column("outcome", String, nullable=False),
    Column("refused_status", Integer),
    # A checked refusal can be a 409 too, so the status does not say which were checked.
    Column("out_of_place", Boolean, nullable=False),
    Column("line", String),
    Column("model", String, nullable=False),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("cost_usd", String),
    Column("at", String, nullable=False),
)
proposals = Table(
    "proposals",
    metadata,
    Column("id", String, primary_key=True),
    # None: handed in to be checked. Such a proposal is no curator's and is never approved.
    Column("run_id", String, ForeignKey("curator_runs.id"), unique=True),
    Column("state", String, nullable=False),
    # None: a checked proposal whose text the checks keep out of the record.
    Column("content", JSON(none_as_null=True)),
    Column("sha256", String, nullable=False),
    Column("recorded_at", String, nullable=False),
    Column("line", Integer, nullable=False),
    # The ledger line that approved it, refused it or found it stale. None: none has.
    Column("decided_line", Integer),
)
gate_runs = Table(
    "gate_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("proposal_id", String, ForeignKey("proposals.id"), nullable=False, index=True),
    Column("kind", String, nullable=False),
    Column("who", String, nullable=False),
    Column("at", String, nullable=False),
    Column("set_id", String, nullable=False),
    Column("set_version", Integer, nullable=False),
    Column("set_hash", String, nullable=False),
    Column("bundle_sha256", String, nullable=False),
    Column("outcome", String, nullable=False),
    Column("checks", JSON, nullable=False),
    Column("disagreements", JSON, nullable=False),
    # What a ruling is judged by. None: the gate run's line holds none, and no approval
    # rests on such a run.
    Column("disputed", JSON(none_as_null=True)),
    # What the held-out check ran. None for a check of a proposal handed in.
    Column("held_out", JSON(none_as_null=True)),
    Column("line", Integer, nullable=False),
)
# A gate that could not run: no model for its trials, a model that wrote the cases, and so on.
gate_refusals = Table(
    "gate_refusals",
    metadata,
    Column("line", Integer, primary_key=True),
    Column("proposal_id", String, ForeignKey("proposals.id"), nullable=False, index=True),
    Column("who", String, nullable=False),
    Column("at", String, nullable=False),
    Column("reason", String, nullable=False),
    Column("refusal", String, nullable=False),
    # The gate run it stopped, when it had begun its trials. None: it ran none.
    Column("gate_run_id", String),
)
# Each held-out trial, recorded as it ends, before the gate run that ran it is.
held_out_trials = Table(
    "held_out_trials",
    metadata,
    Column("line", Integer, primary_key=True),
    Column("gate_run_id", String, nullable=False, index=True),
    Column("proposal_id", String, ForeignKey("proposals.id"), nullable=False, index=True),
    Column("case_set_sha256", String, nullable=False),
    Column("trial", JSON, nullable=False),
)

_PARTS = (reads, exceptions, notices, notes, steps)
_NEVER_UPDATED = (
    reads,
    exceptions,
    notices,
    steps,
    ledger_entries,
    playbook_sets,
    digest_keys,
    sources,
    review_steps,
    curator_steps,
    gate_runs,
    gate_refusals,
    held_out_trials,
)
_NEVER_DELETED = (
    ledger_entries,
    playbook_sets,
    versions,
    digest_keys,
    sources,
    corrections,
    review_steps,
    gap_reports,
    curator_steps,
    proposals,
    gate_runs,
    gate_refusals,
    held_out_trials,
)
_CORRECTION_IDENTITY = ("id", "source_id", "run_id", "note_id", "recorded_at")
_REPORT_IDENTITY = tuple(
    column.name
    for column in gap_reports.c
    if column.name not in ("state", "dismissed", "proposal_id")
)
_CURATION_IDENTITY = ("id", "items", "bundle_sha256", "gap_report_ids", "started_at", "line")
_PROPOSAL_IDENTITY = ("id", "run_id", "content", "sha256", "recorded_at", "line")
_VERSION_IDENTITY = ("playbook", "version", "sha256", "steps")
_RUN_IDENTITY = ("id", "date", "cutoff", "set_id", "set_version", "set_hash", "started_at")


def _trigger(name: str, rule: str, message: str) -> str:
    return f"CREATE TRIGGER IF NOT EXISTS {name} {rule} BEGIN SELECT RAISE(ABORT, '{message}'); END"


def _changed(columns: tuple[str, ...]) -> str:
    return " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in columns)


def _triggers() -> list[str]:
    made = [
        _trigger(
            "playbook_versions_update",
            "BEFORE UPDATE ON playbook_versions WHEN NOT ("
            "(OLD.state = 'in_use' AND NEW.state IN ('superseded', 'retired') "
            "AND NEW.approved_by IS OLD.approved_by) OR "
            "(OLD.state = 'superseded' AND NEW.state = 'in_use' "
            "AND NEW.approved_by IS OLD.approved_by) OR "
            # A founding version's approval, recorded once, while it is in use.
            "(OLD.state = 'in_use' AND NEW.state = 'in_use' AND OLD.approved_by IS NULL "
            "AND NEW.approved_by = 'playbook_owner')"
            f") OR {_changed(_VERSION_IDENTITY)}",
            "a playbook version is not rewritten",
        ),
        _trigger(
            "triage_runs_update",
            "BEFORE UPDATE ON triage_runs "
            f"WHEN OLD.state != 'working' OR {_changed(_RUN_IDENTITY)}",
            "a triage run is not rewritten",
        ),
        _trigger(
            "triage_runs_delete",
            "BEFORE DELETE ON triage_runs WHEN OLD.state != 'working' OR EXISTS "
            "(SELECT 1 FROM triage_steps WHERE run_id = OLD.id)",
            "a triage run that was worked is not removed",
        ),
        _trigger(
            "triage_notes_update",
            "BEFORE UPDATE ON triage_notes WHEN OLD.state != 'pending' "
            "OR NEW.run_id != OLD.run_id OR NEW.exception_id != OLD.exception_id",
            "a settled note is not rewritten",
        ),
    ]
    made += [
        _trigger(
            "corrections_update",
            "BEFORE UPDATE ON corrections "
            f"WHEN OLD.state != 'recorded' OR {_changed(_CORRECTION_IDENTITY)}",
            "a correction is not rewritten",
        ),
        _trigger(
            "gap_reports_update",
            "BEFORE UPDATE ON gap_reports WHEN NOT ("
            "(OLD.state = 'open' AND NEW.state = 'dismissed' AND NEW.proposal_id IS NULL) OR "
            "(OLD.state = 'open' AND NEW.state = 'cited' AND NEW.proposal_id IS NOT NULL "
            "AND NEW.dismissed IS NULL) OR "
            # Open again only once the proposal citing it has failed, gone stale or been refused.
            "(OLD.state = 'cited' AND NEW.state = 'open' AND NEW.proposal_id IS NULL "
            "AND NEW.dismissed IS NULL AND EXISTS (SELECT 1 FROM proposals "
            "WHERE id = OLD.proposal_id AND state IN ('gate_failed', 'stale', 'refused'))) OR "
            # Resolved only by the approval of the proposal citing it.
            "(OLD.state = 'cited' AND NEW.state = 'resolved' "
            "AND NEW.proposal_id IS OLD.proposal_id AND NEW.dismissed IS NULL "
            "AND EXISTS (SELECT 1 FROM proposals "
            "WHERE id = OLD.proposal_id AND state = 'approved'))"
            f") OR {_changed(_REPORT_IDENTITY)}",
            "a gap report is not rewritten",
        ),
        _trigger(
            "proposals_update",
            "BEFORE UPDATE ON proposals WHEN NOT ("
            # Failed, or passed, only by a gate run that did so, recorded first.
            "(OLD.state = 'drafted' AND NEW.state = 'gate_failed' AND NEW.decided_line IS NULL "
            "AND EXISTS (SELECT 1 FROM "
            "gate_runs WHERE proposal_id = OLD.id AND kind = 'gate' AND outcome = 'failed')) "
            "OR (OLD.state = 'drafted' AND NEW.state = 'gate_passed' AND NEW.decided_line IS NULL "
            "AND EXISTS (SELECT 1 FROM "
            "gate_runs WHERE proposal_id = OLD.id AND kind = 'gate' AND outcome = 'passed')) "
            # Decided once, by a later line. Approved only by one that put a set in use.
            "OR (OLD.state = 'gate_passed' AND NEW.state = 'approved' "
            "AND COALESCE(NEW.decided_line, 0) > OLD.line "
            "AND EXISTS (SELECT 1 FROM playbook_sets WHERE line = NEW.decided_line)) "
            "OR (OLD.state = 'gate_passed' AND NEW.state = 'stale' "
            "AND COALESCE(NEW.decided_line, 0) > OLD.line) "
            "OR (OLD.state IN ('drafted', 'gate_failed', 'gate_passed') AND NEW.state = 'refused' "
            "AND COALESCE(NEW.decided_line, 0) > OLD.line)"
            f") OR {_changed(_PROPOSAL_IDENTITY)}",
            "a proposal is not rewritten",
        ),
        _trigger(
            "curator_runs_update",
            "BEFORE UPDATE ON curator_runs "
            f"WHEN OLD.state != 'working' OR {_changed(_CURATION_IDENTITY)}",
            "a curator run is not rewritten",
        ),
        _trigger(
            "curator_runs_delete",
            "BEFORE DELETE ON curator_runs "
            "WHEN OLD.state != 'working' OR OLD.attempt != 1 OR EXISTS "
            "(SELECT 1 FROM curator_steps WHERE run_id = OLD.id)",
            "a curator run that was worked is not removed",
        ),
    ]
    for table in _NEVER_UPDATED:
        made.append(
            _trigger(
                f"{table.name}_update", f"BEFORE UPDATE ON {table.name}", "a record is only added"
            )
        )
    for table in _NEVER_DELETED:
        made.append(
            _trigger(
                f"{table.name}_delete", f"BEFORE DELETE ON {table.name}", "a record is only added"
            )
        )
    for table in _PARTS:
        made.append(
            _trigger(
                f"{table.name}_delete",
                f"BEFORE DELETE ON {table.name} "
                "WHEN EXISTS (SELECT 1 FROM triage_runs WHERE id = OLD.run_id)",
                "a record is only added",
            )
        )
    return made


def open_database(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"timeout": 30},
        # A failed statement's values would put client identifiers in the log.
        hide_parameters=True,
    )

    @event.listens_for(engine, "connect")
    def connected(connection, _record) -> None:
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys = ON")

    # A transaction takes the write lock when it begins, so a move's check and write are one.
    @event.listens_for(engine, "begin")
    def began(connection) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def create(connection: Connection) -> None:
    metadata.create_all(connection)
    for trigger in _triggers():
        connection.exec_driver_sql(trigger)


def _schema() -> int:
    dialect = sqlite.dialect()
    made = [str(CreateTable(each).compile(dialect=dialect)) for each in metadata.sorted_tables]
    return zlib.crc32("\n".join([*made, *_triggers()]).encode()) & 0x7FFF_FFFF


def outdated(connection: Connection) -> bool:
    """Whether the tables are from another schema. They follow the logs, so none is migrated."""
    return connection.exec_driver_sql("PRAGMA user_version").scalar_one() != _schema()


def remake(connection: Connection) -> None:
    """Empty tables of this schema, for the logs to fill."""
    metadata.drop_all(connection)
    create(connection)
    connection.exec_driver_sql(f"PRAGMA user_version = {_schema()}")
