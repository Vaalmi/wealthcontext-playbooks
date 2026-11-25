"""The listable record: what happened to the firm's knowledge, as the ledger has it, and every
proposal with each run of the checks on it and the decision that ended it."""

from typing import Any

from sqlalchemy import Connection, RowMapping, select

from backend import bundle, models, proposals
from backend import database as db
from backend.gate.run import read_gate_refusal, read_gate_run
from backend.journal import Journal
from backend.store import PUT_IN_USE, PutInUse


def _without_digest(value: Any) -> Any:
    # A subject's digest only tells two gap reports apart. It is never shown.
    if isinstance(value, dict):
        return {k: _without_digest(v) for k, v in value.items() if k != "subject_digest"}
    if isinstance(value, list):
        return [_without_digest(each) for each in value]
    return value


def read(journal: Journal) -> models.Record:
    with journal.transaction() as connection:
        found = connection.execute(
            select(
                db.ledger_entries,
                db.playbook_sets.c.id.label("set_id"),
                db.playbook_sets.c.version.label("set_version"),
                db.playbook_sets.c.hash.label("set_hash"),
            )
            .outerjoin(db.playbook_sets, db.playbook_sets.c.line == db.ledger_entries.c.line)
            .order_by(db.ledger_entries.c.line)
        ).all()
        proposed = (
            connection.execute(select(db.proposals).order_by(db.proposals.c.line)).mappings().all()
        )
        gate_runs = (
            connection.execute(select(db.gate_runs).order_by(db.gate_runs.c.line)).mappings().all()
        )
        refusals = (
            connection.execute(select(db.gate_refusals).order_by(db.gate_refusals.c.line))
            .mappings()
            .all()
        )
        decided = {row["id"]: _decision(connection, row) for row in proposed if row["decided_line"]}
    entries = [
        models.RecordEntry(
            line=row.line,
            at=row.at,
            who=row.who,
            event=row.event,
            data=_without_digest(row.data),
            hash=row.hash,
            playbook_set=(
                models.ReceiptSet(id=row.set_id, version=row.set_version, hash=row.set_hash)
                if row.set_id
                else None
            ),
        )
        for row in found
    ]
    listed = [
        models.ListedProposal(
            id=row["id"],
            state=row["state"],
            run_id=row["run_id"],
            sha256=row["sha256"],
            recorded_at=row["recorded_at"],
            content=row["content"],
            gate_runs=[
                read_gate_run(each) for each in gate_runs if each["proposal_id"] == row["id"]
            ],
            gate_refusals=[
                read_gate_refusal(each) for each in refusals if each["proposal_id"] == row["id"]
            ],
            decision=decided.get(row["id"]),
        )
        for row in proposed
    ]
    return models.Record(entries=entries, proposals=listed)


def _decision(connection: Connection, row: RowMapping) -> models.Decision:
    """What changed, why, on what evidence and who, from the line that decided it."""
    entry = connection.execute(
        select(db.ledger_entries).where(db.ledger_entries.c.line == row["decided_line"])
    ).one()
    versions, rulings, reason = [], [], entry.data.get("reason")
    if entry.event == PUT_IN_USE:
        put = PutInUse.model_validate(entry.data)
        assert put.approval is not None  # only an approval decides a proposal
        versions, rulings = proposals.versions_written(put), put.approval.rulings
    proposal = models.Proposed.model_validate(row["content"])
    rested = {each for change in proposal.changes for each in change.rests_on}
    return models.Decision(
        decision=row["state"],
        line=entry.line,
        at=entry.at,
        what_changed=models.WhatChanged(changes=proposal.changes, versions=versions),
        why=models.Why(
            rests_on=[
                models.RestedOn(
                    reference=item["reference"],
                    kind=(
                        "gap_report"
                        if item["kind"] == bundle.GAP_REPORT
                        else item["content"]["kind"]
                    ),
                    sha256=item["sha256"],
                )
                for item in proposals.items(connection, row["run_id"])
                if item["kind"] in bundle.CITABLE and item["reference"] in rested
            ],
            rulings=rulings,
            reason=reason,
        ),
        evidence=proposals.evidence(proposals.last_gate_run(connection, row["id"])),
        who=entry.who,
    )
