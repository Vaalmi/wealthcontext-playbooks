"""The curator run as a state machine.

A run is one bundle and at most one proposal. The bundle is assembled and compared with the
held-out cases before the curator is asked, and the run keeps it whole: a run started
again is given the same. Every move and every tool call is a ledger line, and no line
holds a client identifier, because nothing the curator is given holds one.

An attempt's number is never given back, so a move from an attempt that was fenced out
stays refused.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Connection, RowMapping, select

from backend import agent_calls, bundle, cases, models, proposals, references, triage
from backend import database as db
from backend import ledger_events as events
from backend.agent_calls import OUT_OF_PLACE, REFUSALS_BEFORE_FALLBACK
from backend.asking import Curator, commit_then_ask
from backend.journal import CannotApply, Journal
from backend.refusals import Refusal
from backend.settings import Settings
from backend.store import Store
from backend.vocabulary import BACKEND, CURATOR, OWNER

NO_MODEL = "No model is configured for the curator: set CURATOR_MODEL. Nothing was started."
BAD_MODEL = "CURATOR_MODEL is no provider:name that can be kept in the record. Nothing was started."
WRITER = (
    "CURATOR_MODEL is {}, which wrote the held-out cases' expected notes: a curator is not "
    "tested by its own answers. Nothing was started."
)
NOT_STALLED = (
    "The curator run is working and has not stalled. "
    "It can be started again when nothing has been heard for five minutes."
)
UNDONE = "The run was undone."
AGAIN_UNDONE = (
    "Starting it again was undone: the run is working and still stalled, "
    "so it can be started again."
)
CANNOT_CHECK = (
    "The held-out cases cannot be read, so the bundle cannot be shown free of them. "
    "Nothing was started."
)
UNUSABLE: dict[models.UnusableReason, str] = {
    "no_tool_call": "Answer with exactly one tool call: submit_proposal or no_change.",
    "several_tool_calls": "Make exactly one tool call; none of those was recorded as the answer.",
    "unknown_tool": "There is no such tool: use submit_proposal or no_change.",
    "invalid_arguments": "The arguments do not fit the tool: give each field its schema names, "
    "in its type, and no other.",
    "no_answer": "No answer came back from the model; the most the call could have cost is "
    "counted as spent.",
}
FAILURES: dict[str, str] = {
    events.HELD_OUT: "An item of the bundle is a held-out case or a file that grades them, "
    "so the run was ended.",
    "refused_twice": "The curator's answer was refused twice.",
    "model_failed": "The model could not be called.",
    "cost_cap": "The next model call could pass the run's cost cap, so the run stopped.",
    "recording_mismatch": "The recording no longer matches what the curator asks, so it refused.",
    "backend_refused": "The backend refused a move the curator needed.",
    "unexpected_error": "The curator stopped on an unexpected error.",
}
_MODEL_NAME = TypeAdapter(models.ModelName)


@dataclass(frozen=True)
class _Move:
    """A run started, or started again, and what it was before."""

    run_id: str
    attempt: int
    before: RowMapping | None


def _run(connection: Connection, run_id: str) -> RowMapping:
    found = connection.execute(select(db.curator_runs).where(db.curator_runs.c.id == run_id))
    if (row := found.mappings().first()) is None:
        raise Refusal(404, "There is no such curator run.")
    return row


def _working(connection: Connection) -> RowMapping | None:
    found = connection.execute(select(db.curator_runs).where(db.curator_runs.c.state == "working"))
    return found.mappings().first()


def _stalled(run: RowMapping) -> bool:
    return run["state"] == "working" and run["heard_at"] <= triage.stalled_since()


def _not_current(run: RowMapping) -> str:
    return f"That attempt is not the current one; the curator run is {run['state']}."


def _current(run: RowMapping, attempt: int) -> None:
    if run["attempt"] != attempt:
        raise Refusal(409, _not_current(run))


def _refusals(connection: Connection, run_id: str) -> list[str]:
    table = db.curator_steps
    checked = table.c.out_of_place.is_(False)
    return agent_calls.refused_turns(connection, table, [table.c.run_id == run_id], checked)


def _out_of_place(connection: Connection, run: RowMapping, attempt: int, turn: int) -> str | None:
    if run["attempt"] != attempt:
        return _not_current(run)
    if run["state"] != "working":
        return f"The curator run is {run['state']}: it takes one answer."
    refused = len(_refusals(connection, run["id"]))
    if refused >= REFUSALS_BEFORE_FALLBACK or turn != refused + 1:
        return "The curator run is working, and that is not its next turn."
    return None


def _at(run_id: str, attempt: int, turn: int) -> list:
    return [
        db.curator_steps.c.run_id == run_id,
        db.curator_steps.c.attempt == attempt,
        db.curator_steps.c.turn == turn,
    ]


def _to_cite(
    connection: Connection, run: RowMapping, named: set[str]
) -> tuple[list[str], str | None]:
    """The reports to cite, or why one that is named cannot be."""
    cited: list[str] = []
    for reference in sorted(named & run["gap_report_ids"].keys()):
        ids = run["gap_report_ids"][reference]
        found = connection.execute(
            select(db.gap_reports.c.state).where(db.gap_reports.c.id.in_(ids))
        )
        for state in found.scalars():
            if state != "open":
                # The reference is the bundle's own: it was matched before it is said.
                return [], (
                    f"The gap report {reference} is {state} and no longer open, "
                    "so a proposal cannot name it."
                )
        cited.extend(ids)
    return cited, None


class CuratorRuns:
    def __init__(self, journal: Journal, store: Store, curator: Curator, settings: Settings):
        self._journal = journal
        self._store = store
        self._curator = curator
        self._model = settings.curator_model
        self._cases = settings.cases

    # The playbook owner's move

    def _configured(self) -> str:
        if not self._model:
            raise Refusal(503, NO_MODEL)
        try:
            model = _MODEL_NAME.validate_python(self._model)
        except ValidationError:
            raise Refusal(503, BAD_MODEL) from None
        # Refused before any call: the gate would refuse what it proposed, after it was paid for.
        writer = cases.writer_of(self._cases)
        if writer is not None and cases.written_by(model, writer):
            raise Refusal(409, WRITER.format(writer.name))
        return model

    def start(self) -> models.CurationStarted:
        """Assemble, list and hash the bundle, compare it, record it, then ask the curator.

        A run that stalled is started again in its place: the same bundle, a new attempt.
        """
        model, asked_at = self._configured(), triage.now()
        move = self._committed(model, asked_at)
        payload = {
            "kind": "curation",
            "run_id": move.run_id,
            "attempt": move.attempt,
            "model": model,
        }
        undone = UNDONE if move.before is None else AGAIN_UNDONE
        undo = partial(self._undo, move, asked_at)
        asked = commit_then_ask(self._curator, lambda: payload, undo, undone)
        with self._journal.transaction() as connection:
            state = _run(connection, move.run_id)["state"]
        return models.CurationStarted(
            id=move.run_id, attempt=move.attempt, state=state, asked=asked
        )

    def _committed(self, model: str, asked_at: str) -> _Move:
        refused: Refusal | None = None
        with self._journal.transaction() as connection:
            before = _working(connection)
            if before is not None:
                if not _stalled(before):
                    raise Refusal(409, NOT_STALLED)
                move = _Move(before["id"], before["attempt"] + 1, before)
                items = before["items"]
                event, data = (
                    events.CURATION_STARTED_AGAIN,
                    {
                        "run_id": move.run_id,
                        "before": before["attempt"],
                        "attempt": move.attempt,
                        "model": model,
                        "last_heard": before["heard_at"],
                    },
                )
            else:
                move = _Move(uuid.uuid4().hex, 1, None)
                made = bundle.assemble(connection, self._store)
                items = made.items
                event, data = (
                    events.CURATION_STARTED,
                    {
                        "run_id": move.run_id,
                        "attempt": move.attempt,
                        "model": model,
                        "bundle_sha256": made.sha256,
                        "items": items,
                        "gap_report_ids": made.gap_report_ids,
                    },
                )
            try:
                found = bundle.held_out(items, self._cases)
            except bundle.CannotCheck:
                raise Refusal(500, CANNOT_CHECK) from None
            who = OWNER
            if found is not None:
                kind, reference = found["kind"], found["reference"]
                event, who = events.BUNDLE_REFUSED, BACKEND
                data = {"run_id": None, "kind": kind, "reference": reference}
                if before is not None:
                    data |= {"run_id": before["id"], "attempt": before["attempt"]}
                refused = Refusal(
                    500,
                    f"The bundle's {kind} {reference} is a held-out case or a file that "
                    "grades them, so the curator was not asked.",
                )
            self._store.kept.record(connection, event, who, asked_at, data)
        # Raised after the transaction, so that the refusal's line stands.
        if refused is not None:
            raise refused
        return move

    def _undo(self, move: _Move, asked_at: str) -> bool:
        data = {"run_id": move.run_id, "attempt": move.attempt}
        if move.before is None:
            event = events.CURATION_UNDONE
        else:
            event = events.CURATION_RESTART_UNDONE
            data |= {
                "asked_at": asked_at,
                "last_heard": move.before["heard_at"],
                "last_model": move.before["model"],
            }
        try:
            with self._journal.transaction() as connection:
                self._store.kept.record(connection, event, BACKEND, triage.now(), data)
        except CannotApply:
            return False  # the curator was heard from, so there is nothing to undo
        return True

    # The curator's moves

    def work(self, run_id: str, attempt: int) -> models.CuratorWork:
        with self._journal.transaction() as connection:
            run = _run(connection, run_id)
            _current(run, attempt)
            # A bundle is given out for the work it is for, and for no longer.
            items = run["items"] if run["state"] == "working" else []
            return models.CuratorWork(
                state=run["state"],
                spent_usd=agent_calls.total(_steps(connection, run_id)).cost_usd,
                items=[models.BundleItem.model_validate(_given(each)) for each in items],
                refusals=_refusals(connection, run_id),
            )

    def record_proposal(
        self, run_id: str, attempt: int, sent: models.RecordProposal
    ) -> models.ProposalRecorded:
        def judged(connection: Connection, run: RowMapping) -> tuple[str, dict]:
            proposal = sent.proposal
            wrong = proposals.malformed(proposal, self._store.desks)
            if wrong := wrong or proposals.outside(proposal, run["items"]):
                raise Refusal(422, wrong)
            cited, closed = _to_cite(connection, run, proposals.named(proposal))
            if closed:
                raise Refusal(409, closed)
            content = proposal.model_dump(mode="json")
            recorded = {
                "id": uuid.uuid4().hex,
                "content": content,
                "sha256": references.content_sha256(content),
            }
            return events.PROPOSAL_RECORDED, {"proposal": recorded, "cited": cited}

        def shown(connection: Connection) -> models.ProposalRecorded:
            found = proposals.of_run(connection, run_id)
            assert found is not None  # an accepted submission left one
            return models.ProposalRecorded(id=found.id, state=found.state, sha256=found.sha256)

        move = agent_calls.digest("record_proposal", sent)
        key = (run_id, attempt, sent.turn)
        return self._answer(key, move, "submit_proposal", sent.usage, judged, shown)

    def record_no_change(
        self, run_id: str, attempt: int, sent: models.NoChange
    ) -> models.CuratorMoved:
        def judged(_: Connection, __: RowMapping) -> tuple[str, dict]:
            return events.NO_CHANGE_RECORDED, {}

        def shown(connection: Connection) -> models.CuratorMoved:
            return models.CuratorMoved(state=_run(connection, run_id)["state"])

        move = agent_calls.digest("record_no_change", sent)
        return self._answer(
            (run_id, attempt, sent.turn), move, "no_change", sent.usage, judged, shown
        )

    def _answer[T](
        self,
        key: tuple[str, int, int],
        move: str,
        tool: models.CuratorTool,
        usage: models.Usage,
        judged: Callable[[Connection, RowMapping], tuple[str, dict]],
        shown: Callable[[Connection], T],
    ) -> T:
        """One tool call: taken, or refused in one line. Either way it is a step on the run."""
        run_id, attempt, turn = key
        refusal: tuple[int, str] | None = None
        with self._journal.transaction() as connection:
            run = _run(connection, run_id)
            if attempt > run["attempt"]:
                _current(run, attempt)  # an attempt that was never made has no calls to record
            if before := _same_move(connection, key, move):
                _current(run, attempt)
                if before["outcome"] == "refused":
                    raise Refusal(before["refused_status"], before["line"])
                return shown(connection)
            misplaced = _out_of_place(connection, run, attempt, turn)
            event, data = events.CURATION_CALLS, {}
            if misplaced:
                refusal = (OUT_OF_PLACE, misplaced)
            else:
                try:
                    event, data = judged(connection, run)
                except Refusal as refused:
                    refusal = (refused.status, refused.line)
            made = _made(connection, key, move, usage, [tool], refusal, misplaced is not None)
            self._store.kept.record(connection, event, CURATOR, triage.now(), made | data)
            if refusal is None:
                return shown(connection)
        # Raised after the transaction, so that a refused call is still a step on the run.
        raise Refusal(*refusal)

    def record_unusable_reply(
        self, run_id: str, attempt: int, sent: models.UnusableCuratorReply
    ) -> models.Refused:
        key = (run_id, attempt, sent.turn)
        with self._journal.transaction() as connection:
            run = _run(connection, run_id)
            if attempt > run["attempt"]:
                _current(run, attempt)
            move = agent_calls.digest("record_unusable_curator_reply", sent)
            if before := _same_move(connection, key, move):
                _current(run, attempt)
                status, line = before["refused_status"], before["line"]
            else:
                misplaced = _out_of_place(connection, run, attempt, sent.turn)
                if misplaced:
                    status, line = OUT_OF_PLACE, misplaced
                else:
                    status = agent_calls.refusal_status(sent.reason)
                    line = UNUSABLE[sent.reason]
                tools = sent.calls or [None]  # a reply in words is a refused turn too
                refusal = (status, line)
                made = _made(connection, key, move, sent.usage, tools, refusal, bool(misplaced))
                event, at = events.CURATION_CALLS, triage.now()
                self._store.kept.record(connection, event, CURATOR, at, made)
        if status == OUT_OF_PLACE:
            raise Refusal(status, line)
        return models.Refused(refusal=line)

    def fail(self, run_id: str, attempt: int, sent: models.FailCuratorRun) -> models.CuratorMoved:
        with self._journal.transaction() as connection:
            run = _run(connection, run_id)
            _current(run, attempt)
            if (run["state"], run["failure"]) == ("failed", sent.reason):
                return models.CuratorMoved(state="failed")
            if run["state"] != "working":
                raise Refusal(409, f"The curator run is {run['state']}.")
            refused = len(_refusals(connection, run_id))
            if sent.reason == "refused_twice" and refused < REFUSALS_BEFORE_FALLBACK:
                raise Refusal(409, "The curator run is working: it has not been refused twice.")
            moved = {"run_id": run_id, "attempt": attempt, "reason": sent.reason}
            self._store.kept.record(
                connection, events.CURATION_FAILED, CURATOR, triage.now(), moved
            )
        return models.CuratorMoved(state="failed")

    # What people read

    def listed(self) -> list[models.CuratorRun]:
        """Every curator run, in the order the ledger recorded them."""
        with self._journal.transaction() as connection:
            found = connection.execute(
                select(db.curator_runs.c.id).order_by(db.curator_runs.c.line)
            )
            ids = [row.id for row in found]
        return [self.read(run_id) for run_id in ids]

    def read(self, run_id: str) -> models.CuratorRun:
        with self._journal.transaction() as connection:
            run = _run(connection, run_id)
            steps = _steps(connection, run_id)
            failure = FAILURES[run["failure"]] if run["failure"] else None
            if run["failure"] == "refused_twice":
                failure = f"{failure} The last refusal: {_refusals(connection, run_id)[-1]}"
            return models.CuratorRun(
                id=run["id"],
                attempt=run["attempt"],
                state=run["state"],
                stalled=_stalled(run),
                failure=failure,
                model=run["model"],
                bundle=models.Manifest(
                    sha256=run["bundle_sha256"],
                    items=[models.ManifestItem.model_validate(_listed(i)) for i in run["items"]],
                ),
                started_at=run["started_at"],
                finished_at=run["finished_at"],
                proposal=proposals.of_run(connection, run_id),
                steps=[
                    models.CuratorStep.model_validate(
                        {name: each[name] for name in models.CuratorStep.model_fields}
                    )
                    for each in steps
                ],
                usage=agent_calls.total(steps),
            )


def _given(item: dict) -> dict:
    """An item as the curator is given it: no hash, which reads as an identifier."""
    return {name: item[name] for name in models.BundleItem.model_fields}


def _listed(item: dict) -> dict:
    return {name: item[name] for name in models.ManifestItem.model_fields}


def _steps(connection: Connection, run_id: str) -> Sequence[RowMapping]:
    found = connection.execute(
        select(db.curator_steps)
        .where(db.curator_steps.c.run_id == run_id)
        .order_by(*(db.curator_steps.c[name] for name in ("at", "attempt", "turn", "call")))
    )
    return found.mappings().all()


def _same_move(connection: Connection, key: tuple[str, int, int], move: str) -> RowMapping | None:
    found = connection.execute(
        select(db.curator_steps).where(*_at(*key), db.curator_steps.c.move == move)
    )
    return found.mappings().first()


def _made(
    connection: Connection,
    key: tuple[str, int, int],
    move: str,
    usage: models.Usage,
    tools: Sequence[str | None],
    refusal: tuple[int, str] | None,
    out_of_place: bool,
) -> dict:
    run_id, attempt, turn = key
    calls = agent_calls.written(
        connection, db.curator_steps, _at(*key), usage, tools, refusal=refusal
    )
    return {
        "run_id": run_id,
        "attempt": attempt,
        "turn": turn,
        "move": move,
        # Only a call that was the run's to make is hearing from its curator.
        "heard": not out_of_place,
        "calls": [each | {"out_of_place": out_of_place} for each in calls],
    }
