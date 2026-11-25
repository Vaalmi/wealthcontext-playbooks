"""The correction and its review as a state machine.

A correction is fixed fields and one bounded reason, never a transcript. It is a firm
source from its ledger line on, whether or not the triage agent ever reviews it. The
review's every move and call is in the ledger too, and no line holds a client identifier.

The value a step has for the corrected field is read to check the correction and is never
stored or shown: a version with no recorded approval gives out no action.
"""

import uuid
from typing import Literal

from sqlalchemy import Connection, RowMapping, select

from backend import agent_calls, models, personal_data, references, sources, triage
from backend import database as db
from backend import ledger_events as events
from backend.agent_calls import OUT_OF_PLACE, REFUSALS_BEFORE_FALLBACK
from backend.asking import TriageAgent, commit_then_ask
from backend.gap_reports import GapReports
from backend.journal import CannotApply, Journal
from backend.playbook_format import Scope
from backend.refusals import Refusal
from backend.store import StepInUse, Store
from backend.vocabulary import AGENT, ANALYST, BACKEND

NOT_STALLED = (
    "The correction is recorded and its review has not stalled. "
    "It can be asked for again when nothing has been heard for five minutes."
)
AGAIN_UNDONE = "The correction stands, and its review can be asked for again."
NOT_RECORDED_HERE = "The correction's run is not one recorded here."


def _correction(connection: Connection, correction_id: str) -> RowMapping:
    found = connection.execute(select(db.corrections).where(db.corrections.c.id == correction_id))
    row = found.mappings().first()
    if row is None:
        raise Refusal(404, "There is no such correction.")
    return row


def _source(connection: Connection, correction: RowMapping) -> RowMapping:
    found = connection.execute(select(db.sources).where(db.sources.c.id == correction["source_id"]))
    return found.mappings().one()


def _note(connection: Connection, run_id: str, note_id: str) -> RowMapping | None:
    found = connection.execute(
        select(db.exceptions, db.notes.c.state, db.notes.c.chosen)
        .join(db.notes, db.notes.c.exception_id == db.exceptions.c.id)
        .where(db.exceptions.c.run_id == run_id, db.exceptions.c.id == note_id)
    )
    return found.mappings().first()


def _its_note(connection: Connection, correction: RowMapping) -> RowMapping:
    note = _note(connection, correction["run_id"], correction["note_id"])
    if note is None:
        raise Refusal(409, NOT_RECORDED_HERE)
    return note


def _stalled(correction: RowMapping) -> bool:
    heard = correction["heard_at"]
    return (
        correction["state"] == "recorded" and heard is not None and heard <= triage.stalled_since()
    )


def _not_current(correction: RowMapping) -> str:
    return f"That attempt is not the current one; the correction is {correction['state']}."


def _current(correction: RowMapping, attempt: int) -> None:
    if correction["attempt"] != attempt:
        raise Refusal(409, _not_current(correction))


def _refusals(connection: Connection, correction_id: str) -> list[str]:
    where = [db.review_steps.c.correction_id == correction_id]
    return agent_calls.refused_turns(connection, db.review_steps, where)


def _out_of_place(
    connection: Connection, correction: RowMapping, attempt: int, turn: int
) -> str | None:
    if correction["attempt"] != attempt:
        return _not_current(correction)
    if correction["state"] != "recorded":
        return f"The correction is {correction['state']}."
    refused = len(_refusals(connection, correction["id"]))
    if refused >= REFUSALS_BEFORE_FALLBACK or turn != refused + 1:
        return "The correction is recorded, and that is not its review's next turn."
    return None


def _at(correction_id: str, attempt: int, turn: int) -> list:
    return [
        db.review_steps.c.correction_id == correction_id,
        db.review_steps.c.attempt == attempt,
        db.review_steps.c.turn == turn,
    ]


def _same_move(
    connection: Connection, correction_id: str, attempt: int, turn: int, move: str
) -> RowMapping | None:
    found = connection.execute(
        select(db.review_steps).where(
            *_at(correction_id, attempt, turn), db.review_steps.c.move == move
        )
    )
    return found.mappings().first()


class Corrections:
    def __init__(self, journal: Journal, store: Store, agent: TriageAgent, reports: GapReports):
        self._journal = journal
        self._store = store
        self._agent = agent
        self._reports = reports

    # The analyst's moves

    def record(
        self, run_id: str, note_id: str, sent: models.RecordCorrection
    ) -> models.CorrectionRecorded:
        rule: personal_data.Rule | None = None
        correction_id, asked_at = uuid.uuid4().hex, triage.now()
        with self._journal.transaction() as connection:
            source = self._as_source(connection, run_id, note_id, sent)
            content = {name: source[name] for name in source if name not in ("id", "sha256")}
            rule = personal_data.broken_rule(sent.reason, personal_data.REASON_LENGTH)
            if rule is not None:
                sha256 = references.content_sha256(content)
                sources.refuse_once(connection, self._store, "correction", rule, sha256, ANALYST)
            else:
                before = connection.execute(
                    select(db.corrections).where(
                        db.corrections.c.run_id == run_id,
                        db.corrections.c.note_id == note_id,
                        db.corrections.c.source_id == source["id"],
                    )
                )
                if (row := before.mappings().first()) is not None:
                    # The same move again. Asking again is `request_review`.
                    return _told(row, "not_asked")
                recorded = {
                    "correction_id": correction_id,
                    "run_id": run_id,
                    "note_id": note_id,
                    "source": source,
                }
                self._store.kept.record(
                    connection, events.CORRECTION_RECORDED, ANALYST, asked_at, recorded
                )
        if rule is not None:
            raise Refusal(422, personal_data.refusal("reason", rule))

        # Committed first. An agent that is down undoes the asking, never the correction.
        payload = {"kind": "review", "correction_id": correction_id, "attempt": 1}
        asked, why = self._agent.ask(payload)
        if asked == "not_started" and not self._not_started(correction_id, 1, asked_at):
            asked, why = "accepted", None  # it has been heard from, so it did start
        with self._journal.transaction() as connection:
            return _told(_correction(connection, correction_id), asked, why)

    def _as_source(
        self, connection: Connection, run_id: str, note_id: str, sent: models.RecordCorrection
    ) -> dict:
        run = connection.execute(select(db.runs).where(db.runs.c.id == run_id)).mappings().first()
        if run is None:
            raise Refusal(404, "There is no such triage run.")
        note = _note(connection, run_id, note_id)
        if note is None:
            raise Refusal(404, "There is no such note on this triage run.")
        if run["state"] != "completed":
            raise Refusal(409, f"The run is {run['state']}: only a completed one is corrected.")
        if note["state"] != "chosen":
            raise Refusal(409, f"The note is {note['state']}: it has no step chosen to correct.")
        # From the chosen step, as a note shows no action for a version nobody approved.
        chosen = StepInUse.model_validate(note["chosen"])
        field, should_be = sent.change.field, sent.change.should_be
        if field == "tell" and should_be not in self._store.desks:
            raise Refusal(422, "The desk to tell is not on the list of desks.")
        if getattr(chosen.step, field) == should_be:
            raise Refusal(422, "The step already says that, so there is nothing to correct.")
        scope = Scope(playbook=chosen.playbook, code=chosen.step.code, when=chosen.step.when)
        content = {
            "kind": "correction",
            # The business date triaged, so that the same correction is the same on any day.
            "date": run["date"],
            "desk": None,
            "scope": scope.model_dump(mode="json"),
            "correction": {
                "version": chosen.version,
                "step": chosen.step.id,
                "field": field,
                "should_be": should_be,
            },
            "text": sent.reason,
        }
        return {
            "id": references.reference("correction", content),
            "sha256": references.content_sha256(content),
            **content,
        }

    def _not_started(self, correction_id: str, attempt: int, asked_at: str) -> bool:
        undone = {"correction_id": correction_id, "attempt": attempt, "asked_at": asked_at}
        try:
            with self._journal.transaction() as connection:
                self._store.kept.record(
                    connection, events.REVIEW_NOT_STARTED, BACKEND, triage.now(), undone
                )
        except CannotApply:
            return False
        return True

    def request_review(self, correction_id: str) -> models.CorrectionRecorded:
        """Ask again, under a new attempt, whose number is never given back."""
        asked_at, attempt = triage.now(), 0

        def commit() -> dict:
            nonlocal attempt
            with self._journal.transaction() as connection:
                found = _correction(connection, correction_id)
                if found["state"] != "recorded":
                    raise Refusal(409, f"The correction is {found['state']}.")
                # Nothing heard at all, when asking never started, or none for five minutes.
                if found["heard_at"] is not None and not _stalled(found):
                    raise Refusal(409, NOT_STALLED)
                attempt = found["attempt"] + 1
                moved = {
                    "correction_id": correction_id,
                    "before": found["attempt"],
                    "attempt": attempt,
                    "last_heard": found["heard_at"],
                }
                self._store.kept.record(
                    connection, events.REVIEW_ASKED_AGAIN, ANALYST, asked_at, moved
                )
            return {"kind": "review", "correction_id": correction_id, "attempt": attempt}

        def undo() -> bool:
            return self._not_started(correction_id, attempt, asked_at)

        asked = commit_then_ask(self._agent, commit, undo, AGAIN_UNDONE)
        with self._journal.transaction() as connection:
            return _told(_correction(connection, correction_id), asked)

    # The triage agent's moves

    def work(self, correction_id: str, attempt: int) -> models.ReviewWork:
        with self._journal.transaction() as connection:
            found = _correction(connection, correction_id)
            _current(found, attempt)
            note, source = _its_note(connection, found), _source(connection, found)
            chosen = StepInUse.model_validate(note["chosen"])
            taken = connection.execute(
                select(db.review_steps).where(db.review_steps.c.correction_id == correction_id)
            )
            return models.ReviewWork(
                state=found["state"],
                spent_usd=agent_calls.total(taken.mappings().all()).cost_usd,
                code=note["code"],
                feed=note["facts"],
                warning_count=len(note["subjects"]),
                step=models.Offered(
                    playbook=chosen.playbook,
                    step=chosen.step.id,
                    when=chosen.step.when,
                    line=chosen.step.line,
                ),
                version=chosen.version,
                field=source["correction"]["field"],
                should_be=source["correction"]["should_be"],
                reason=source["text"],
                refusals=_refusals(connection, correction_id),
            )

    def record_review(
        self, correction_id: str, attempt: int, sent: models.RecordReview
    ) -> models.Reviewed:
        review = sent.review
        summary = review.summary if isinstance(review, models.ReviewFiles) else None
        if summary is not None:
            with self._journal.transaction() as connection:
                _its_note(connection, _correction(connection, correction_id))
            self._reports.make_key()
        tool = "no_gap_report" if summary is None else "file_gap_report"
        refusal: tuple[int, str] | None = None
        with self._journal.transaction() as connection:
            found = _correction(connection, correction_id)
            if attempt > found["attempt"]:
                _current(found, attempt)
            move = agent_calls.digest("record_review", sent)
            if before := _same_move(connection, correction_id, attempt, sent.turn, move):
                _current(found, attempt)
                if before["outcome"] == "refused":
                    raise Refusal(before["refused_status"], before["line"])
                return _reviewed(connection, found)

            note, first, rule = None, None, None
            if summary is not None:
                note = _its_note(connection, found)
                rule = personal_data.broken_rule(summary, personal_data.SUMMARY_LENGTH)
                first = self._reports.first(connection, found["run_id"], note)
            misplaced = _out_of_place(connection, found, attempt, sent.turn)
            # The same trace, code and subject again gives the first report, reviewed or not.
            again = first is not None and rule is None and found["attempt"] == attempt
            if misplaced and not again:
                refusal = (OUT_OF_PLACE, misplaced)
            elif rule:
                refusal = (422, personal_data.refusal("summary", rule))

            report = None
            if note is not None and refusal is None and first is None:
                assert summary is not None
                chosen = StepInUse.model_validate(note["chosen"])
                report = self._reports.drafted(
                    connection,
                    found["run_id"],
                    note,
                    summary,
                    playbook=chosen.playbook,
                    version=chosen.version,
                    correction_id=correction_id,
                )
            option = None
            if refusal is None and (named := report or first):
                option = named["reference"]
            made = self._made(
                connection,
                (correction_id, attempt, sent.turn),
                move,
                sent.usage,
                [tool],
                option=option,
                refusal=refusal,
                heard=not misplaced,
            )
            if rule and not misplaced:
                event, data = events.GAP_REPORT_REFUSED, {"rule": rule, "made": made}
            elif misplaced:
                event, data = events.REVIEW_CALLS, made
            elif report is not None:
                event, data = events.GAP_REPORT_FILED, {"report": report, "made": made}
            else:
                event = events.CORRECTION_REVIEWED
                data = made | {
                    "outcome": "filed" if first else "none",
                    "gap_report_id": first["id"] if first else None,
                }
            self._store.kept.record(connection, event, AGENT, triage.now(), data)
            if refusal is None:
                done = _reviewed(connection, _correction(connection, correction_id))
                return done.model_copy(update={"gap_report": option}) if option else done
        # Raised after the transaction, so that a refused call is still a step on the review.
        raise Refusal(*refusal)

    def record_unusable_reply(
        self, correction_id: str, attempt: int, sent: models.UnusableReply
    ) -> models.Refused:
        with self._journal.transaction() as connection:
            found = _correction(connection, correction_id)
            if attempt > found["attempt"]:
                _current(found, attempt)
            move = agent_calls.digest("record_unusable_review", sent)
            if before := _same_move(connection, correction_id, attempt, sent.turn, move):
                _current(found, attempt)
                status, line = before["refused_status"], before["line"]
            else:
                misplaced = _out_of_place(connection, found, attempt, sent.turn)
                if misplaced:
                    status, line = OUT_OF_PLACE, misplaced
                else:
                    status = agent_calls.refusal_status(sent.reason)
                    line = agent_calls.GAP_UNUSABLE[sent.reason]
                made = self._made(
                    connection,
                    (correction_id, attempt, sent.turn),
                    move,
                    sent.usage,
                    sent.calls or [None],  # a reply in words is a refused turn too
                    refusal=(status, line),
                    heard=not misplaced,
                )
                self._store.kept.record(connection, events.REVIEW_CALLS, AGENT, triage.now(), made)
        if status == OUT_OF_PLACE:
            raise Refusal(status, line)
        return models.Refused(refusal=line)

    def record_fallback(self, correction_id: str, attempt: int) -> models.Reviewed:
        with self._journal.transaction() as connection:
            found = _correction(connection, correction_id)
            _current(found, attempt)
            if found["outcome"] == "refused_twice":
                return _reviewed(connection, found)
            if found["state"] != "recorded":
                raise Refusal(409, f"The correction is {found['state']}.")
            if len(_refusals(connection, correction_id)) < REFUSALS_BEFORE_FALLBACK:
                raise Refusal(
                    409, "The correction is recorded: fallback follows a second refusal only."
                )
            moved = {
                "correction_id": correction_id,
                "attempt": attempt,
                "calls": [],
                "outcome": "refused_twice",
                "gap_report_id": None,
            }
            self._store.kept.record(
                connection, events.CORRECTION_REVIEWED, AGENT, triage.now(), moved
            )
            return _reviewed(connection, _correction(connection, correction_id))

    def record_stopped(
        self, correction_id: str, attempt: int, sent: models.StopReview
    ) -> models.Reviewed:
        with self._journal.transaction() as connection:
            found = _correction(connection, correction_id)
            _current(found, attempt)
            if found["state"] != "recorded":
                raise Refusal(409, f"The correction is {found['state']}.")
            if found["stopped"] == sent.reason and found["heard_at"] is None:
                return _reviewed(connection, found)
            moved = {"correction_id": correction_id, "attempt": attempt, "reason": sent.reason}
            self._store.kept.record(connection, events.REVIEW_STOPPED, AGENT, triage.now(), moved)
            return _reviewed(connection, _correction(connection, correction_id))

    def _made(
        self,
        connection: Connection,
        key: tuple[str, int, int],
        move: str,
        usage: models.Usage,
        tools: list,
        *,
        heard: bool,
        option: str | None = None,
        refusal: tuple[int, str] | None = None,
    ) -> dict:
        correction_id, attempt, turn = key
        calls = agent_calls.written(
            connection, db.review_steps, _at(*key), usage, tools, option=option, refusal=refusal
        )
        return {
            "correction_id": correction_id,
            "attempt": attempt,
            "turn": turn,
            "move": move,
            "heard": heard,
            "calls": calls,
        }

    # What people read

    def read(self, correction_id: str) -> models.Correction:
        with self._journal.transaction() as connection:
            return _shown(connection, _correction(connection, correction_id))

    def listed(self) -> models.CorrectionList:
        with self._journal.transaction() as connection:
            found = connection.execute(
                select(db.corrections).order_by(db.corrections.c.recorded_at, db.corrections.c.id)
            ).mappings()
            shown = [_shown(connection, row) for row in found.all()]
        return models.CorrectionList(corrections=shown)


def _told(
    row: RowMapping,
    asked: Literal["accepted", "unknown", "not_started", "not_asked"],
    why: str | None = None,
) -> models.CorrectionRecorded:
    return models.CorrectionRecorded(
        id=row["id"],
        source_id=row["source_id"],
        state=row["state"],
        attempt=row["attempt"],
        asked=asked,
        why=why,
    )


def _reviewed(connection: Connection, row: RowMapping) -> models.Reviewed:
    return models.Reviewed(state=row["state"], gap_report=_filed(connection, row))


def _filed(connection: Connection, row: RowMapping) -> str | None:
    if row["gap_report_id"] is None:
        return None
    return connection.execute(
        select(db.gap_reports.c.reference).where(db.gap_reports.c.id == row["gap_report_id"])
    ).scalar_one()


def _shown(connection: Connection, row: RowMapping) -> models.Correction:
    source = _source(connection, row)
    steps = connection.execute(
        select(db.review_steps)
        .where(db.review_steps.c.correction_id == row["id"])
        .order_by(*(db.review_steps.c[name] for name in ("at", "attempt", "turn", "call")))
    ).mappings()
    taken = steps.all()
    return models.Correction(
        id=row["id"],
        source_id=row["source_id"],
        run_id=row["run_id"],
        note_id=row["note_id"],
        date=source["date"],
        scope=source["scope"],
        **source["correction"],
        reason=source["text"],
        state=row["state"],
        attempt=row["attempt"],
        asked=row["heard_at"] is not None,
        stalled=_stalled(row),
        stopped=triage.REVIEW_FAILURES[row["stopped"]] if row["stopped"] else None,
        outcome=row["outcome"],
        gap_report=_filed(connection, row),
        recorded_at=row["recorded_at"],
        reviewed_at=row["reviewed_at"],
        steps=[
            models.ReviewStep.model_validate(
                {name: each[name] for name in models.ReviewStep.model_fields}
            )
            for each in taken
        ],
        usage=agent_calls.total(taken),
    )
