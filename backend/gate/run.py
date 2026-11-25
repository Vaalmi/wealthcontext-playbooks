"""Runs the checks on a proposal and records every result, as one ledger line.

The gate runs on a curator's drafted proposal, against the bundle that curator was given:
the fixed-rule checks, then the held-out cases, by fixed rules and three times each on the
triage agent. It passes only when every check passes. A proposal handed in to be checked
gets the fixed-rule checks only, against the bundle as it would be built at that moment,
and is final: it can never be approved. Neither writes a playbook file, so a failed gate
leaves the approved playbooks exactly as they were.

A gate that cannot run (no model for its trials, a model that wrote the held-out cases,
cases that cannot be read, an agent that does not start) records one ledger line saying
why, and the proposal stays drafted.
"""

import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import PurePosixPath

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Connection, RowMapping, select

from backend import bundle, cases, gap_reports, models, personal_data, proposals, references, triage
from backend import database as db
from backend import ledger_events as events
from backend.asking import Agent
from backend.gate import base, effect, guardrails, held_out, overlap, personal, support
from backend.gate.effect import TEXT_LENGTH, Given
from backend.gate.trials import Trials
from backend.journal import Journal
from backend.refusals import Refusal
from backend.settings import Settings
from backend.store import Store

_MODEL_NAME = TypeAdapter(models.ModelName)
REFUSED = {
    "cases_unreadable": "The held-out cases cannot be read ({}), so the gate did not run.",
    "no_trial_model": "No model is configured for the held-out trials: set TRIAGE_MODEL. "
    "The gate did not run.",
    "trial_model_is_writer": "The model for the held-out trials is {}, which wrote the cases' "
    "expected notes. A test is never run by the model that wrote its answers.",
    "curator_model_is_writer": "The proposal's curator ran on {}, which wrote the cases' "
    "expected notes. A test is never run by the model that wrote its answers.",
    "agent_did_not_start": "{} The gate did not finish, and records no result.",
    "agent_model_differs": "The triage agent is configured with another model than TRIAGE_MODEL "
    "names, so no trial ran and the gate records no result.",
    "curator_answered_as_another": "A call of the proposal's curator run named another model "
    "than the one the run recorded, so the gate does not run.",
    "trial_model_unusable": "TRIAGE_MODEL names no model this layer can record, so the gate "
    "did not run.",
    "other_model_answered": "A held-out trial was answered by another model than the one "
    "configured for the trials, so the gate records no result.",
    "playbooks_changed": "The playbooks in use changed while the gate ran, so it records no "
    "result. Run it again.",
}
STATUS = {
    "cases_unreadable": 503,
    "trial_model_unusable": 503,
    "no_trial_model": 503,
    "agent_did_not_start": 503,
}


class GateRefused(Exception):
    """The gate cannot run, or cannot record what it ran. Its line is the backend's own."""

    def __init__(self, reason: str, *shown: str):
        self.reason = reason
        self.line = REFUSED[reason].format(*shown)
        self.status = STATUS.get(reason, 409)


def _result(check: models.CheckName, failures: list[models.Failure]) -> models.CheckResult:
    return models.CheckResult(
        check=check, outcome="failed" if failures else "passed", failures=failures
    )


def judged(given: Given) -> tuple[list[models.CheckResult], support.Supported]:
    """Every fixed-rule check's result, each run whatever another found, and what support found."""
    supported = support.check(given)
    results = [
        _result("support", supported.failures),
        _result("personal", personal.check(given)),
        _result("guardrails", guardrails.check(given)),
        _result("base", base.check(given)),
        _result("overlap", overlap.check(given)),
    ]
    return results, supported


def answers_as(setting: str, reported: str) -> bool:
    """Whether a model a call named is the one the setting configures, a replay's by its file."""
    provider, _, named = setting.partition(":")
    if provider == "replay":
        return reported.startswith(f"replay:{PurePosixPath(named).stem} (")
    return reported == setting


def _gate_run(
    proposal_id: str,
    kind: str,
    given: Given,
    sha256: str,
    results: list[models.CheckResult],
    supported: support.Supported,
    held: dict | None = None,
) -> dict:
    used = given.playbooks
    failed = any(each.outcome == "failed" for each in results)
    return {
        "id": uuid.uuid4().hex,
        "proposal_id": proposal_id,
        "kind": kind,
        "playbook_set": {"id": used.id, "version": used.version, "hash": used.hash},
        "bundle_sha256": sha256,
        "outcome": "failed" if failed else "passed",
        "checks": [each.model_dump(mode="json") for each in results],
        "disagreements": supported.disagreements,
        "disputed": [each.model_dump(mode="json") for each in supported.disputed],
        "held_out": held,
    }


def read_gate_run(row: RowMapping) -> models.GateRun:
    return models.GateRun(
        id=row["id"],
        kind=row["kind"],
        proposal_id=row["proposal_id"],
        at=row["at"],
        who=row["who"],
        playbook_set=models.ReceiptSet(
            id=row["set_id"], version=row["set_version"], hash=row["set_hash"]
        ),
        bundle_sha256=row["bundle_sha256"],
        outcome=row["outcome"],
        checks=row["checks"],
        disagreements=row["disagreements"],
        held_out=row["held_out"],
    )


def read_gate_refusal(row: RowMapping) -> models.GateRefusal:
    return models.GateRefusal(
        proposal_id=row["proposal_id"],
        at=row["at"],
        who=row["who"],
        reason=row["reason"],
        line=row["refusal"],
    )


def _recorded(connection: Connection, gate_run_id: str) -> models.GateRun:
    found = connection.execute(select(db.gate_runs).where(db.gate_runs.c.id == gate_run_id))
    return read_gate_run(found.mappings().one())


def _run_of(connection: Connection, run_id: str) -> RowMapping:
    found = connection.execute(select(db.curator_runs).where(db.curator_runs.c.id == run_id))
    return found.mappings().one()


def _drafted(connection: Connection, proposal_id: str) -> RowMapping:
    found = connection.execute(select(db.proposals).where(db.proposals.c.id == proposal_id))
    if (row := found.mappings().first()) is None:
        raise Refusal(404, "There is no such proposal.")
    if row["state"] != "drafted":
        raise Refusal(409, f"The proposal is {row['state']}: the gate runs on a drafted one.")
    return row


@dataclass
class _Attempt:
    """One gate run as it goes: its ID, and what a refusal of it must name."""

    id: str
    case_set_sha256: str | None = None
    model: str | None = None
    # Whether a trial of it is in the ledger.
    tried: bool = False


def _stopped_with_a_miss(
    connection: Connection, proposal_id: str
) -> tuple[str, str, list[dict]] | None:
    """The trials of a gate run of this proposal that recorded no end, the first in which one
    missed. A gate that stopped with no miss is no result: its trials are in the ledger only."""
    ended = select(db.gate_runs.c.id).union(
        select(db.gate_refusals.c.gate_run_id).where(db.gate_refusals.c.gate_run_id.is_not(None))
    )
    found = connection.execute(
        select(db.held_out_trials)
        .where(
            db.held_out_trials.c.proposal_id == proposal_id,
            db.held_out_trials.c.gate_run_id.not_in(ended),
        )
        .order_by(db.held_out_trials.c.line)
    ).mappings()
    attempts: dict[str, list[RowMapping]] = {}
    for row in found.all():
        attempts.setdefault(row["gate_run_id"], []).append(row)
    for gate_run_id, rows in attempts.items():
        trials = [row["trial"] for row in rows]
        if any(held_out.failures_of(trial) for trial in trials):
            return gate_run_id, rows[0]["case_set_sha256"], trials
    return None


def _curator_models(connection: Connection, run: RowMapping) -> tuple[set[str], set[str]]:
    """Every model the proposal's curator run was configured with or whose name its calls
    gave, over every attempt; and the names the calls of its last attempt gave."""
    started = connection.execute(
        select(db.ledger_entries.c.data).where(
            db.ledger_entries.c.event.in_((events.CURATION_STARTED, events.CURATION_STARTED_AGAIN))
        )
    ).scalars()
    configured = {data["model"] for data in started if data.get("run_id") == run["id"]}
    steps = connection.execute(
        select(db.curator_steps.c.attempt, db.curator_steps.c.model).where(
            db.curator_steps.c.run_id == run["id"]
        )
    ).all()
    last = {step.model for step in steps if step.attempt == run["attempt"]}
    return {run["model"], *configured, *(step.model for step in steps)}, last


class Gate:
    def __init__(
        self,
        journal: Journal,
        store: Store,
        settings: Settings | None = None,
        agent: Agent | None = None,
        trials: Trials | None = None,
    ):
        self._journal = journal
        self._store = store
        given = settings or Settings.from_environment({})
        self._cases = given.cases
        self._trial_model = given.triage_model
        cap = Decimal(given.gate_max_dollars)
        if not cap.is_finite() or cap <= 0:
            raise ValueError("GATE_MAX_DOLLARS is an amount above zero")
        self._held_out = (
            held_out.HeldOut(trials, agent, cap)
            if agent is not None and trials is not None
            else None
        )
        self._running: set[str] = set()
        self._lock = threading.Lock()

    def running(self, proposal_id: str) -> bool:
        """Whether a run of the gate on that proposal is under way."""
        with self._lock:
            return proposal_id in self._running

    def run(self, proposal_id: str, who: str) -> models.GateRun:
        """Run every check on a drafted proposal. One that fails opens its gap reports again."""
        with self._lock:
            if proposal_id in self._running:
                raise Refusal(409, "The gate is already running on that proposal.")
            self._running.add(proposal_id)
        attempt = _Attempt(id=uuid.uuid4().hex)
        try:
            return self._run(proposal_id, who, attempt)
        except GateRefused as refused:
            self._record_refusal(proposal_id, who, refused, attempt)
            raise Refusal(refused.status, refused.line) from None
        finally:
            with self._lock:
                self._running.discard(proposal_id)

    def _record_refusal(
        self, proposal_id: str, who: str, refused: GateRefused, attempt: "_Attempt"
    ) -> None:
        data = {
            "proposal_id": proposal_id,
            "reason": refused.reason,
            "line": refused.line,
            # Where trials ran, they are lines of their own under this gate run's ID.
            "gate_run_id": attempt.id if attempt.tried else None,
            "case_set_sha256": attempt.case_set_sha256,
            "trial_model": attempt.model,
        }
        with self._journal.transaction() as connection:
            _drafted(connection, proposal_id)
            self._store.kept.record(connection, events.GATE_REFUSED, who, triage.now(), data)

    def _keep_trial(self, proposal_id: str, who: str, attempt: "_Attempt", trial: dict) -> None:
        """A trial, as it ends: a miss is in the ledger before the next trial starts."""
        data = {
            "gate_run_id": attempt.id,
            "proposal_id": proposal_id,
            "case_set_sha256": attempt.case_set_sha256,
            "trial": trial,
        }
        with self._journal.transaction() as connection:
            _drafted(connection, proposal_id)
            self._store.kept.record(connection, events.HELD_OUT_TRIAL, who, triage.now(), data)
        attempt.tried = True

    def _model(self) -> str:
        if not self._trial_model:
            raise GateRefused("no_trial_model")
        try:
            return _MODEL_NAME.validate_python(self._trial_model)
        except ValidationError:
            raise GateRefused("trial_model_unusable") from None

    def _run(self, proposal_id: str, who: str, attempt: "_Attempt") -> models.GateRun:
        with self._journal.transaction() as connection:
            row = _drafted(connection, proposal_id)
            run = _run_of(connection, row["run_id"])
            given = Given(
                proposal=models.Proposed.model_validate(row["content"]),
                items=run["items"],
                playbooks=self._store.current(connection),
                desks=self._store.desks,
            )
            stopped = _stopped_with_a_miss(connection, proposal_id)
            curated_by, last_calls = _curator_models(connection, run)
        try:
            case_set = cases.read_set(self._cases, self._store.desks)
        except cases.CasesInvalid as invalid:
            raise GateRefused("cases_unreadable", str(invalid)) from None
        attempt.case_set_sha256 = case_set.sha256
        writer = case_set.writer
        if cases.written_by(self._trial_model, writer):
            raise GateRefused("trial_model_is_writer", writer.name)
        model = self._model()
        attempt.model = model
        if any(cases.written_by(named, writer) for named in curated_by):
            raise GateRefused("curator_model_is_writer", writer.name)
        if not all(answers_as(run["model"], named) for named in last_calls):
            raise GateRefused("curator_answered_as_another")

        results, supported = judged(given)
        before = effect.in_use(given.playbooks)
        after = effect.after(before, given.proposal)
        fixed = held_out.fixed(case_set, before, after)
        earlier_failed = any(each.outcome == "failed" for each in results)
        tried = held_out.Tried(trials=[], failures=[], models=[])
        if stopped is not None:
            # A gate that stopped after a trial missed: the miss stands, and fails this one.
            attempt.id, attempt.case_set_sha256, trials = stopped
            failures = [f for trial in trials for f in held_out.failures_of(trial)]
            note = models.Failure(
                change=None,
                step=None,
                rule="trial",
                line="The gate that ran these trials stopped before it recorded its result.",
            )
            tried = held_out.Tried(trials, [*failures, note], held_out.models_of(trials))
        elif not earlier_failed and not fixed.failures:
            tried = self._tried(proposal_id, who, attempt, case_set, after, model)
        failures = fixed.failures + tried.failures
        if failures:
            outcome = "failed"
        else:
            outcome = "not_run" if earlier_failed else "passed"
        results.append(models.CheckResult(check="held_out", outcome=outcome, failures=failures))
        spent = sum((Decimal(trial["cost_usd"]) for trial in tried.trials), Decimal(0))
        held = {
            "case_set_sha256": attempt.case_set_sha256,
            "writer": writer.name,
            "trial_model": model,
            "models": tried.models,
            "graded": fixed.graded,
            "trials": tried.trials,
            "cost_usd": f"{spent:.6f}",
        }
        return self._record(
            proposal_id, who, given, run["bundle_sha256"], results, supported, held, attempt
        )

    def _tried(
        self,
        proposal_id: str,
        who: str,
        attempt: "_Attempt",
        case_set: cases.CaseSet,
        after: list[effect.Placed],
        model: str,
    ) -> held_out.Tried:
        if self._held_out is None:
            raise GateRefused("agent_did_not_start", "No triage agent is configured.")
        try:
            tried = self._held_out.run(
                case_set,
                after,
                model,
                lambda trial: self._keep_trial(proposal_id, who, attempt, trial),
            )
        except held_out.AgentDidNotStart as stopped:
            missed = [f for trial in stopped.trials for f in held_out.failures_of(trial)]
            if missed:
                # A trial has already missed: the gate fails, whatever stopped the rest.
                line = f"The trials stopped: {stopped}"
                stop = models.Failure(change=None, step=None, rule="trial", line=line)
                trials = stopped.trials
                return held_out.Tried(trials, [*missed, stop], held_out.models_of(trials))
            if stopped.reason == "other_model":
                raise GateRefused("agent_model_differs") from None
            raise GateRefused("agent_did_not_start", str(stopped)) from None
        writer = case_set.writer
        for named in tried.models:
            if cases.written_by(named, writer):
                raise GateRefused("trial_model_is_writer", writer.name)
            if not answers_as(model, named):
                raise GateRefused("other_model_answered")
        return tried

    def _record(
        self,
        proposal_id: str,
        who: str,
        given: Given,
        sha256: str,
        results: list[models.CheckResult],
        supported: support.Supported,
        held: dict,
        attempt: "_Attempt",
    ) -> models.GateRun:
        changed = False
        with self._journal.transaction() as connection:
            _drafted(connection, proposal_id)
            if self._store.current(connection).hash != given.playbooks.hash:
                changed = True
            else:
                made = _gate_run(proposal_id, "gate", given, sha256, results, supported, held)
                made["id"] = attempt.id
                reopened = []
                if made["outcome"] == "failed":
                    reopened = gap_reports.cited_by(connection, proposal_id)
                data = {"gate_run": made, "reopened": reopened}
                self._store.kept.record(connection, events.GATE_RUN, who, triage.now(), data)
                return _recorded(connection, made["id"])
        assert changed
        raise GateRefused("playbooks_changed")

    def check(self, sent: models.Proposed, who: str) -> models.CheckedProposal:
        """The fixed-rule checks on a proposal handed in, which is then checked and final."""
        if wrong := proposals.unreadable(sent):
            raise Refusal(422, wrong)
        content = sent.model_dump(mode="json")
        # Its text is kept only if no rule on text refuses it: the ledger is permanent.
        joined = [proposals.names(change) for change in sent.changes]
        kept = None if personal_data.broken_rule([content, joined], TEXT_LENGTH) else content
        proposal_id = uuid.uuid4().hex
        sha256 = references.content_sha256(content)
        with self._journal.transaction() as connection:
            made = bundle.assemble(connection, self._store)
            given = Given(
                proposal=sent,
                items=made.items,
                playbooks=self._store.current(connection),
                desks=self._store.desks,
            )
            results, supported = judged(given)
            run = _gate_run(proposal_id, "check", given, made.sha256, results, supported)
            data = {
                "proposal": {"id": proposal_id, "sha256": sha256, "content": kept},
                "gate_run": run,
            }
            self._store.kept.record(connection, events.PROPOSAL_CHECKED, who, triage.now(), data)
            return models.CheckedProposal(
                id=proposal_id,
                state="checked",
                sha256=sha256,
                content_kept=kept is not None,
                gate_run=_recorded(connection, run["id"]),
            )
