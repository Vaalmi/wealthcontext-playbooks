"""The playbook owner's moves: approve, refuse and retire, each one ledger line in one
transaction.

An approval is tied to one exact proposal, by its hash, to the gate run that passed it, and
to the playbook set that run was made against. The line that puts the new versions in use
is the approval, the ruling and the resolving of the gap reports. A proposal whose set has
changed goes stale: the set that goes into use is always one the gate tested as a whole.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlalchemy import Connection, RowMapping, select

from backend import bundle, clock, gap_reports, models, proposals, references, rulings
from backend import database as db
from backend import ledger_events as events
from backend.gate import effect
from backend.journal import Journal
from backend.refusals import Refusal
from backend.store import (
    ApprovedProposal,
    Overlap,
    PlaybookSet,
    PutInUse,
    RestsOn,
    Retirement,
    Ruling,
    Store,
)
from backend.vocabulary import OWNER

STALE = (
    "The proposal is stale: the playbooks in use changed after its gate ran. "
    "The gate must run again, on a new proposal."
)
NOT_THIS_ONE = (
    "The hash or the gate run named is not this proposal's, so nothing changes. "
    "It is still gate_passed."
)
NOTHING_TO_RULE_BY = (
    "The proposal is gate_passed by a gate run whose record holds nothing to judge a ruling "
    "by, so it cannot be approved. Refuse it and curate again: the gate must run on a new "
    "proposal."
)
GATE_RUNNING = (
    "The proposal is drafted and the gate is running on it: refuse it once the gate has ended."
)


class Approval:
    def __init__(self, journal: Journal, store: Store, gate_running: Callable[[str], bool]):
        self._journal = journal
        self._store = store
        self._gate_running = gate_running

    def retire(self, playbook: str, version: int, sent: models.Retire) -> models.Retired:
        with self._journal.transaction() as connection:
            found = _version(connection, playbook, version)
            if found["state"] == "retired":
                # The same move sent again returns what it did the first time.
                before = Retirement.model_validate(_written(connection, found["retired_line"]))
                if before.reason == sent.reason:
                    return _retired(connection, before, found["retired_line"])
            if found["state"] != "in_use":
                raise Refusal(409, f"The version is {found['state']}.")
            at = clock.stamp(clock.now())
            try:
                line = self._store.retire(
                    connection, playbook, version, sent.reason, who=OWNER, at=at
                )
            except Overlap as overlap:
                raise Refusal(
                    409,
                    "The version is in_use and cannot be retired: the one before it "
                    f"would overlap another in use ({overlap}).",
                ) from None
            return _retired(connection, Retirement.model_validate(line.data), line.line)

    def approve(self, proposal_id: str, sent: models.Approve) -> models.Approved:
        with self._journal.transaction() as connection:
            row = proposals.found(connection, proposal_id)
            match row["state"]:
                case "checked":
                    raise Refusal(409, proposals.CHECKED)
                case "approved":
                    return _approved_again(connection, row, sent)
                case "stale":
                    raise Refusal(409, STALE)
                case "gate_passed":
                    pass
                case state:
                    line = f"The proposal is {state}: only one that passed the gate is approved."
                    raise Refusal(409, line)
            run = proposals.last_gate_run(connection, proposal_id)
            assert run is not None  # nothing else made it gate_passed
            exact = references.content_sha256(row["content"]) == row["sha256"] == sent.sha256
            if not exact or run["outcome"] != "passed" or run["id"] != sent.gate_run:
                raise Refusal(409, NOT_THIS_ONE)
            playbooks = self._store.current(connection)
            if playbooks.hash == run["set_hash"]:
                if run["disputed"] is None:
                    raise Refusal(409, NOTHING_TO_RULE_BY)
                proposal = models.Proposed.model_validate(row["content"])
                items = proposals.items(connection, row["run_id"])
                # Everything that can refuse does so before a version file is written.
                approval = ApprovedProposal(
                    proposal_id=proposal_id,
                    sha256=row["sha256"],
                    gate_run_id=run["id"],
                    rulings=rulings.settled(proposal, run["disputed"], _memos(items), sent.rulings),
                    resolved=gap_reports.cited_by(connection, proposal_id),
                )
                written = self._store.put_in_use(
                    connection,
                    _changed(playbooks, proposal),
                    who=OWNER,
                    at=clock.stamp(clock.now()),
                    rests_on=_rests_on(proposal, items),
                    approval=approval,
                )
                return _approved(connection, written.line)
            self._gone_stale(connection, row, run, playbooks)
        # Raised after the transaction, so that the line that says it is stale stands.
        raise Refusal(409, STALE)

    def _gone_stale(
        self, connection: Connection, row: RowMapping, run: RowMapping, now: PlaybookSet
    ) -> None:
        data = {
            "proposal_id": row["id"],
            "sha256": row["sha256"],
            "gate_run_id": run["id"],
            "gate_ran_against": {
                "id": run["set_id"],
                "version": run["set_version"],
                "hash": run["set_hash"],
            },
            "in_use": {"id": now.id, "version": now.version, "hash": now.hash},
            "reopened": gap_reports.cited_by(connection, row["id"]),
        }
        at = clock.stamp(clock.now())
        self._store.kept.record(connection, events.PROPOSAL_STALE, OWNER, at, data)

    def refuse(self, proposal_id: str, sent: models.Refuse) -> models.ProposalRefused:
        with self._journal.transaction() as connection:
            row = proposals.found(connection, proposal_id)
            if row["state"] == "checked":
                raise Refusal(409, proposals.CHECKED)
            if row["state"] == "refused":
                before = _written(connection, row["decided_line"])
                if before["reason"] == sent.reason:
                    return _refused(row["decided_line"], before)
            if row["state"] not in events.REFUSABLE:
                raise Refusal(409, f"The proposal is {row['state']}, so it cannot be refused.")
            # A trial that ends is recorded only on a drafted proposal, so it would never end.
            if self._gate_running(proposal_id):
                raise Refusal(409, GATE_RUNNING)
            data = {
                "proposal_id": proposal_id,
                "sha256": row["sha256"],
                "reason": sent.reason,
                "reopened": gap_reports.cited_by(connection, proposal_id),
            }
            at = clock.stamp(clock.now())
            made = self._store.kept.record(connection, events.PROPOSAL_REFUSED, OWNER, at, data)
            return _refused(made.line, made.data)


def _changed(playbooks: PlaybookSet, proposal: models.Proposed) -> dict:
    """Each changed playbook's steps, worked out as the gate worked out what it tested."""
    after = effect.after(effect.in_use(playbooks), proposal)
    return {
        name: [each.as_step() for each in after if each.playbook == name]
        for name in {change.playbook for change in proposal.changes}
    }


def _memos(items: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        each["reference"]
        for each in items
        if each["kind"] == bundle.FIRM_SOURCE and each["content"]["kind"] == "memo"
    }


def _rests_on(
    proposal: models.Proposed, items: Sequence[Mapping[str, Any]]
) -> dict[str, list[RestsOn]]:
    """What each new version rests on, by the hashes of the curator's manifest."""
    hashes = {each["reference"]: each["sha256"] for each in items if each["kind"] in bundle.CITABLE}
    rested: dict[str, set[str]] = {}
    for change in proposal.changes:
        rested.setdefault(change.playbook, set()).update(change.rests_on)
    return {
        playbook: [RestsOn(id=each, sha256=hashes[each]) for each in sorted(cited)]
        for playbook, cited in rested.items()
    }


def _version(connection: Connection, playbook: str, version: int) -> RowMapping:
    found = connection.execute(
        select(db.versions).where(
            db.versions.c.playbook == playbook, db.versions.c.version == version
        )
    )
    row = found.mappings().first()
    if row is None:
        raise Refusal(404, "There is no such playbook version.")
    return row


def _written(connection: Connection, line: int) -> dict:
    return connection.execute(
        select(db.ledger_entries.c.data).where(db.ledger_entries.c.line == line)
    ).scalar_one()


def _set_of(connection: Connection, line: int) -> models.ReceiptSet:
    found = connection.execute(select(db.playbook_sets).where(db.playbook_sets.c.line == line))
    then = found.one()
    return models.ReceiptSet(id=then.id, version=then.version, hash=then.hash)


def _retired(connection: Connection, retirement: Retirement, line: int) -> models.Retired:
    return models.Retired(
        playbook=retirement.playbook,
        version=retirement.version,
        state="retired",
        now_in_use=retirement.restored,
        playbook_set=_set_of(connection, line),
    )


def _approved_again(
    connection: Connection, row: RowMapping, sent: models.Approve
) -> models.Approved:
    """The same move sent again returns what it did the first time. Another is refused."""
    before = PutInUse.model_validate(_written(connection, row["decided_line"])).approval
    assert before is not None  # nothing else made it approved
    was = (before.sha256, before.gate_run_id, _as_ruled(before.rulings))
    if was != (sent.sha256, sent.gate_run, _as_ruled(sent.rulings)):
        raise Refusal(409, "The proposal is approved already, so nothing changes.")
    return _approved(connection, row["decided_line"])


def _as_ruled(made: Sequence[Ruling | models.RuleFor]) -> set[tuple]:
    """Rulings as a move is compared: in whatever order they and their sources were sent."""
    return {(each.change, tuple(sorted(set(each.between))), each.ruled_for) for each in made}


def _approved(connection: Connection, line: int) -> models.Approved:
    put = PutInUse.model_validate(_written(connection, line))
    assert put.approval is not None
    return models.Approved(
        proposal_id=put.approval.proposal_id,
        state="approved",
        line=line,
        versions=proposals.versions_written(put),
        rulings=put.approval.rulings,
        resolved=put.approval.resolved,
        playbook_set=_set_of(connection, line),
    )


def _refused(line: int, data: Mapping[str, Any]) -> models.ProposalRefused:
    return models.ProposalRefused(
        proposal_id=data["proposal_id"],
        state="refused",
        line=line,
        reason=data["reason"],
        reopened=data["reopened"],
    )
