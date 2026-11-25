"""The triage run and its notes as a state machine, and the triage receipts.

Each move is one conditional update on the run or note, its state and the run's attempt,
written with its line in the triage log. The update itself is `triage_log.apply`.
A note holds nothing a model wrote: a step named from a list, or no step.
A note gives an action only from a version whose approval is recorded.
A run that stalls is started again as the same run, under a new attempt: what was settled
stays settled, and what was refused still counts.
A note is settled only while the playbook set its run started under is the set in use, so
no action of a retired version is given out and every receipt names the set then in use.
For a note the backend settled because no playbook covers its warning, the agent is asked
one thing only: whether to file a gap report. That answer changes no note.
"""

import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Connection, RowMapping, func, or_, select

from backend import (
    agent_calls,
    clock,
    exceptions,
    ledger_events,
    matching,
    models,
    personal_data,
    triage_log,
)
from backend import database as db
from backend.agent_calls import OUT_OF_PLACE, REFUSALS_BEFORE_FALLBACK
from backend.asking import TriageAgent, commit_then_ask
from backend.gap_reports import GapReports
from backend.journal import CannotApply, Journal, Kept
from backend.refusals import Refusal
from backend.store import StepInUse, Store
from backend.vocabulary import AGENT, ANALYST, BACKEND, FeedFacts
from backend.wealthcontext import Reading, WealthContext, WealthContextFailed

NO_STEP = "No approved step applies."
UNAPPROVED = f"{NO_STEP} The step that fits has no recorded approval, so no action is given."
UNVERIFIED = "No verified choice; needs a person."
STATEMENTS = {"pending": None, "chosen": None, "no_step": NO_STEP, "unverified": UNVERIFIED}
STALLED_AFTER = timedelta(minutes=5)
NOT_STALLED = (
    "The run is working and has not stalled. "
    "It can be started again when nothing has been heard for five minutes."
)
UNDONE = "The run was undone."
AGAIN_UNDONE = (
    "Starting it again was undone: the run is working and still stalled, "
    "so it can be started again."
)
PLAYBOOKS_CHANGED = (
    "The run is working and the playbooks in use changed after it started, "
    "so its pending notes cannot be settled."
)
NOT_RECORDED_HERE = "That run is not one recorded here."
# Why a choice is refused. A gate's held-out trial refuses with the same lines.
NOT_IN_USE = "That is not a step in use with this exception's warning code."
DOES_NOT_FIT = "That step does not fit this exception: "
A_STEP_APPLIES = "A step applies to this exception, so 'no step' is refused."
NOT_ITS_TURN = "The note is pending, and that is not its next turn."

UNUSABLE: dict[models.UnusableReason, str] = {
    "no_tool_call": "Answer with exactly one tool call: choose_step or no_step_applies.",
    "several_tool_calls": "Make exactly one tool call; none of those was recorded as the choice.",
    "unknown_tool": "There is no such tool: use choose_step or no_step_applies.",
    "invalid_arguments": "The arguments do not fit the tool: choose one of the options it lists.",
    "no_answer": "No answer came back from the model; the most the call could have cost is "
    "counted as spent.",
}
FAILURES: dict[models.FailureReason, str] = {
    "model_failed": "The model could not be called.",
    "cost_cap": "The next model call could pass the run's cost cap, so the run stopped.",
    "recording_mismatch": "The recording no longer matches what the agent asks, so it refused.",
    "backend_refused": "The backend refused a move the triage agent needed.",
    "unexpected_error": "The triage agent stopped on an unexpected error.",
}
REVIEW_FAILURES: dict[models.FailureReason, str] = {
    **FAILURES,
    "cost_cap": "The next model call could pass the review's cost cap, so the review stopped.",
}


def _clock() -> datetime:
    return clock.now()


def now() -> str:
    return clock.stamp(_clock())


def stalled_since() -> str:
    return clock.stamp(_clock() - STALLED_AFTER)


def _heard(connection: Connection, run: RowMapping) -> str:
    """When a run was last heard from. A filing is a ledger line, which writes no run, so
    the run's own steps count too: all but those refused as out of place."""
    last = connection.execute(
        select(func.max(db.steps.c.at)).where(
            db.steps.c.run_id == run["id"],
            or_(db.steps.c.refused_status.is_(None), db.steps.c.refused_status != OUT_OF_PLACE),
        )
    ).scalar_one()
    return max(run["heard_at"], last or "")


def _stalled(connection: Connection, run: RowMapping) -> bool:
    return run["state"] == "working" and _heard(connection, run) <= stalled_since()


def _in_use(exception: RowMapping) -> list[StepInUse]:
    return [StepInUse.model_validate(each) for each in exception["steps"]]


def _facts(exception: RowMapping) -> FeedFacts | None:
    return FeedFacts.model_validate(exception["facts"]) if exception["facts"] else None


def _run(connection: Connection, run_id: str) -> RowMapping:
    row = connection.execute(select(db.runs).where(db.runs.c.id == run_id)).mappings().first()
    if row is None:
        raise Refusal(404, "There is no such triage run.")
    return row


def _current(run: RowMapping, attempt: int) -> None:
    if run["attempt"] != attempt:
        raise Refusal(409, _not_current(run))


def _not_current(run: RowMapping) -> str:
    return f"That attempt is not the current one; the run is {run['state']}."


def _run_at(connection: Connection, run_id: str, attempt: int) -> RowMapping:
    run = _run(connection, run_id)
    _current(run, attempt)
    return run


def _unfinished(connection: Connection, asked_for: models.StartTriage) -> RowMapping | None:
    found = connection.execute(
        select(db.runs)
        .where(
            db.runs.c.state == "working",
            db.runs.c.date == asked_for.date,
            db.runs.c.cutoff == asked_for.cutoff,
        )
        .order_by(db.runs.c.started_at.desc())
    )
    return found.mappings().first()


def _note(connection: Connection, run_id: str, note_id: str) -> RowMapping:
    found = connection.execute(
        select(db.exceptions, db.notes.c.state)
        .join(db.notes, db.notes.c.exception_id == db.exceptions.c.id)
        .where(db.exceptions.c.run_id == run_id, db.exceptions.c.id == note_id)
    )
    row = found.mappings().first()
    if row is None:
        raise Refusal(404, "There is no such note on this triage run.")
    return row


def _refused_turns(connection: Connection, run_id: str, note_id: str) -> list[str]:
    """The line of each refused turn on a note, in order, whichever attempt it was in."""
    where = [db.steps.c.run_id == run_id, db.steps.c.exception_id == note_id]
    return agent_calls.refused_turns(connection, db.steps, where)


def _uncovered(note: RowMapping) -> bool:
    """Whether the backend settled the note because no step in use had its warning code."""
    return note["state"] == "no_step" and not note["steps"]


def _answered(connection: Connection, run_id: str, note_id: str) -> bool:
    found = connection.execute(
        select(db.steps.c.turn)
        .where(
            db.steps.c.run_id == run_id,
            db.steps.c.exception_id == note_id,
            db.steps.c.outcome == "accepted",
        )
        .limit(1)
    )
    return found.first() is not None


def _out_of_place(
    connection: Connection,
    run: RowMapping,
    attempt: int,
    note: RowMapping,
    turn: int,
    changed: bool,
) -> str | None:
    """Why a tool call cannot be taken as this note's answer, whatever it says, or nothing."""
    if run["attempt"] != attempt:
        return _not_current(run)
    if run["state"] != "working" or note["state"] != "pending":
        return f"The run is {run['state']} and the note is {note['state']}."
    if changed:
        return PLAYBOOKS_CHANGED
    refused = len(_refused_turns(connection, run["id"], note["id"]))
    if refused >= REFUSALS_BEFORE_FALLBACK or turn != refused + 1:
        return NOT_ITS_TURN
    return None


def _gap_out_of_place(
    connection: Connection,
    run: RowMapping,
    attempt: int,
    note: RowMapping,
    turn: int,
    changed: bool,
) -> str | None:
    """Why a tool call cannot answer whether to file a gap report, or nothing."""
    if run["attempt"] != attempt:
        return _not_current(run)
    if run["state"] != "working" or not _uncovered(note):
        return f"The run is {run['state']} and the note is {note['state']}."
    if changed:
        return (
            "The run is working and the playbooks in use changed after it started, "
            "so whether one covers the warning is no longer known."
        )
    if _answered(connection, run["id"], note["id"]):
        return "The note is no_step, and whether to file a gap report is already answered."
    refused = len(_refused_turns(connection, run["id"], note["id"]))
    if refused >= REFUSALS_BEFORE_FALLBACK or turn != refused + 1:
        return "The note is no_step, and that is not its next turn."
    return None


def outcome_summary(connection: Connection) -> dict[str, list[dict]]:
    """Counts from completed runs, and nothing else: no subject, no identifier, no line.

    By warning code and the step chosen, whether or not it gave an action, and by warning
    code for the exceptions no step covered. A note with no verified choice is in neither.
    """
    found = connection.execute(
        select(db.exceptions.c.code, db.notes.c.state, db.notes.c.chosen)
        .join(db.notes, db.notes.c.exception_id == db.exceptions.c.id)
        .join(db.runs, db.runs.c.id == db.exceptions.c.run_id)
        .where(db.runs.c.state == "completed", db.notes.c.state.in_(("chosen", "no_step")))
    )
    by_step: Counter[tuple[str, str, str]] = Counter()
    uncovered: Counter[str] = Counter()
    for row in found:
        if row.state == "chosen":
            chosen = StepInUse.model_validate(row.chosen)
            by_step[row.code, chosen.playbook, chosen.step.id] += 1
        else:
            uncovered[row.code] += 1
    return {
        "by_step": [
            {"code": code, "playbook": playbook, "step": step, "count": count}
            for (code, playbook, step), count in sorted(by_step.items())
        ],
        "no_step": [{"code": code, "count": count} for code, count in sorted(uncovered.items())],
    }


def _has_pending(connection: Connection, run_id: str) -> bool:
    found = connection.execute(
        select(db.notes.c.exception_id)
        .where(db.notes.c.run_id == run_id, db.notes.c.state == "pending")
        .limit(1)
    )
    return found.first() is not None


def _at(key: tuple[str, int, str, int]) -> list:
    run_id, attempt, note_id, turn = key
    return [
        db.steps.c.run_id == run_id,
        db.steps.c.attempt == attempt,
        db.steps.c.exception_id == note_id,
        db.steps.c.turn == turn,
    ]


class Triage:
    def __init__(
        self,
        journal: Journal,
        log: Kept,
        store: Store,
        wealthcontext: WealthContext,
        agent: TriageAgent,
        reports: GapReports,
    ):
        self._journal = journal
        self._log = log
        self._store = store
        self._wealthcontext = wealthcontext
        self._agent = agent
        self._reports = reports

    def _changed(self, connection: Connection, run: RowMapping) -> bool:
        return self._store.current(connection).hash != run["set_hash"]

    def _settled(
        self,
        connection: Connection,
        run: RowMapping,
        note: RowMapping,
        state: models.NoteState,
        chosen: dict | None = None,
    ) -> dict:
        """A note as it is settled, with its action as shown and its receipt, for the log."""
        found = connection.execute(
            select(db.reads).where(db.reads.c.run_id == run["id"]).order_by(db.reads.c.id)
        )
        receipt = _receipt(run, found.mappings().all(), note, _set_of(run))
        shown = _shown({**note, "state": state, "chosen": chosen}, receipt)
        return {"state": state, "chosen": chosen, "note": shown.model_dump(mode="json")}

    # The analyst's move

    def start(self, asked_for: models.StartTriage) -> models.Started:
        with self._journal.transaction() as connection:
            unfinished = _unfinished(connection, asked_for)
        if unfinished is not None:
            return self._start_again(unfinished)
        try:
            reading = self._wealthcontext.read_day(asked_for.date, asked_for.cutoff)
        except WealthContextFailed as failed:
            raise Refusal(502, str(failed)) from None
        run_id, attempt = uuid.uuid4().hex, 1

        def commit() -> dict:
            self._write_run(run_id, attempt, asked_for, reading)
            return {"kind": "triage", "run_id": run_id, "attempt": attempt}

        asked = commit_then_ask(self._agent, commit, lambda: self._undo(run_id, attempt), UNDONE)
        return self._started(run_id, attempt, asked)

    def _start_again(self, found: RowMapping) -> models.Started:
        """The same run under a new attempt. WealthContext is not read again.

        An attempt's number is never given back, even by an undo. A move refused as an old
        attempt's stays refused, so a late agent cannot end the run it was fenced out of.
        """
        run_id, before, attempt = found["id"], found["attempt"], found["attempt"] + 1
        asked_at, last_heard = now(), ""

        def commit() -> dict:
            nonlocal last_heard
            with self._journal.transaction() as connection:
                run = _run(connection, run_id)
                if run["state"] != "working":
                    raise Refusal(409, f"The run is {run['state']}.")
                if run["attempt"] != before or not _stalled(connection, run):
                    raise Refusal(409, NOT_STALLED)
                last_heard = run["heard_at"]
                moved = {
                    "run_id": run_id,
                    "before": before,
                    "attempt": attempt,
                    "last_heard": last_heard,
                }
                self._log.record(connection, triage_log.STARTED_AGAIN, ANALYST, asked_at, moved)
            return {"kind": "triage", "run_id": run_id, "attempt": attempt}

        def undo() -> bool:
            put_back = {
                "run_id": run_id,
                "attempt": attempt,
                "asked_at": asked_at,
                "last_heard": last_heard,
            }

            def unheard(connection: Connection) -> bool:
                return _heard(connection, _run(connection, run_id)) == asked_at

            return self._take_back(triage_log.RESTART_UNDONE, put_back, unheard)

        asked = commit_then_ask(self._agent, commit, undo, AGAIN_UNDONE)
        return self._started(run_id, attempt, asked)

    def _started(
        self, run_id: str, attempt: int, asked: Literal["accepted", "unknown"]
    ) -> models.Started:
        with self._journal.transaction() as connection:
            state = _run(connection, run_id)["state"]
        return models.Started(id=run_id, attempt=attempt, state=state, asked=asked)

    def _write_run(
        self,
        run_id: str,
        attempt: int,
        asked_for: models.StartTriage,
        reading: Reading,
    ) -> None:
        found, notices = exceptions.build(reading)
        with self._journal.transaction() as connection:
            # Another start may have been written while WealthContext was being read.
            if _unfinished(connection, asked_for) is not None:
                raise Refusal(409, NOT_STALLED)
            # Read here, so that a run is never written under a set that is no longer in use.
            playbooks = self._store.current(connection)
            used = models.ReceiptSet(
                id=playbooks.id, version=playbooks.version, hash=playbooks.hash
            )
            written, settled = [], []
            for position, exception in enumerate(found):
                in_use = playbooks.with_code(exception.code)
                each = {
                    "id": uuid.uuid4().hex,
                    "position": position,
                    "steps": [step.model_dump(mode="json") for step in in_use],
                    # With no step in use for its code there is nothing to choose,
                    # so no model is asked.
                    "state": "pending" if in_use else "no_step",
                    **exception.model_dump(mode="json"),
                }
                written.append(each)
                if not in_use:
                    receipt = models.Receipt(
                        date=asked_for.date,
                        cutoff=asked_for.cutoff,
                        reads=reading.reads,
                        feed_run=exception.feed_run,
                        playbook_set=used,
                    )
                    settled.append(
                        _shown({**each, "chosen": None}, receipt).model_dump(mode="json")
                    )
            started = {
                "run_id": run_id,
                "attempt": attempt,
                "date": asked_for.date,
                "cutoff": asked_for.cutoff,
                "playbook_set": used.model_dump(),
                "reads": [read.model_dump(mode="json") for read in reading.reads],
                "exceptions": written,
                "notices": [notice.model_dump(mode="json") for notice in notices],
                "settled": settled,
            }
            self._log.record(connection, triage_log.RUN_STARTED, ANALYST, now(), started)

    def _undo(self, run_id: str, attempt: int) -> bool:
        undone = {"run_id": run_id, "attempt": attempt}
        return self._take_back(triage_log.RUN_UNDONE, undone)

    def _take_back(
        self, event: str, data: dict, only_if: Callable[[Connection], bool] = lambda _: True
    ) -> bool:
        try:
            with self._journal.transaction() as connection:
                if not only_if(connection):
                    return False
                self._log.record(connection, event, BACKEND, now(), data)
        except CannotApply:
            return False  # the agent was heard from, so there is nothing to undo
        return True

    # The triage agent's moves

    def work(self, run_id: str, attempt: int) -> models.TriageWork:
        with self._journal.transaction() as connection:
            run = _run_at(connection, run_id, attempt)
            working = run["state"] == "working"
            changed = working and self._changed(connection, run)
            if changed and _has_pending(connection, run_id):
                raise Refusal(409, PLAYBOOKS_CHANGED)
            taken = connection.execute(select(db.steps).where(db.steps.c.run_id == run_id))
            spent = agent_calls.total(taken.mappings().all()).cost_usd
            rows = connection.execute(
                select(db.exceptions)
                .join(db.notes, db.notes.c.exception_id == db.exceptions.c.id)
                .where(db.exceptions.c.run_id == run_id, db.notes.c.state == "pending")
                .order_by(db.exceptions.c.position)
            ).mappings()
            pending = [
                models.PendingNote(
                    id=row["id"],
                    code=row["code"],
                    feed=_facts(row),
                    warning_count=len(row["subjects"]),
                    options=[
                        models.Offered(
                            playbook=each.playbook,
                            step=each.step.id,
                            when=each.step.when,
                            line=each.step.line,
                        )
                        for each in _in_use(row)
                    ],
                    refusals=_refused_turns(connection, run_id, row["id"]),
                )
                for row in rows
            ]
            uncovered = []
            if working and not changed:
                uncovered = self._still_to_ask(connection, run_id)
        return models.TriageWork(
            state=run["state"], spent_usd=spent, notes=pending, uncovered=uncovered
        )

    def _still_to_ask(self, connection: Connection, run_id: str) -> list[models.UncoveredNote]:
        """The notes no playbook covers that have no answer yet, and a turn left for one."""
        found = connection.execute(
            select(db.exceptions, db.notes.c.state)
            .join(db.notes, db.notes.c.exception_id == db.exceptions.c.id)
            .where(db.exceptions.c.run_id == run_id)
            .order_by(db.exceptions.c.position)
        ).mappings()
        asking = []
        for row in found.all():
            if not _uncovered(row) or _answered(connection, run_id, row["id"]):
                continue
            refusals = _refused_turns(connection, run_id, row["id"])
            if len(refusals) < REFUSALS_BEFORE_FALLBACK:
                asking.append(
                    models.UncoveredNote(
                        id=row["id"],
                        code=row["code"],
                        feed=_facts(row),
                        warning_count=len(row["subjects"]),
                        refusals=refusals,
                    )
                )
        return asking

    def record_choice(
        self, run_id: str, attempt: int, note_id: str, sent: models.RecordChoice
    ) -> models.Moved:
        refusal: tuple[int, str] | None = None
        state: models.NoteState = "pending"
        with self._journal.transaction() as connection:
            run = _run(connection, run_id)
            note = _note(connection, run_id, note_id)
            if attempt > run["attempt"]:
                _current(run, attempt)  # an attempt that was never made has no calls to record
            key, move = (
                (run_id, attempt, note_id, sent.turn),
                agent_calls.digest("record_choice", sent),
            )
            if (before := self._same_move(connection, key, move)) is not None:
                _current(run, attempt)
                if before["outcome"] == "refused":
                    raise Refusal(before["refused_status"], before["line"])
                return models.Moved(state=note["state"])

            in_use, facts = _in_use(note), _facts(note)
            named: StepInUse | None = None
            wrong: str | None = None
            choice = sent.choice
            if isinstance(choice, models.StepChoice):
                tool = "choose_step"
                named = next(
                    (
                        each
                        for each in in_use
                        if (each.playbook, each.step.id) == (choice.playbook, choice.step)
                    ),
                    None,
                )
                if named is None:
                    wrong = NOT_IN_USE
                elif why := matching.misfit(named.step, note["code"], facts):
                    wrong = DOES_NOT_FIT + why
            else:
                tool = "no_step_applies"
                if matching.some_step_fits([each.step for each in in_use], note["code"], facts):
                    wrong = A_STEP_APPLIES

            changed = self._changed(connection, run)
            misplaced = _out_of_place(connection, run, attempt, note, sent.turn, changed)
            settled: dict | None = None
            if misplaced:
                refusal = (OUT_OF_PLACE, misplaced)
            elif wrong:
                refusal = (422, wrong)
            else:
                chosen = named.model_dump(mode="json") if named else None
                state = "chosen" if named else "no_step"
                settled = self._settled(connection, run, note, state, chosen)
            self._record_calls(
                connection,
                key,
                move,
                sent.usage,
                [tool],
                # Only an option that was offered is kept, so no other text can be stored.
                option=f"{named.playbook}/{named.step.id}" if named else None,
                refusal=refusal,
                # Only a call that was this note's to make is hearing from the run's agent.
                heard=not misplaced,
                settled=settled,
            )
        # Raised after the transaction, so that a refused call is still a step on the run.
        if refusal is not None:
            raise Refusal(*refusal)
        return models.Moved(state=state)

    def record_unusable_reply(
        self, run_id: str, attempt: int, note_id: str, sent: models.UnusableReply
    ) -> models.Refused:
        with self._journal.transaction() as connection:
            run = _run(connection, run_id)
            note = _note(connection, run_id, note_id)
            if attempt > run["attempt"]:
                _current(run, attempt)
            key = (run_id, attempt, note_id, sent.turn)
            move = agent_calls.digest("record_unusable_reply", sent)
            if (before := self._same_move(connection, key, move)) is not None:
                _current(run, attempt)
                status, line = before["refused_status"], before["line"]
            else:
                changed = self._changed(connection, run)
                if _uncovered(note):
                    place, lines = _gap_out_of_place, agent_calls.GAP_UNUSABLE
                else:
                    place, lines = _out_of_place, UNUSABLE
                misplaced = place(connection, run, attempt, note, sent.turn, changed)
                if misplaced:
                    status, line = OUT_OF_PLACE, misplaced
                else:
                    status, line = agent_calls.refusal_status(sent.reason), lines[sent.reason]
                calls = sent.calls or [None]  # a reply in words is a refused turn too
                self._record_calls(
                    connection,
                    key,
                    move,
                    sent.usage,
                    calls,
                    refusal=(status, line),
                    heard=not misplaced,
                )
        if status == OUT_OF_PLACE:
            raise Refusal(status, line)
        return models.Refused(refusal=line)

    def record_fallback(self, run_id: str, attempt: int, note_id: str) -> models.Moved:
        with self._journal.transaction() as connection:
            run = _run_at(connection, run_id, attempt)
            note = _note(connection, run_id, note_id)
            if note["state"] == "unverified":
                return models.Moved(state="unverified")
            if run["state"] != "working" or note["state"] != "pending":
                raise Refusal(409, f"The run is {run['state']} and the note is {note['state']}.")
            if self._changed(connection, run):
                raise Refusal(409, PLAYBOOKS_CHANGED)
            if len(_refused_turns(connection, run_id, note_id)) < REFUSALS_BEFORE_FALLBACK:
                raise Refusal(409, "The note is pending: fallback follows a second refusal only.")
            moved = {
                "run_id": run_id,
                "attempt": attempt,
                "note_id": note_id,
                "settled": self._settled(connection, run, note, "unverified"),
            }
            self._log.record(connection, triage_log.FELL_BACK, AGENT, now(), moved)
        return models.Moved(state="unverified")

    def file_gap_report(
        self, run_id: str, attempt: int, note_id: str, sent: models.FileGapReport
    ) -> models.GapFiled:
        """The run, exception and playbook come from here. The model gives only the summary."""
        with self._journal.transaction() as connection:
            known = connection.execute(select(db.runs.c.id).where(db.runs.c.id == run_id))
            if known.first() is None:
                raise Refusal(409, NOT_RECORDED_HERE)
            _note(connection, run_id, note_id)
        self._reports.make_key()
        refusal: tuple[int, str] | None = None
        with self._journal.transaction() as connection:
            run = connection.execute(select(db.runs).where(db.runs.c.id == run_id)).mappings()
            if (found := run.first()) is None:  # undone meanwhile
                raise Refusal(409, NOT_RECORDED_HERE)
            note = _note(connection, run_id, note_id)
            if attempt > found["attempt"]:
                _current(found, attempt)
            first = self._reports.first(connection, run_id, note)
            key, move = (run_id, attempt, note_id, sent.turn), agent_calls.digest("file", sent)
            if (before := self._same_move(connection, key, move)) is not None:
                _current(found, attempt)
                if before["outcome"] == "refused":
                    raise Refusal(before["refused_status"], before["line"])
                assert first is not None  # an accepted filing left one
                return _filed(first)

            rule = personal_data.broken_rule(sent.summary, personal_data.SUMMARY_LENGTH)
            changed = self._changed(connection, found)
            misplaced = _gap_out_of_place(connection, found, attempt, note, sent.turn, changed)
            # The same trace, code and subject again gives the first report, whenever it is.
            again = first is not None and rule is None and found["attempt"] == attempt
            if misplaced and not again:
                refusal = (OUT_OF_PLACE, misplaced)
            elif rule:
                refusal = (422, personal_data.refusal("summary", rule))
            report = None
            if refusal is None and first is None:
                report = self._reports.drafted(
                    connection,
                    run_id,
                    note,
                    sent.summary,
                    playbook=None,  # no playbook had the warning's code when the run started
                    version=None,
                    correction_id=None,
                )
            named = None if refusal else report or first
            made = self._made(
                connection,
                key,
                move,
                sent.usage,
                ["file_gap_report"],
                option=named["reference"] if named else None,
                refusal=refusal,
                heard=not misplaced,
            )
            if rule and not misplaced:
                data = {"rule": rule, "made": made}
                event = ledger_events.GAP_REPORT_REFUSED
                self._store.kept.record(connection, event, AGENT, now(), data)
            elif report is not None:
                data = {"report": report, "made": made}
                event = ledger_events.GAP_REPORT_FILED
                self._store.kept.record(connection, event, AGENT, now(), data)
                return models.GapFiled(id=report["id"], reference=report["reference"], state="open")
            else:
                self._log.record(connection, triage_log.TOOL_CALLS, AGENT, now(), made)
                if refusal is None:
                    assert first is not None
                    return _filed(first)
        # Raised after the transaction, so that a refused call is still a step on the run.
        assert refusal is not None
        raise Refusal(*refusal)

    def record_no_gap_report(
        self, run_id: str, attempt: int, note_id: str, sent: models.NoGapReport
    ) -> models.Answered:
        refusal: tuple[int, str] | None = None
        with self._journal.transaction() as connection:
            run = _run(connection, run_id)
            note = _note(connection, run_id, note_id)
            if attempt > run["attempt"]:
                _current(run, attempt)
            key, move = (run_id, attempt, note_id, sent.turn), agent_calls.digest("none", sent)
            if (before := self._same_move(connection, key, move)) is not None:
                _current(run, attempt)
                if before["outcome"] == "refused":
                    raise Refusal(before["refused_status"], before["line"])
                return models.Answered(answer="no_gap_report")
            changed = self._changed(connection, run)
            misplaced = _gap_out_of_place(connection, run, attempt, note, sent.turn, changed)
            if misplaced:
                refusal = (OUT_OF_PLACE, misplaced)
            self._record_calls(
                connection,
                key,
                move,
                sent.usage,
                ["no_gap_report"],
                refusal=refusal,
                heard=not misplaced,
            )
        if refusal is not None:
            raise Refusal(*refusal)
        return models.Answered(answer="no_gap_report")

    def complete(self, run_id: str, attempt: int) -> models.Moved:
        with self._journal.transaction() as connection:
            run = _run_at(connection, run_id, attempt)
            if run["state"] == "completed":
                return models.Moved(state="completed")
            if run["state"] != "working":
                raise Refusal(409, f"The run is {run['state']}.")
            if _has_pending(connection, run_id):
                raise Refusal(409, "The run is working and a note is still pending.")
            moved = {"run_id": run_id, "attempt": attempt}
            self._log.record(connection, triage_log.COMPLETED, AGENT, now(), moved)
        return models.Moved(state="completed")

    def fail(self, run_id: str, attempt: int, sent: models.FailTriage) -> models.Moved:
        with self._journal.transaction() as connection:
            run = _run_at(connection, run_id, attempt)
            if (run["state"], run["failure"]) == ("failed", sent.reason):
                return models.Moved(state="failed")
            if run["state"] != "working":
                raise Refusal(409, f"The run is {run['state']}.")
            moved = {"run_id": run_id, "attempt": attempt, "reason": sent.reason}
            self._log.record(connection, triage_log.FAILED, AGENT, now(), moved)
        return models.Moved(state="failed")

    def _same_move(
        self, connection: Connection, key: tuple[str, int, str, int], move: str
    ) -> RowMapping | None:
        """What this turn of this attempt recorded, when this is the same move sent again."""
        found = connection.execute(select(db.steps).where(*_at(key), db.steps.c.move == move))
        return found.mappings().first()

    def _made(
        self,
        connection: Connection,
        key: tuple[str, int, str, int],
        move: str,
        usage: models.Usage,
        tools: Sequence[str | None],
        *,
        heard: bool,
        option: str | None = None,
        refusal: tuple[int, str] | None = None,
        settled: dict | None = None,
    ) -> dict:
        run_id, attempt, note_id, turn = key
        calls = agent_calls.written(
            connection, db.steps, _at(key), usage, tools, option=option, refusal=refusal
        )
        return {
            "run_id": run_id,
            "attempt": attempt,
            "note_id": note_id,
            "turn": turn,
            "move": move,
            "heard": heard,
            "calls": calls,
            "settled": settled,
        }

    def _record_calls(
        self,
        connection: Connection,
        key: tuple[str, int, str, int],
        move: str,
        usage: models.Usage,
        tools: Sequence[str | None],
        **how: Any,
    ) -> None:
        made = self._made(connection, key, move, usage, tools, **how)
        self._log.record(connection, triage_log.TOOL_CALLS, AGENT, now(), made)

    # What people read

    def latest(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._journal.transaction() as connection:
            found = connection.execute(
                select(db.runs).order_by(db.runs.c.started_at.desc(), db.runs.c.id).limit(limit)
            )
            return [{**run, "stalled": _stalled(connection, run)} for run in found.mappings().all()]

    def read(self, run_id: str) -> models.TriageRun:
        with self._journal.transaction() as connection:
            run = _run(connection, run_id)

            def rows(query: Any) -> Sequence[RowMapping]:
                return connection.execute(query).mappings().all()

            reads = rows(
                select(db.reads).where(db.reads.c.run_id == run_id).order_by(db.reads.c.id)
            )
            noted = rows(
                select(db.exceptions, db.notes.c.state, db.notes.c.chosen)
                .join(db.notes, db.notes.c.exception_id == db.exceptions.c.id)
                .where(db.exceptions.c.run_id == run_id)
                .order_by(db.exceptions.c.position)
            )
            notices = rows(
                select(db.notices).where(db.notices.c.run_id == run_id).order_by(db.notices.c.id)
            )
            in_order = ("at", "attempt", "turn", "call", "exception_id")
            steps = rows(
                select(db.steps)
                .where(db.steps.c.run_id == run_id)
                .order_by(*(db.steps.c[name] for name in in_order))
            )
            stalled = _stalled(connection, run)
        playbook_set = _set_of(run)
        return models.TriageRun(
            id=run["id"],
            attempt=run["attempt"],
            state=run["state"],
            stalled=stalled,
            failure=FAILURES[run["failure"]] if run["failure"] else None,
            date=run["date"],
            cutoff=run["cutoff"],
            playbook_set=playbook_set,
            started_at=run["started_at"],
            finished_at=run["finished_at"],
            notes=[_shown(row, _receipt(run, reads, row, playbook_set)) for row in noted],
            notices=[exceptions.Notice(code=r["code"], subject=r["subject"]) for r in notices],
            steps=[
                models.StepTaken(
                    note_id=row["exception_id"],
                    attempt=row["attempt"],
                    turn=row["turn"],
                    tool=row["tool"],
                    option=row["option"],
                    outcome=row["outcome"],
                    line=row["line"],
                    model=row["model"],
                )
                for row in steps
            ],
            usage=agent_calls.total(steps),
        )


def _set_of(run: RowMapping) -> models.ReceiptSet:
    return models.ReceiptSet(id=run["set_id"], version=run["set_version"], hash=run["set_hash"])


def _receipt(
    run: RowMapping,
    reads: Sequence[RowMapping],
    exception: RowMapping,
    playbook_set: models.ReceiptSet,
) -> models.Receipt:
    # Every note rests on every read of its run: a warning stands alone only because
    # none of the failed runs' impacts claimed it.
    return models.Receipt(
        date=run["date"],
        cutoff=run["cutoff"],
        reads=[models.Read.model_validate(dict(read)) for read in reads],
        feed_run=exception["feed_run"],
        playbook_set=playbook_set,
    )


def _shown(row: Mapping[Any, Any], receipt: models.Receipt) -> models.Note:
    chosen = StepInUse.model_validate(row["chosen"]) if row["chosen"] else None
    # What a person is told to do comes from an approved version, and from nothing else.
    approved = chosen if chosen and chosen.approved_by else None
    statement = UNAPPROVED if chosen and not approved else STATEMENTS[row["state"]]
    return models.Note(
        id=row["id"],
        code=row["code"],
        subjects=row["subjects"],
        state=row["state"],
        statement=statement,
        step=(
            models.NoteStep(
                playbook=chosen.playbook,
                version=chosen.version,
                id=chosen.step.id,
                approved_by=chosen.approved_by,
            )
            if chosen
            else None
        ),
        action=(
            models.Action(
                tell=approved.step.tell,
                desk=approved.desk,
                by=approved.step.by,
                hold_report=approved.step.hold_report,
                line=approved.step.line,
            )
            if approved
            else None
        ),
        receipt=receipt,
    )


def _filed(report: RowMapping) -> models.GapFiled:
    return models.GapFiled(id=report["id"], reference=report["reference"], state=report["state"])
