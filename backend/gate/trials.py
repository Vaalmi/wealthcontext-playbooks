"""A held-out trial: one case worked by the triage agent, as a triage run is worked.

The triage agent cannot tell a trial from a triage run. It reads its work and records its
choices at the same addresses, under a run ID the backend made for the trial, and each
choice is checked both ways and refused with the same lines as on a triage run. A trial is
kept here, in memory, while the gate waits on it, and then in the gate run's ledger line.
It is never written to the triage log, it files no gap report, and it is asked no question
about one.
"""

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import NoReturn

from backend import agent_calls, matching, models, triage
from backend.agent_calls import OUT_OF_PLACE, REFUSALS_BEFORE_FALLBACK
from backend.playbook_format import When
from backend.refusals import Refusal
from backend.vocabulary import FeedFacts, WarningCode

NO_GAP_REPORT = "A held-out trial files no gap report, and none is stored."


@dataclass(frozen=True)
class Option:
    """A step a trial's note may name: one of the steps in use once the proposal is made."""

    playbook: str
    step: str
    code: str
    when: When
    tell: str
    by: str
    hold_report: bool
    line: str


@dataclass
class TrialNote:
    id: str
    # The exception's ID in its case.
    exception: str
    code: WarningCode
    facts: FeedFacts | None
    warnings: int
    options: list[Option]
    state: models.NoteState = "pending"
    chosen: Option | None = None
    refusals: list[str] = field(default_factory=list)


@dataclass
class Trial:
    id: str
    case: str
    number: int
    notes: list[TrialNote]
    state: models.RunState = "working"
    # A failure reason the agent gave, or `stalled` when nothing was heard for too long.
    failure: str | None = None
    steps: list[dict] = field(default_factory=list)
    moves: dict[tuple, tuple[int, str | None]] = field(default_factory=dict)
    heard: float = field(default_factory=time.monotonic)
    # Records the trial as it ends, before its end is acknowledged. What it returns is kept.
    on_end: Callable[["Trial"], object] = lambda _: None
    recorded: object = None

    def end(self, state: models.RunState, failure: str | None = None) -> None:
        """End it: recorded first, so an end the agent was told of is never lost."""
        before = (self.state, self.failure)
        self.state, self.failure = state, failure
        try:
            self.recorded = self.on_end(self)
        except BaseException:
            self.state, self.failure = before
            raise

    def cost(self) -> Decimal:
        return sum((Decimal(s["cost_usd"]) for s in self.steps if s["cost_usd"]), Decimal(0))


def _not_current(trial: Trial) -> str:
    return f"That attempt is not the current one; the run is {trial.state}."


class Trials:
    """The trials the gate is waiting on, by run ID. Every operation holds one lock."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._changed = threading.Condition()
        self._trials: dict[str, Trial] = {}
        self._clock = clock

    def holds(self, run_id: str) -> bool:
        with self._changed:
            return run_id in self._trials

    def open(
        self,
        case: str,
        number: int,
        notes: list[TrialNote],
        on_end: Callable[[Trial], object] = lambda _: None,
    ) -> Trial:
        trial = Trial(id=uuid.uuid4().hex, case=case, number=number, notes=notes, on_end=on_end)
        trial.heard = self._clock()
        if not any(note.state == "pending" for note in notes):
            trial.end("completed")  # nothing to choose: no model is asked
        with self._changed:
            self._trials[trial.id] = trial
        return trial

    def close(self, run_id: str) -> Trial:
        with self._changed:
            return self._trials.pop(run_id)

    def wait(self, run_id: str, stalled_after: float, tick: float = 0.05) -> None:
        """Until the trial ends, or nothing has been heard from its agent for too long."""
        with self._changed:
            trial = self._trials[run_id]
            while trial.state == "working":
                if self._clock() - trial.heard > stalled_after:
                    trial.end("failed", "stalled")
                    break
                self._changed.wait(tick)

    # The triage agent's operations, on a trial

    def _trial(self, run_id: str, attempt: int) -> Trial:
        trial = self._trials.get(run_id)
        if trial is None:  # the gate has stopped waiting on it
            raise Refusal(404, "There is no such triage run.")
        if attempt != 1:
            raise Refusal(409, _not_current(trial))
        return trial

    def _note(self, trial: Trial, note_id: str) -> TrialNote:
        found = next((note for note in trial.notes if note.id == note_id), None)
        if found is None:
            raise Refusal(404, "There is no such note on this triage run.")
        return found

    def _heard(self, trial: Trial) -> None:
        trial.heard = self._clock()
        self._changed.notify_all()

    def work(self, run_id: str, attempt: int) -> models.TriageWork:
        with self._changed:
            trial = self._trial(run_id, attempt)
            if trial.state == "working":
                self._heard(trial)
            pending = [
                models.PendingNote(
                    id=note.id,
                    code=note.code,
                    feed=note.facts,
                    warning_count=note.warnings,
                    options=[
                        models.Offered(playbook=o.playbook, step=o.step, when=o.when, line=o.line)
                        for o in note.options
                    ],
                    refusals=list(note.refusals),
                )
                for note in trial.notes
                if note.state == "pending"
            ]
            spent = trial.cost()
            return models.TriageWork(
                state=trial.state,
                spent_usd=f"{spent:.6f}" if any(s["cost_usd"] for s in trial.steps) else None,
                notes=pending,
                uncovered=[],
            )

    def _misplaced(self, trial: Trial, note: TrialNote, turn: int) -> str | None:
        if trial.state != "working" or note.state != "pending":
            return f"The run is {trial.state} and the note is {note.state}."
        refused = len(note.refusals)
        if refused >= REFUSALS_BEFORE_FALLBACK or turn != refused + 1:
            return triage.NOT_ITS_TURN
        return None

    def _step(
        self,
        trial: Trial,
        note: TrialNote,
        turn: int,
        usage: models.Usage,
        tools: list[str | None],
        option: str | None,
        refusal: tuple[int, str] | None,
    ) -> None:
        for number, tool in enumerate(tools, start=1):
            counted = number == 1  # a reply's usage is counted once
            trial.steps.append(
                {
                    "exception": note.exception,
                    "turn": turn,
                    "tool": tool,
                    "option": option,
                    "outcome": "refused" if refusal else "accepted",
                    "line": refusal[1] if refusal else None,
                    "model": usage.model,
                    "input_tokens": usage.input_tokens if counted else None,
                    "output_tokens": usage.output_tokens if counted else None,
                    "cost_usd": usage.cost_usd if counted else None,
                }
            )

    def record_choice(
        self, run_id: str, attempt: int, note_id: str, sent: models.RecordChoice
    ) -> models.Moved:
        with self._changed:
            trial = self._trial(run_id, attempt)
            note = self._note(trial, note_id)
            key = (note_id, sent.turn, agent_calls.digest("record_choice", sent))
            if (before := trial.moves.get(key)) is not None:
                status, line = before
                if line is not None:
                    raise Refusal(status, line)
                return models.Moved(state=note.state)
            named: Option | None = None
            wrong: str | None = None
            if isinstance(sent.choice, models.StepChoice):
                tool = "choose_step"
                wanted = (sent.choice.playbook, sent.choice.step)
                named = next((o for o in note.options if (o.playbook, o.step) == wanted), None)
                if named is None:
                    wrong = triage.NOT_IN_USE
                elif why := matching.misfit_of(named.code, named.when, note.code, note.facts):
                    wrong = triage.DOES_NOT_FIT + why
            else:
                tool = "no_step_applies"
                if any(
                    matching.misfit_of(o.code, o.when, note.code, note.facts) is None
                    for o in note.options
                ):
                    wrong = triage.A_STEP_APPLIES
            refusal: tuple[int, str] | None = None
            if misplaced := self._misplaced(trial, note, sent.turn):
                refusal = (OUT_OF_PLACE, misplaced)
            elif wrong:
                refusal = (422, wrong)
                note.refusals.append(wrong)
            else:
                note.state, note.chosen = ("chosen", named) if named else ("no_step", None)
            shown = f"{named.playbook}/{named.step}" if named and not refusal else None
            self._step(trial, note, sent.turn, sent.usage, [tool], shown, refusal)
            trial.moves[key] = refusal if refusal else (200, None)
            if not misplaced:
                self._heard(trial)
        if refusal is not None:
            raise Refusal(*refusal)
        return models.Moved(state=note.state)

    def record_unusable_reply(
        self, run_id: str, attempt: int, note_id: str, sent: models.UnusableReply
    ) -> models.Refused:
        with self._changed:
            trial = self._trial(run_id, attempt)
            note = self._note(trial, note_id)
            key = (note_id, sent.turn, agent_calls.digest("record_unusable_reply", sent))
            if (before := trial.moves.get(key)) is None:
                if misplaced := self._misplaced(trial, note, sent.turn):
                    before = (OUT_OF_PLACE, misplaced)
                else:
                    before = (
                        agent_calls.refusal_status(sent.reason),
                        triage.UNUSABLE[sent.reason],
                    )
                    # A call with no answer is charged, and is no refused answer.
                    if sent.reason != "no_answer":
                        note.refusals.append(before[1])
                    self._heard(trial)
                calls: list[str | None] = list(sent.calls) or [None]
                self._step(trial, note, sent.turn, sent.usage, calls, None, before)
                trial.moves[key] = before
        status, line = before
        assert line is not None
        if status == OUT_OF_PLACE:
            raise Refusal(status, line)
        return models.Refused(refusal=line)

    def record_fallback(self, run_id: str, attempt: int, note_id: str) -> models.Moved:
        with self._changed:
            trial = self._trial(run_id, attempt)
            note = self._note(trial, note_id)
            if note.state == "unverified":
                return models.Moved(state="unverified")
            if trial.state != "working" or note.state != "pending":
                raise Refusal(409, f"The run is {trial.state} and the note is {note.state}.")
            if len(note.refusals) < REFUSALS_BEFORE_FALLBACK:
                raise Refusal(409, "The note is pending: fallback follows a second refusal only.")
            note.state = "unverified"
            self._heard(trial)
        return models.Moved(state="unverified")

    def complete(self, run_id: str, attempt: int) -> models.Moved:
        with self._changed:
            trial = self._trial(run_id, attempt)
            if trial.state == "completed":
                return models.Moved(state="completed")
            if trial.state != "working":
                raise Refusal(409, f"The run is {trial.state}.")
            if any(note.state == "pending" for note in trial.notes):
                raise Refusal(409, "The run is working and a note is still pending.")
            trial.end("completed")
            self._heard(trial)
        return models.Moved(state="completed")

    def fail(self, run_id: str, attempt: int, sent: models.FailTriage) -> models.Moved:
        with self._changed:
            trial = self._trial(run_id, attempt)
            if (trial.state, trial.failure) == ("failed", sent.reason):
                return models.Moved(state="failed")
            if trial.state != "working":
                raise Refusal(409, f"The run is {trial.state}.")
            trial.end("failed", sent.reason)
            self._heard(trial)
        return models.Moved(state="failed")

    def refuse_gap_question(
        self,
        run_id: str,
        attempt: int,
        note_id: str,
        tool: str,
        turn: int,
        usage: models.Usage,
    ) -> NoReturn:
        """A gap report, or its answer, on a trial: refused, and only the call is kept."""
        with self._changed:
            trial = self._trial(run_id, attempt)
            note = self._note(trial, note_id)
            self._step(trial, note, turn, usage, [tool], None, (409, NO_GAP_REPORT))
        raise Refusal(409, NO_GAP_REPORT)
