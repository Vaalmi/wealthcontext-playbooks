"""The recorded cycle: the first cycle's moves by the firm's people, in order, through the
same operations anyone uses, and the six steps the page shows of it.

The people are the synthetic firm's. The analyst triages and corrects. The playbook owner
takes the memos in, runs the curator and the gate, and rules as the firm's first cycle
describes: for the correction and the gap report over the founding memo. Where the sources
leave a ruling open, for the later memo. The agents answer on whatever model is set: on a
replay, the cycle needs no key.

It runs once, on a layer with nothing in its records, and only where the layer is started
for it. Every move is recorded as the move a person makes would be, so a stopped cycle
leaves a record of how far it got and the page says why it stopped.
"""

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from sqlalchemy import select

from backend import clock, models, record
from backend import database as db
from backend import ledger_events as events
from backend.approval import Approval
from backend.corrections import Corrections
from backend.curator_runs import CuratorRuns
from backend.gap_reports import GapReports
from backend.gate.run import Gate
from backend.journal import Journal
from backend.proposal_view import ProposalView
from backend.refusals import Refusal
from backend.sources import Sources
from backend.store import FOUNDED, FOUNDING_APPROVED, Store
from backend.triage import Triage

log = logging.getLogger(__name__)

DATES = ("2026-10-15", "2026-10-09")
CUTOFF = "2026-10-31T16:00:00Z"
FOUNDING = ("feed-outage-founding", "missing-cross-check-founding")
CHANGE = "custodian-b-feed-escalation"
POISONED = "stale-price-poisoned"
CORRECTION = "cross-check-alone-is-no-hold"
# A hostile proposal that asks for what the poisoned memo asks for, written for the bundle the
# cycle has before the curator runs.
HOSTILE = "guardrails-old-price-carried-forward"
OWNER = "playbook_owner"
# Each line of the cycle's own: where it has got, and what each step made. The page shows only
# what these lines name, so a run or a check made by hand is never taken for a step of it.
CYCLE_STEP = events.RECORDED_CYCLE
WHO = "recorded_cycle"
STOPPED_BY_A_RESTART = (
    "The backend stopped while the cycle ran. The steps it had ended are recorded."
)
# How long a move's agent may take before the cycle stops and says so.
WAIT_SECONDS = 900.0
TICK_SECONDS = 0.25
STEPS = {
    1: "The failed feed and the missing cross-check, triaged under the first playbooks",
    2: "What arrived since: the memo, the person's correction, and the agent's gap report",
    3: "The curator's proposal: the changed steps, each next to the source it rests on",
    4: "The gate: each check and its result, then the approval and who gave it",
    5: "Both dates triaged again under the second playbooks",
    6: "The poisoned memo and a hostile proposal: what each asked for, and what stopped it",
}

State = Literal["not_run", "running", "done", "stopped"]


class Stopped(Exception):
    """A move did not end as the cycle needs. The line says which, and why."""


@dataclass(frozen=True)
class Files:
    """What the people hand in, as committed."""

    founding: tuple[str, ...]
    change: str
    poisoned: str
    correction: dict[str, Any]
    hostile: dict[str, Any]

    @classmethod
    def read(cls, sources: Path, cases: Path) -> "Files":
        def memo(name: str) -> str:
            return (sources / "memos" / f"{name}.md").read_bytes().decode()

        correction = sources / "corrections" / f"{CORRECTION}.yaml"
        hostile = cases / "hostile" / f"{HOSTILE}.yaml"
        return cls(
            founding=tuple(memo(name) for name in FOUNDING),
            change=memo(CHANGE),
            poisoned=memo(POISONED),
            correction=yaml.safe_load(correction.read_text(encoding="utf-8")),
            hostile=yaml.safe_load(hostile.read_text(encoding="utf-8")),
        )


@dataclass(frozen=True)
class Operations:
    journal: Journal
    triage: Triage
    sources: Sources
    corrections: Corrections
    reports: GapReports
    curation: CuratorRuns
    gate: Gate
    approval: Approval
    view: ProposalView
    store: Store


class Cycle:
    def __init__(
        self,
        operations: Operations,
        files: Callable[[], Files],
        wait: float = WAIT_SECONDS,
    ):
        self._do = operations
        self._files = files
        self._wait = wait
        self._lock = threading.Lock()
        # Whether a thread of this backend plays it now, the step it plays, and what that step
        # has made so far. Everything else is in the ledger's lines.
        self._playing = False
        self._step = 0
        self._made: dict[str, list[str]] = {}

    def _lines(self) -> list[tuple[str, dict[str, Any]]]:
        with self._do.journal.transaction() as connection:
            found = connection.execute(
                select(db.ledger_entries.c.at, db.ledger_entries.c.data)
                .where(db.ledger_entries.c.event == CYCLE_STEP)
                .order_by(db.ledger_entries.c.line)
            )
            return [(row.at, row.data) for row in found]

    def status(self) -> models.CycleState:
        with self._lock:
            playing = self._playing
        lines = self._lines()
        if not lines:
            return models.CycleState(
                state="not_run",
                step=0,
                line=None,
                started_at=None,
                finished_at=None,
                offered=not playing and self._fresh(),
            )
        at, last = lines[-1]
        state, step, line, finished = last["state"], last["step"], last["line"], at
        if state == "running":
            step, finished = step + 1, None
            if not playing:
                state, line, finished = "stopped", STOPPED_BY_A_RESTART, at
        return models.CycleState(
            state=state,
            step=step,
            line=line,
            started_at=lines[0][0],
            finished_at=finished,
            offered=False,
        )

    def made(self) -> dict[int, dict[str, list[str]]]:
        """What each step made, as its lines name it, and what the step played now has so far."""
        steps: dict[int, dict[str, list[str]]] = {}
        for _, data in self._lines():
            for kind, ids in data["made"].items():
                steps.setdefault(data["step"] or 1, {}).setdefault(kind, []).extend(ids)
        with self._lock:
            if self._playing:
                for kind, ids in self._made.items():
                    steps.setdefault(self._step, {}).setdefault(kind, []).extend(ids)
        return steps

    def _fresh(self) -> bool:
        """Nothing in the records but the founding and its approval: the cycle starts there."""
        if self._do.triage.latest(1):
            return False
        founded = {FOUNDED, FOUNDING_APPROVED}
        return all(each.event in founded for each in record.read(self._do.journal).entries)

    def start(self) -> models.CycleState:
        with self._lock:
            if self._playing or self._lines():
                raise Refusal(409, "The recorded cycle has run on this layer, and runs once.")
            if not self._fresh():
                raise Refusal(
                    409,
                    "The recorded cycle runs once, on a layer with nothing in its records yet. "
                    "Start the layer again on fresh volumes.",
                )
            try:
                files = self._files()
            except (OSError, ValueError, KeyError, IndexError):
                raise Refusal(
                    409, "The firm's files for the recorded cycle are not here."
                ) from None
            self._playing, self._step, self._made = True, 1, {}
            try:
                self._write(0, "running", None, {})
            except BaseException:
                self._playing = False
                raise
        threading.Thread(target=self._play, args=(files,), daemon=True).start()
        return self.status()

    def _write(self, step: int, state: str, line: str | None, made: dict) -> None:
        data = {"step": step, "state": state, "line": line, "made": made}
        with self._do.journal.transaction() as connection:
            at = clock.stamp(clock.now())
            self._do.store.kept.record(connection, CYCLE_STEP, WHO, at, data)

    def _keep(self, kind: str, *ids: str) -> None:
        with self._lock:
            self._made.setdefault(kind, []).extend(ids)

    def _ended(self, step: int, state: str = "running", line: str | None = None) -> None:
        """The step's line, with what it made. The next step is played from here."""
        with self._lock:
            made, self._made = self._made, {}
        self._write(step, state, line, made)
        with self._lock:
            self._step = step + 1

    def _at(self, step: int) -> None:
        self._ended(step - 1)

    def _play(self, files: Files) -> None:
        try:
            self._moves(files)
        except Stopped as stopped:
            self._stop(str(stopped))
        except Refusal as refused:
            self._stop(f"A move was refused ({refused.status}): {refused.line}")
        except Exception:
            log.exception("the recorded cycle stopped")
            self._stop("A move failed in the backend. Its log says which.")
        else:
            self._end(lambda: self._ended(6, "done"))

    def _stop(self, line: str) -> None:
        with self._lock:
            step = self._step
        self._end(lambda: self._ended(step, "stopped", line))

    def _end(self, write: Callable[[], None]) -> None:
        try:
            write()
        except Exception:
            log.exception("the recorded cycle's end could not be recorded")
        finally:
            with self._lock:
                self._playing = False

    def _moves(self, files: Files) -> None:
        do = self._do
        # 1. The firm's founding memos, and both dates under the first playbooks.
        for content in files.founding:
            self._keep("sources", do.sources.intake(models.Intake(content=content)).id)
        cross_check = None
        for day in DATES:
            cross_check = self._triaged(day)
        assert cross_check is not None

        # 2. The memo that changes the rule, the analyst's correction and its review.
        self._at(2)
        self._keep("sources", do.sources.intake(models.Intake(content=files.change)).id)
        self._corrected(cross_check, files.correction)
        # The hostile proposal is checked now, on the bundle it is written for.
        hostile = {name: files.hostile[name] for name in ("changes", "disagreements")}
        self._keep("checked", do.gate.check(models.Proposed.model_validate(hostile), OWNER).id)

        # 3. The curator's proposal.
        self._at(3)
        proposal = self._curated()
        if proposal is None:
            raise Stopped("The curator proposed no change, so there is nothing to approve.")

        # 4. The gate, then the playbook owner's approval with a ruling on each disagreement.
        self._at(4)
        gated = do.gate.run(proposal.id, OWNER)
        self._keep("gate_runs", gated.id)
        if gated.outcome != "passed":
            raise Stopped(f"The gate did not pass the proposal: it is {gated.outcome}.")
        shown = do.view.read(proposal.id)
        do.approval.approve(
            proposal.id,
            models.Approve(sha256=shown.sha256, gate_run=gated.id, rulings=self._rulings(shown)),
        )
        self._keep("decided", proposal.id)

        # 5. Both dates under the second playbooks.
        self._at(5)
        for day in DATES:
            self._triaged(day)

        # 6. The poisoned memo, and what the curator and the gate make of it.
        self._at(6)
        self._keep("sources", do.sources.intake(models.Intake(content=files.poisoned)).id)
        poisoned = self._curated()
        if poisoned is not None:
            gated = do.gate.run(poisoned.id, OWNER)
            self._keep("gate_runs", gated.id)
            if gated.outcome == "passed":
                # The playbook owner does not approve a change from a source nobody trusts.
                do.approval.refuse(poisoned.id, models.Refuse(reason="source_not_trusted"))
                self._keep("decided", poisoned.id)

    def _until(self, read: Callable[[], Any], ended: Callable[[Any], bool], what: str) -> Any:
        until = time.monotonic() + self._wait
        while True:
            found = read()
            if ended(found):
                return found
            if getattr(found, "stalled", False) or time.monotonic() >= until:
                raise Stopped(f"{what} did not end: nothing more was heard from its agent.")
            time.sleep(TICK_SECONDS)

    def _triaged(self, day: str) -> models.TriageRun:
        started = self._do.triage.start(
            models.StartTriage.model_validate({"date": day, "cutoff": CUTOFF})
        )
        self._keep("triage_runs", started.id)
        run = self._until(
            lambda: self._do.triage.read(started.id),
            lambda run: run.state != "working",
            f"The triage of {day}",
        )
        if run.state != "completed":
            raise Stopped(f"The triage of {day} ended {run.state}: {run.failure}")
        return run

    def _corrected(self, run: models.TriageRun, told: dict[str, Any]) -> None:
        chosen = [note for note in run.notes if note.state == "chosen"]
        if len(chosen) != 1:
            raise Stopped("The missing cross-check has no one note with a step to correct.")
        change = {
            "field": told["correction"]["field"],
            "should_be": told["correction"]["should_be"],
        }
        recorded = self._do.corrections.record(
            run.id,
            chosen[0].id,
            models.RecordCorrection.model_validate({"change": change, "reason": told["text"]}),
        )
        self._keep("corrections", recorded.id)
        if recorded.source_id != told["id"]:
            raise Stopped("The correction recorded is not the committed one.")
        reviewed = self._until(
            lambda: self._do.corrections.read(recorded.id),
            lambda found: found.state == "reviewed" or found.stopped is not None,
            "The correction's review",
        )
        # By its ID: reports filed on other traces can share its content's reference.
        filed = self._do.reports.listed().gap_reports
        self._keep("gap_reports", *[r.id for r in filed if r.correction_id == recorded.id])
        if reviewed.state != "reviewed":
            raise Stopped(f"The correction's review stopped: {reviewed.stopped}")

    def _curated(self) -> models.Proposal | None:
        started = self._do.curation.start()
        self._keep("curator_runs", started.id)
        run = self._until(
            lambda: self._do.curation.read(started.id),
            lambda run: run.state != "working",
            "The curator run",
        )
        if run.proposal:
            self._keep("proposals", run.proposal.id)
        if run.state not in ("completed", "completed_no_change"):
            raise Stopped(f"The curator run ended {run.state}: {run.failure}")
        return run.proposal

    def _rulings(self, shown: models.ProposalShown) -> list[models.RuleFor]:
        """As the firm's first cycle is ruled: a correction or a gap report over a memo; between
        memos alone, the later one."""
        dated = {source.id: source.date for source in self._do.sources.listed().sources}
        made = []
        for each in shown.to_rule_on:
            others = [source for source in each.between if not source.startswith("memo-")]
            if others:
                winner = others[0]
            else:
                winner = max(each.between, key=lambda memo: (dated.get(memo, ""), memo))
            made.append(models.RuleFor(change=each.change, between=each.between, ruled_for=winner))
        return made


def configured(setting: str) -> str:
    """A model's setting as the page names it before any reply: a replay with its recording's
    origin, scripted or recorded, when the file is here."""
    provider, _, named = setting.partition(":")
    if provider != "replay":
        return setting
    try:
        origin = json.loads(Path(named).read_bytes())["origin"]
    except (OSError, ValueError, KeyError, TypeError):
        return setting
    return f"{setting} ({origin})"


def captured(models_set: list[str]) -> dict[str, str]:
    """For each replay of a recording captured from a model, what the recording says of itself
    (which model, on which day), under each name the page shows it by."""
    found = {}
    for setting in models_set:
        provider, _, named = setting.partition(":")
        if provider != "replay":
            continue
        try:
            kept = json.loads(Path(named).read_bytes())
            origin, about = kept["origin"], kept["about"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if origin == "recorded" and isinstance(about, str):
            for name in (f"{setting} (recorded)", f"replay:{Path(named).stem} (recorded)"):
                found[name] = about
    return found


def evidence(
    do: Operations, made: dict[int, dict[str, list[str]]], models_set: list[str]
) -> dict[str, Any]:
    """Each of the six steps, from what the cycle's own lines say each step made, and nothing
    else. Before any reply, the models are named as they are set."""

    def ids(step: int, kind: str) -> list[str]:
        return made.get(step, {}).get(kind, [])

    def runs(step: int) -> list[models.TriageRun]:
        return [do.triage.read(each) for each in ids(step, "triage_runs")]

    first, again = runs(1), runs(5)
    sources = {source.id: source for source in do.sources.listed().sources}
    reports = {report.id: report for report in do.reports.listed().gap_reports}
    listed = record.read(do.journal)
    proposals = {proposal.id: proposal for proposal in listed.proposals}

    def decided(proposed: list[str], step: int) -> list[dict[str, Any]]:
        """Each proposal, with the gate runs and the decision of the step that judged it."""
        shown = []
        for each in proposed:
            proposal = proposals[each]
            shown.append(
                {
                    "proposal": proposal,
                    "gate_runs": [g for g in proposal.gate_runs if g.id in ids(step, "gate_runs")],
                    "decision": proposal.decision if each in ids(step, "decided") else None,
                }
            )
        return shown

    curator_runs = {
        each: do.curation.read(each) for step in (3, 6) for each in ids(step, "curator_runs")
    }
    lines = {}
    for entry in listed.entries:
        checked = entry.data.get("proposal") if entry.event == "proposal_checked" else None
        if isinstance(checked, dict):
            lines[str(checked.get("id"))] = entry.line
    used = [m for run in first + again for m in run.usage.models]
    used += [step.model for run in curator_runs.values() for step in run.steps]
    proposal_ids = ids(3, "proposals")
    return {
        "models": sorted(set(used)) or [configured(each) for each in models_set],
        "captured": captured(models_set),
        "first": first,
        "change": [sources[each] for each in ids(2, "sources") if each in sources],
        "corrections": [do.corrections.read(each) for each in ids(2, "corrections")],
        "reports": [reports[each] for each in ids(2, "gap_reports") if each in reports],
        "curator_run": next((curator_runs[e] for e in ids(3, "curator_runs")), None),
        "proposal": next(iter(decided(proposal_ids[:1], 4)), None),
        "shown": do.view.read(proposal_ids[0]) if proposal_ids else None,
        "again": again,
        "poisoned": [sources[each] for each in ids(6, "sources") if each in sources],
        "poisoned_runs": [curator_runs[each] for each in ids(6, "curator_runs")],
        "poisoned_proposals": decided(ids(6, "proposals"), 6),
        "checked": [proposals[each] for each in ids(2, "checked") if each in proposals],
        "checked_lines": lines,
    }
