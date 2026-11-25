"""The held-out check: the cases graded by fixed rules, and each run three times on the
triage agent with the proposal in use.

Its fixed part works out each case's notes from the steps alone, with the playbooks in use
and with the proposal made. A regression case must give its expected notes both ways, and
a new-rule case once the proposal is made. Its live part asks the triage agent to triage
each case three times, as if the proposal were in use, and needs every trial to give the
expected notes. Grading is by fixed rules: no model judges a note.

A failure names the case, the exception and the fields that differ, and never the
expected note itself.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from backend import cases
from backend.asking import Agent
from backend.cases import Case, CaseSet, Graded, Rule
from backend.gate.effect import Placed
from backend.gate.trials import Option, Trial, TrialNote, Trials
from backend.models import Failure

TRIALS_PER_CASE = 3
# A trial with nothing heard from its agent for this long ends, as a stalled triage run would.
STALLED_SECONDS = 300.0
KIND_NAME = {"regression": "Regression case", "new_rule": "New-rule case"}


class AgentDidNotStart(Exception):
    """The triage agent definitely did not start a trial. The line says why, `reason` is the
    agent's own refusal when it gave one, and `trials` holds those that ran before it."""

    def __init__(self, line: str, trials: Sequence[dict] = (), reason: str | None = None):
        super().__init__(line)
        self.trials = list(trials)
        self.reason = reason


def rules(steps: Sequence[Placed]) -> list[Rule]:
    return [
        Rule(
            playbook=each.playbook,
            code=each.code,
            when=each.when,
            tell=each.action[0],
            by=each.action[1],
            hold_report=each.action[2],
        )
        for each in steps
    ]


def _differs(case: Case, misses: list[tuple[str, list[str]]], where: str) -> list[Failure]:
    return [
        Failure(
            change=None,
            step=None,
            rule=f"{case.kind}_case",
            line=(
                f"{KIND_NAME[case.kind]} {case.id}, exception {exception}: "
                f"{', '.join(fields)} {'differs' if len(fields) == 1 else 'differ'} "
                f"from its expected note {where}."
            ),
        )
        for exception, fields in misses
    ]


@dataclass(frozen=True)
class Fixed:
    failures: list[Failure]
    # For each case: whether it gives its expected notes with the playbooks in use, and once
    # the proposal is made. A new-rule case is expected to fail the first.
    graded: list[dict]


def fixed(case_set: CaseSet, before: Sequence[Placed], after: Sequence[Placed]) -> Fixed:
    failures, graded = [], []
    now, then = rules(before), rules(after)
    for case in case_set.cases:
        missed_now = cases.misses(case, cases.by_fixed_rules(case, now))
        missed_then = cases.misses(case, cases.by_fixed_rules(case, then))
        if case.kind == "regression":
            failures += _differs(case, missed_now, "with the playbooks in use")
        failures += _differs(case, missed_then, "once the proposal is made")
        graded.append(
            {
                "case": case.id,
                "kind": case.kind,
                "in_use": "passed" if not missed_now else "failed",
                "proposed": "passed" if not missed_then else "failed",
            }
        )
    return Fixed(failures=failures, graded=graded)


def _notes(case: Case, after: Sequence[Placed]) -> list[TrialNote]:
    made = []
    for exception_id, exception in zip(case.ids, case.exceptions, strict=True):
        options = [
            Option(
                playbook=each.playbook,
                step=each.step,
                code=each.code,
                when=each.when,
                tell=each.action[0],
                by=each.action[1],
                hold_report=each.action[2],
                line=each.action[3],
            )
            for each in after
            if each.code == exception.code
        ]
        made.append(
            TrialNote(
                id=f"{exception_id}-{len(made)}",
                exception=exception_id,
                code=exception.code,
                facts=exception.facts,
                warnings=len(exception.subjects),
                options=options,
                # With no step for its code there is nothing to choose, as on a triage run.
                state="pending" if options else "no_step",
            )
        )
    return made


def given(trial: Trial) -> dict[str, Graded]:
    notes = {}
    for note in trial.notes:
        if note.state == "chosen" and note.chosen is not None:
            o = note.chosen
            notes[note.exception] = Graded("step", o.playbook, o.tell, o.by, o.hold_report)
        elif note.state == "no_step":
            notes[note.exception] = Graded("no_step")
        else:
            notes[note.exception] = Graded("unverified")
    return notes


def recorded(case: Case, trial: Trial, misses: list[tuple[str, list[str]]]) -> dict:
    """A trial as the gate run's line keeps it: what it gave, what it missed, every call."""
    return {
        "case": case.id,
        "kind": case.kind,
        "trial": trial.number,
        "state": trial.state,
        "failure": trial.failure,
        "notes": [
            {
                "exception": note.exception,
                "state": note.state,
                "step": f"{note.chosen.playbook}/{note.chosen.step}" if note.chosen else None,
            }
            for note in trial.notes
        ],
        "misses": [{"exception": e, "fields": fields} for e, fields in misses],
        "steps": trial.steps,
        "cost_usd": f"{trial.cost():.6f}",
    }


@dataclass(frozen=True)
class Tried:
    trials: list[dict]
    failures: list[Failure]
    models: list[str]


def failures_of(trial: dict) -> list[Failure]:
    """What a recorded trial failed, from its record alone, so a trial recorded before a gate
    stopped fails the next one just the same."""
    named = (
        f"{KIND_NAME[trial['kind']]} {trial['case']}, trial {trial['trial']} of {TRIALS_PER_CASE}"
    )
    if trial["state"] != "completed":
        why = "nothing was heard from it" if trial["failure"] == "stalled" else trial["failure"]
        line = f"{named}: it ended {trial['state']} ({why}), so it gave no notes to grade."
        return [Failure(change=None, step=None, rule="trial", line=line)]
    return [
        Failure(
            change=None,
            step=None,
            rule="trial",
            line=(
                f"{named}, exception {miss['exception']}: {', '.join(miss['fields'])} "
                f"{'differs' if len(miss['fields']) == 1 else 'differ'} from its expected note."
            ),
        )
        for miss in trial["misses"]
    ]


def models_of(trials: Sequence[dict]) -> list[str]:
    return sorted({step["model"] for trial in trials for step in trial["steps"]})


def _cost_cap(line: str) -> Failure:
    return Failure(change=None, step=None, rule="cost_cap", line=line)


class HeldOut:
    def __init__(
        self,
        trials: Trials,
        agent: Agent,
        cap: Decimal,
        stalled_after: Callable[[], float] = lambda: STALLED_SECONDS,
    ):
        self._trials = trials
        self._agent = agent
        self._cap = cap
        self._stalled_after = stalled_after

    def _one(
        self,
        case: Case,
        number: int,
        after: Sequence[Placed],
        model: str,
        left: Decimal,
        kept: Callable[[dict], None],
    ) -> Trial:
        def end(trial: Trial) -> dict:
            """The trial as recorded, written before the agent hears that it ended."""
            ended = trial.state == "completed"
            made = recorded(case, trial, cases.misses(case, given(trial)) if ended else [])
            kept(made)
            return made

        trial = self._trials.open(case.id, number, _notes(case, after), end)
        try:
            if trial.state == "working":
                # The agent works under the model the gate records, or refuses before any call,
                # and spends at most what is left of the gate's cap.
                asked, line, reason = self._agent.ask_why(
                    {
                        "kind": "trial",
                        "run_id": trial.id,
                        "attempt": 1,
                        "model": model,
                        "cap_usd": f"{left:.6f}",
                    }
                )
                if asked == "not_started":
                    raise AgentDidNotStart(line or "The triage agent did not start.", (), reason)
                # Unknown: the agent may have the work. If it never does, the trial stalls.
                self._trials.wait(trial.id, self._stalled_after())
        finally:
            closed = self._trials.close(trial.id)
        return closed

    def run(
        self,
        case_set: CaseSet,
        after: Sequence[Placed],
        model: str,
        kept: Callable[[dict], None],
    ) -> Tried:
        """Every case, three times, in order. `kept` records each trial as it ends.

        Each trial may spend what is left of the cap, and no trial starts with nothing left.
        """
        done: list[dict] = []
        failures: list[Failure] = []
        spent = Decimal(0)
        for case in case_set.cases:
            for number in range(1, TRIALS_PER_CASE + 1):
                if spent >= self._cap:
                    failures.append(
                        _cost_cap(
                            f"The trials stopped before case {case.id}, trial {number}: nothing "
                            f"is left of the gate's cap of ${self._cap}."
                        )
                    )
                    return Tried(done, failures, models_of(done))
                try:
                    trial = self._one(case, number, after, model, self._cap - spent, kept)
                except AgentDidNotStart as stopped:
                    raise AgentDidNotStart(str(stopped), done, stopped.reason) from None
                spent += trial.cost()
                assert isinstance(trial.recorded, dict)
                made = trial.recorded
                done.append(made)
                failures += failures_of(made)
                if trial.failure == "cost_cap":
                    # The agent stopped before a call could pass what was left: so would the next.
                    failures.append(
                        _cost_cap(
                            f"The trials stopped at case {case.id}, trial {number}: its next model "
                            f"call could pass what was left of the gate's cap of ${self._cap}."
                        )
                    )
                    return Tried(done, failures, models_of(done))
                if spent > self._cap:
                    # Only an agent that ignored the cap it was given gets here.
                    failures.append(
                        _cost_cap(
                            f"The trials passed the gate's cap of ${self._cap} at case {case.id}, "
                            f"trial {number}, and stopped."
                        )
                    )
                    return Tried(done, failures, models_of(done))
        return Tried(done, failures, models_of(done))
