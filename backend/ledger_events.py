"""What each ledger line does that leaves the playbook set as it is.

Firm sources, corrections and their reviews, gap reports, curator runs with the proposals
they leave, the gate's runs and checks, and refused and stale proposals. An approval changes
the set, so its line is the store's, which calls `approved` here for the proposal and its
gap reports. Like the triage log's `apply`, this is the only code that writes those tables,
for a move as it is made and for a replay alike, and it uses only the line's data and time.

A tool call that files a gap report, or is refused one by a rule, is that move's line, so
the call's step is written from here. One move stays one line.
"""

from collections.abc import Callable

from sqlalchemy import Connection, delete, exists, insert, update
from sqlalchemy.dialects.sqlite import insert as insert_if_absent

from backend import database as db
from backend.chainlog import Line
from backend.journal import CannotApply, moved_one
from backend.triage_log import add_steps

SOURCE_TAKEN_IN = "source_taken_in"
SOURCE_REFUSED = "source_refused"
CORRECTION_RECORDED = "correction_recorded"
REVIEW_ASKED_AGAIN = "review_asked_again"
REVIEW_NOT_STARTED = "review_not_started"
REVIEW_CALLS = "review_calls"
REVIEW_STOPPED = "review_stopped"
CORRECTION_REVIEWED = "correction_reviewed"
GAP_REPORT_FILED = "gap_report_filed"
GAP_REPORT_REFUSED = "gap_report_refused"
GAP_REPORT_DISMISSED = "gap_report_dismissed"
BUNDLE_REFUSED = "bundle_refused"
CURATION_STARTED = "curator_run_started"
CURATION_UNDONE = "curator_run_undone"
CURATION_STARTED_AGAIN = "curator_run_started_again"
CURATION_RESTART_UNDONE = "curator_restart_undone"
CURATION_CALLS = "curator_calls"
PROPOSAL_RECORDED = "proposal_recorded"
NO_CHANGE_RECORDED = "curator_found_no_change"
CURATION_FAILED = "curator_run_failed"
GATE_RUN = "gate_run"
GATE_REFUSED = "gate_refused"
HELD_OUT_TRIAL = "held_out_trial"
PROPOSAL_CHECKED = "proposal_checked"
PROPOSAL_REFUSED = "proposal_refused"
PROPOSAL_STALE = "proposal_went_stale"
# The recorded cycle: where it has got, and what each step made. It changes no table.
RECORDED_CYCLE = "recorded_cycle_step"
# What the playbook owner can still refuse.
REFUSABLE = ("drafted", "gate_failed", "gate_passed")
# The one failure that is the backend's to record. No curator may claim it.
HELD_OUT = "held_out"


def _recorded(data: dict) -> tuple:
    return (
        db.corrections.c.id == data["correction_id"],
        db.corrections.c.state == "recorded",
        db.corrections.c.attempt == data["attempt"],
    )


def _update_review(connection: Connection, where: tuple, **values) -> None:
    moved_one(connection.execute(update(db.corrections).where(*where).values(**values)).rowcount)


def _add_review_steps(connection: Connection, at: str, data: dict) -> None:
    common = {name: data[name] for name in ("correction_id", "attempt", "turn", "move")}
    connection.execute(
        insert(db.review_steps), [common | {"at": at} | each for each in data["calls"]]
    )


def _source_taken_in(connection: Connection, line: Line) -> None:
    connection.execute(
        insert(db.sources).values(line=line.line, correction=None, **line.data["source"])
    )


def _source_refused(_: Connection, __: Line) -> None:
    pass  # the line is the whole record: what was refused is never a firm source


def _correction_recorded(connection: Connection, line: Line) -> None:
    # Two runs' corrections with one content are one firm source, and two records of it.
    connection.execute(
        insert_if_absent(db.sources)
        .values(line=line.line, **line.data["source"])
        .on_conflict_do_nothing(index_elements=["id"])
    )
    connection.execute(
        insert(db.corrections).values(
            id=line.data["correction_id"],
            source_id=line.data["source"]["id"],
            run_id=line.data["run_id"],
            note_id=line.data["note_id"],
            state="recorded",
            attempt=1,
            recorded_at=line.at,
            heard_at=line.at,
        )
    )


def _review_asked_again(connection: Connection, line: Line) -> None:
    data = line.data
    where = (
        *_recorded(data | {"attempt": data["before"]}),
        db.corrections.c.heard_at == data["last_heard"],
    )
    _update_review(connection, where, attempt=data["attempt"], heard_at=line.at, stopped=None)


def _review_not_started(connection: Connection, line: Line) -> None:
    # Any move the agent made would have been heard. The attempt's number is not given back.
    where = (*_recorded(line.data), db.corrections.c.heard_at == line.data["asked_at"])
    _update_review(connection, where, heard_at=None)


def _review_calls(connection: Connection, line: Line) -> None:
    if line.data["heard"]:
        _update_review(connection, _recorded(line.data), heard_at=line.at)
    _add_review_steps(connection, line.at, line.data)


def _review_stopped(connection: Connection, line: Line) -> None:
    _update_review(connection, _recorded(line.data), heard_at=None, stopped=line.data["reason"])


def _reviewed(
    connection: Connection, at: str, data: dict, outcome: str, report_id: str | None
) -> None:
    _update_review(
        connection,
        _recorded(data),
        state="reviewed",
        outcome=outcome,
        gap_report_id=report_id,
        heard_at=at,
        reviewed_at=at,
        stopped=None,
    )


def _correction_reviewed(connection: Connection, line: Line) -> None:
    data = line.data
    _reviewed(connection, line.at, data, data["outcome"], data["gap_report_id"])
    if data["calls"]:
        _add_review_steps(connection, line.at, data)


def _gap_report_filed(connection: Connection, line: Line) -> None:
    report, made = line.data["report"], line.data["made"]
    connection.execute(
        insert(db.gap_reports).values(state="open", filed_at=line.at, line=line.line, **report)
    )
    if report["correction_id"] is None:
        add_steps(connection, line.at, made)
    else:
        _reviewed(connection, line.at, made, "filed", report["id"])
        _add_review_steps(connection, line.at, made)


def _gap_report_refused(connection: Connection, line: Line) -> None:
    made = line.data["made"]
    if "correction_id" in made:
        _update_review(connection, _recorded(made), heard_at=line.at)
        _add_review_steps(connection, line.at, made)
    else:
        add_steps(connection, line.at, made)


def _gap_report_dismissed(connection: Connection, line: Line) -> None:
    moved = connection.execute(
        update(db.gap_reports)
        .where(db.gap_reports.c.id == line.data["gap_report_id"], db.gap_reports.c.state == "open")
        .values(state="dismissed", dismissed=line.data["reason"])
    )
    moved_one(moved.rowcount)


def _bundle_refused(connection: Connection, line: Line) -> None:
    # With no run, the line is the whole record: none was made of that bundle. A stalled
    # run of it ends, so that the attempt that was asked can go no further on it.
    if line.data["run_id"] is not None:
        values = {"state": "failed", "failure": HELD_OUT, "finished_at": line.at}
        _update_curation(connection, _curating(line.data), heard_at=line.at, **values)


def _curating(data: dict) -> tuple:
    return (
        db.curator_runs.c.id == data["run_id"],
        db.curator_runs.c.state == "working",
        db.curator_runs.c.attempt == data["attempt"],
    )


def _update_curation(connection: Connection, where: tuple, **values) -> None:
    moved = connection.execute(update(db.curator_runs).where(*where).values(**values))
    moved_one(moved.rowcount)


def _curation_started(connection: Connection, line: Line) -> None:
    data = line.data
    connection.execute(
        insert(db.curator_runs).values(
            id=data["run_id"],
            attempt=data["attempt"],
            state="working",
            model=data["model"],
            items=data["items"],
            bundle_sha256=data["bundle_sha256"],
            gap_report_ids=data["gap_report_ids"],
            started_at=line.at,
            heard_at=line.at,
            line=line.line,
        )
    )


def _curation_undone(connection: Connection, line: Line) -> None:
    worked = exists().where(db.curator_steps.c.run_id == line.data["run_id"])
    where = (*_curating(line.data), ~worked)
    moved_one(connection.execute(delete(db.curator_runs).where(*where)).rowcount)


def _curation_started_again(connection: Connection, line: Line) -> None:
    data = line.data
    where = (
        *_curating(data | {"attempt": data["before"]}),
        db.curator_runs.c.heard_at == data["last_heard"],
    )
    values = {"attempt": data["attempt"], "model": data["model"], "heard_at": line.at}
    _update_curation(connection, where, **values)


def _curation_restart_undone(connection: Connection, line: Line) -> None:
    # As for a triage run: any move would have been heard, and the number is not given back.
    data = line.data
    where = (*_curating(data), db.curator_runs.c.heard_at == data["asked_at"])
    _update_curation(connection, where, heard_at=data["last_heard"], model=data["last_model"])


def _add_curator_steps(connection: Connection, at: str, data: dict) -> None:
    common = {name: data[name] for name in ("run_id", "attempt", "turn", "move")}
    connection.execute(
        insert(db.curator_steps), [common | {"at": at} | each for each in data["calls"]]
    )


def _curation_calls(connection: Connection, line: Line) -> None:
    if line.data["heard"]:
        _update_curation(connection, _curating(line.data), heard_at=line.at)
    _add_curator_steps(connection, line.at, line.data)


def _proposal_recorded(connection: Connection, line: Line) -> None:
    data, proposal = line.data, line.data["proposal"]
    values = {"state": "completed", "heard_at": line.at, "finished_at": line.at}
    _update_curation(connection, _curating(data), **values)
    connection.execute(
        insert(db.proposals).values(
            run_id=data["run_id"], state="drafted", recorded_at=line.at, line=line.line, **proposal
        )
    )
    for report_id in data["cited"]:
        moved = connection.execute(
            update(db.gap_reports)
            .where(db.gap_reports.c.id == report_id, db.gap_reports.c.state == "open")
            .values(state="cited", proposal_id=proposal["id"])
        )
        moved_one(moved.rowcount)
    _add_curator_steps(connection, line.at, data)


def _no_change_recorded(connection: Connection, line: Line) -> None:
    values = {"state": "completed_no_change", "heard_at": line.at, "finished_at": line.at}
    _update_curation(connection, _curating(line.data), **values)
    _add_curator_steps(connection, line.at, line.data)


def _curation_failed(connection: Connection, line: Line) -> None:
    values = {"state": "failed", "failure": line.data["reason"], "finished_at": line.at}
    _update_curation(connection, _curating(line.data), heard_at=line.at, **values)


def _add_gate_run(connection: Connection, line: Line, run: dict) -> None:
    used = run["playbook_set"]
    connection.execute(
        insert(db.gate_runs).values(
            id=run["id"],
            proposal_id=run["proposal_id"],
            kind=run["kind"],
            who=line.who,
            at=line.at,
            set_id=used["id"],
            set_version=used["version"],
            set_hash=used["hash"],
            bundle_sha256=run["bundle_sha256"],
            outcome=run["outcome"],
            checks=run["checks"],
            disagreements=run["disagreements"],
            disputed=run.get("disputed"),
            held_out=run.get("held_out"),
            line=line.line,
        )
    )


def _gate_run(connection: Connection, line: Line) -> None:
    run = line.data["gate_run"]
    moved_to = {"passed": "gate_passed", "failed": "gate_failed"}.get(run["outcome"])
    if moved_to is None:
        raise CannotApply("a gate run ended neither passed nor failed")
    _add_gate_run(connection, line, run)
    moved = connection.execute(
        update(db.proposals)
        .where(db.proposals.c.id == run["proposal_id"], db.proposals.c.state == "drafted")
        .values(state=moved_to)
    )
    moved_one(moved.rowcount)
    _reopen(connection, line.data["reopened"], run["proposal_id"])


def _reopen(connection: Connection, reports: list[str], proposal_id: str) -> None:
    _cited_moved(connection, reports, proposal_id, state="open", proposal_id_after=None)


def _cited_moved(
    connection: Connection,
    reports: list[str],
    proposal_id: str,
    *,
    state: str,
    proposal_id_after: str | None,
) -> None:
    """Move reports that this proposal cites. A report another proposal cites is not moved."""
    if not reports:
        return
    moved = connection.execute(
        update(db.gap_reports)
        .where(
            db.gap_reports.c.id.in_(reports),
            db.gap_reports.c.state == "cited",
            db.gap_reports.c.proposal_id == proposal_id,
        )
        .values(state=state, proposal_id=proposal_id_after)
    )
    if moved.rowcount != len(reports):
        raise CannotApply("a gap report to move is not cited by that proposal")


def _decide(
    connection: Connection, line: int, proposal_id: str, before: tuple[str, ...], state: str
) -> None:
    moved = connection.execute(
        update(db.proposals)
        .where(db.proposals.c.id == proposal_id, db.proposals.c.state.in_(before))
        .values(state=state, decided_line=line)
    )
    moved_one(moved.rowcount)


def approved(connection: Connection, line: int, proposal_id: str, resolved: list[str]) -> None:
    """An approval's part that is not the set's. The line's versions are in use already."""
    _decide(connection, line, proposal_id, ("gate_passed",), "approved")
    _cited_moved(connection, resolved, proposal_id, state="resolved", proposal_id_after=proposal_id)


def _proposal_refused(connection: Connection, line: Line) -> None:
    proposal_id = line.data["proposal_id"]
    _decide(connection, line.line, proposal_id, REFUSABLE, "refused")
    _reopen(connection, line.data["reopened"], proposal_id)


def _proposal_stale(connection: Connection, line: Line) -> None:
    proposal_id = line.data["proposal_id"]
    _decide(connection, line.line, proposal_id, ("gate_passed",), "stale")
    _reopen(connection, line.data["reopened"], proposal_id)


def _gate_refused(connection: Connection, line: Line) -> None:
    data = line.data
    connection.execute(
        insert(db.gate_refusals).values(
            line=line.line,
            proposal_id=data["proposal_id"],
            who=line.who,
            at=line.at,
            reason=data["reason"],
            refusal=data["line"],
            gate_run_id=data.get("gate_run_id"),
        )
    )


def _held_out_trial(connection: Connection, line: Line) -> None:
    data = line.data
    connection.execute(
        insert(db.held_out_trials).values(
            line=line.line,
            gate_run_id=data["gate_run_id"],
            proposal_id=data["proposal_id"],
            case_set_sha256=data["case_set_sha256"],
            trial=data["trial"],
        )
    )


def _recorded_cycle(_: Connection, __: Line) -> None:
    pass  # read from the ledger's own entries: what each step made is in the line


def _proposal_checked(connection: Connection, line: Line) -> None:
    proposal = line.data["proposal"]
    connection.execute(
        insert(db.proposals).values(
            id=proposal["id"],
            run_id=None,
            state="checked",
            content=proposal["content"],
            sha256=proposal["sha256"],
            recorded_at=line.at,
            line=line.line,
        )
    )
    _add_gate_run(connection, line, line.data["gate_run"])


MOVES: dict[str, Callable[[Connection, Line], None]] = {
    SOURCE_TAKEN_IN: _source_taken_in,
    SOURCE_REFUSED: _source_refused,
    CORRECTION_RECORDED: _correction_recorded,
    REVIEW_ASKED_AGAIN: _review_asked_again,
    REVIEW_NOT_STARTED: _review_not_started,
    REVIEW_CALLS: _review_calls,
    REVIEW_STOPPED: _review_stopped,
    CORRECTION_REVIEWED: _correction_reviewed,
    GAP_REPORT_FILED: _gap_report_filed,
    GAP_REPORT_REFUSED: _gap_report_refused,
    GAP_REPORT_DISMISSED: _gap_report_dismissed,
    BUNDLE_REFUSED: _bundle_refused,
    CURATION_STARTED: _curation_started,
    CURATION_UNDONE: _curation_undone,
    CURATION_STARTED_AGAIN: _curation_started_again,
    CURATION_RESTART_UNDONE: _curation_restart_undone,
    CURATION_CALLS: _curation_calls,
    PROPOSAL_RECORDED: _proposal_recorded,
    NO_CHANGE_RECORDED: _no_change_recorded,
    CURATION_FAILED: _curation_failed,
    GATE_RUN: _gate_run,
    GATE_REFUSED: _gate_refused,
    HELD_OUT_TRIAL: _held_out_trial,
    PROPOSAL_CHECKED: _proposal_checked,
    PROPOSAL_REFUSED: _proposal_refused,
    PROPOSAL_STALE: _proposal_stale,
    RECORDED_CYCLE: _recorded_cycle,
}
