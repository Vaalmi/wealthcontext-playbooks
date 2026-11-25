"""The proposal: whether it can be recorded, and how it is shown.

What is checked here is only that a proposal can be read and that it stays inside its
bundle: its form, its texts and what it names. Whether the change is supported, right,
or made to a version still in use is no question of this module's. It also holds what
approval, the view and the record all read of a proposal.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Connection, RowMapping, select

from backend import bundle, models, personal_data
from backend import database as db
from backend.playbook_format import Slug, Step
from backend.refusals import Refusal
from backend.store import PutInUse, VersionInUse

CHECKED = "The proposal is checked: it was handed in to be checked, and can never be approved."
PROPOSAL_TEXT_LENGTH = 300
NO_CHANGE = "The proposal changes no step: use no_change when nothing is to change."
NOT_IN_BUNDLE = (
    "The proposal names an item that is not in the bundle as a firm source or a gap report."
)
_SLUG = TypeAdapter(Slug)
_NOT_A_NAME = "is not lower-case words joined by hyphens"
_WRONG = {
    "id": f"its step {_NOT_A_NAME}",
    "tell": f"its tell {_NOT_A_NAME}",
    "by": "its by is none of the three forms of a deadline",
    "line": "its line is empty, over 160 characters, or holds a number, currency or percentage",
}


def names(change: models.StepChange) -> list[str]:
    """A change's playbook and step, and the two as triage shows them, `playbook/step`.

    Words split across the two are read whole in the third.
    """
    return [change.playbook, change.step, f"{change.playbook}/{change.step}"]


def step_of(change: models.AddStep | models.ChangeStep) -> Step:
    """The step as it would stand in a playbook."""
    fields = change.model_dump(include={"code", "when", "tell", "by", "hold_report", "line"})
    return Step.model_validate({"id": change.step, **fields})


def named(proposal: models.Proposed) -> set[str]:
    """Every bundle item a proposal names."""
    rested = [reference for change in proposal.changes for reference in change.rests_on]
    return set(rested + [each for told in proposal.disagreements for each in told.between])


def malformed(proposal: models.Proposed, desks: Mapping[str, str]) -> str | None:
    """Why a proposal cannot be read, in one line that repeats none of it, or nothing."""
    sent = [proposal.model_dump(mode="json"), *map(names, proposal.changes)]
    if rule := personal_data.broken_rule(sent, PROPOSAL_TEXT_LENGTH):
        return personal_data.refusal("proposal", rule)
    if not proposal.changes:
        return NO_CHANGE
    seen = set()
    for number, change in enumerate(proposal.changes, start=1):
        where = f"The proposal's change {number} is malformed:"
        try:
            _SLUG.validate_python(change.playbook)
            _SLUG.validate_python(change.step)
        except ValidationError:
            return f"{where} its playbook or its step {_NOT_A_NAME}."
        if (change.playbook, change.step) in seen:
            return f"{where} an earlier change names the same step."
        seen.add((change.playbook, change.step))
        if not change.rests_on:
            return f"{where} it names nothing that it rests on."
        if isinstance(change, models.RemoveStep):
            continue
        try:
            step = step_of(change)
        except ValidationError as error:
            field = str(error.errors()[0]["loc"][0])
            return f"{where} {_WRONG.get(field, 'a field is out of its form')}."
        if step.tell not in desks:
            return f"{where} its tell is not on the list of desks."
    for number, told in enumerate(proposal.disagreements, start=1):
        if len(set(told.between)) < 2:
            return f"The proposal's disagreement {number} is malformed: it names fewer than two."
    return None


def unreadable(proposal: models.Proposed) -> str | None:
    """Why a proposal handed in cannot be checked at all, in one line, or nothing.

    Only its form: what it says is for the checks to find, each by its own rule.
    """
    if not proposal.changes:
        return NO_CHANGE
    named = [(change.playbook, change.step) for change in proposal.changes]
    if len(set(named)) != len(named):
        return "The proposal is malformed: two of its changes name the same step."
    for number, told in enumerate(proposal.disagreements, start=1):
        if len(set(told.between)) < 2:
            return f"The proposal's disagreement {number} is malformed: it names fewer than two."
    return None


def outside(proposal: models.Proposed, items: Sequence[Mapping[str, Any]]) -> str | None:
    """Why a proposal does not stay inside its bundle, or nothing."""
    if named(proposal) - bundle.citable(items):
        return NOT_IN_BUNDLE
    in_use = next(item["content"] for item in items if item["kind"] == bundle.PLAYBOOKS_IN_USE)
    listed = {each["playbook"]: each for each in in_use["playbooks"]}
    for number, change in enumerate(proposal.changes, start=1):
        where = f"The proposal's change {number}"
        playbook = listed.get(change.playbook)
        steps = {each["step"] for each in playbook["steps"]} if playbook else set()
        if isinstance(change, models.AddStep):
            if playbook and change.version is None:
                return f"{where} names no version, and the bundle lists one for that playbook."
            if change.step in steps:
                return f"{where} adds a step that the bundle lists already: change it instead."
        elif change.step not in steps:
            return f"{where} names a step that is not in the bundle."
    return None


def of_run(connection: Connection, run_id: str) -> models.Proposal | None:
    found = connection.execute(select(db.proposals).where(db.proposals.c.run_id == run_id))
    if (row := found.mappings().first()) is None:
        return None
    return models.Proposal(
        id=row["id"],
        state=row["state"],
        sha256=row["sha256"],
        recorded_at=row["recorded_at"],
        **row["content"],
    )


def found(connection: Connection, proposal_id: str) -> RowMapping:
    row = connection.execute(select(db.proposals).where(db.proposals.c.id == proposal_id))
    if (kept := row.mappings().first()) is None:
        raise Refusal(404, "There is no such proposal.")
    return kept


def items(connection: Connection, run_id: str) -> list[dict]:
    """The bundle of the curator that made it, as that curator was given it."""
    return connection.execute(
        select(db.curator_runs.c["items"]).where(db.curator_runs.c.id == run_id)
    ).scalar_one()


def last_gate_run(connection: Connection, proposal_id: str) -> RowMapping | None:
    runs = connection.execute(
        select(db.gate_runs)
        .where(db.gate_runs.c.proposal_id == proposal_id, db.gate_runs.c.kind == "gate")
        .order_by(db.gate_runs.c.line.desc())
    )
    return runs.mappings().first()


def evidence(run: Mapping[Any, Any] | None) -> models.Evidence:
    """A gate run as evidence: each check's result. With none, no check's result is there."""
    if run is None:
        return models.Evidence(gate_run=None, outcome=None, playbook_set=None, checks=[])
    return models.Evidence(
        gate_run=run["id"],
        outcome=run["outcome"],
        playbook_set=models.ReceiptSet(
            id=run["set_id"], version=run["set_version"], hash=run["set_hash"]
        ),
        checks=run["checks"],
    )


def versions_written(put: PutInUse) -> list[VersionInUse]:
    return [VersionInUse(id=v.id, version=v.version, sha256=v.sha256) for v in put.versions]
