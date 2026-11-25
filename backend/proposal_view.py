"""A proposal as the playbook owner reads it before approving.

Each changed step stands next to the exact words of each source it cites, as the curator's
bundle kept them, with the disagreements the proposal lists and those the gate found.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import Connection, select

from backend import bundle, models, proposals, rulings
from backend import database as db
from backend.journal import Journal
from backend.refusals import Refusal
from backend.store import Store, steps_of


class ProposalView:
    def __init__(self, journal: Journal, store: Store):
        self._journal = journal
        self._store = store

    def read(self, proposal_id: str) -> models.ProposalShown:
        with self._journal.transaction() as connection:
            row = proposals.found(connection, proposal_id)
            if row["state"] == "checked":
                raise Refusal(409, proposals.CHECKED)
            items = proposals.items(connection, row["run_id"])
            run = proposals.last_gate_run(connection, proposal_id)
            proposal = models.Proposed.model_validate(row["content"])
            words = {each.reference: each for each in _cited(items)}
            found = run["disagreements"] if run else []
            return models.ProposalShown(
                id=row["id"],
                state=row["state"],
                sha256=row["sha256"],
                gate_run=proposals.evidence(run),
                changes=[
                    models.ChangeShown(
                        change=change,
                        desk=(
                            None
                            if isinstance(change, models.RemoveStep)
                            else self._store.desks.get(change.tell)
                        ),
                        before=_before(connection, change),
                        cites=[words[each] for each in change.rests_on],
                    )
                    for change in proposal.changes
                ],
                disagreements=[
                    models.DisagreementShown(
                        between=told.between,
                        about=told.about,
                        sources=[words[each] for each in told.between],
                    )
                    for told in proposal.disagreements
                ],
                found_disagreements=found,
                to_rule_on=(
                    rulings.to_rule_on(proposal, run["disputed"])
                    if run and run["disputed"] is not None
                    else []
                ),
            )


def _cited(items: Sequence[Mapping[str, Any]]) -> list[models.Cited]:
    cited = []
    for item in items:
        content = item["content"]
        if item["kind"] == bundle.FIRM_SOURCE:
            kind, words = content["kind"], content["text"]
        elif item["kind"] == bundle.GAP_REPORT:
            kind, words = "gap_report", content["summary"]
        else:
            continue
        cited.append(
            models.Cited(
                reference=item["reference"],
                kind=kind,
                sha256=item["sha256"],
                words=words,
                desk=content.get("desk"),
                date=content.get("date"),
                scope=content.get("scope"),
                correction=content.get("correction"),
            )
        )
    return cited


def _before(connection: Connection, change: models.StepChange) -> models.StepBefore | None:
    """The step in the version a change applies to, as that version's file has it."""
    if change.version is None:
        return None
    kept = connection.execute(
        select(db.versions).where(
            db.versions.c.playbook == change.playbook, db.versions.c.version == change.version
        )
    ).first()
    for each in steps_of(kept) if kept else []:
        if each.step.id == change.step:
            # An action is given out from a version with a recorded approval, and no other.
            approved = each.approved_by is not None
            return models.StepBefore(
                version=each.version,
                approval_recorded=approved,
                code=each.step.code,
                when=each.step.when,
                tell=each.step.tell if approved else None,
                desk=each.desk if approved else None,
                by=each.step.by if approved else None,
                hold_report=each.step.hold_report if approved else None,
                line=each.step.line,
            )
    return None
