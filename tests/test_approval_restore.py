"""An approval, a refusal and a stale proposal are each one line, whole at every point and
after a rebuild.
"""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import approving
import firm
import gating
import pytest
from killed import POINTS
from layer import OWNER, running_layer
from test_curation_restore import kill_during, logged, started

from backend.store import Store

OWNER_HEADERS = {"Authorization": f"Bearer {OWNER}"}
EVENT = {
    "approved": "versions_put_in_use",
    "refused": "proposal_refused",
    "stale": "proposal_went_stale",
}
HAPPENED = ("all but the line end", "after the line")


def lived(folder: Path) -> dict[str, str]:
    """A proposal refused after its gate failed, one approved and one gone stale after it."""
    second: dict = {}

    managers = []

    def entered(**options):
        managers.append(running_layer(folder, **options))
        return managers[-1].__enter__()

    failing = gating.honest() | {"disagreements": []}
    layer = approving.scripted(entered, failing, gating.honest(), lambda: second)
    try:
        gating.first_cycle(layer)
        failed = approving.drafted(layer)
        assert gating.gated(layer, failed["id"])["outcome"] == "failed"
        assert approving.refuse(layer, failed["id"], "against_the_sources").status_code == 200
        first = approving.passed(layer)
        report = approving.another_report(layer)
        second.update(approving.resting_on(report["reference"]))
        other = approving.passed(layer)
        approving.approved(layer, *first)
        assert approving.approve(layer, *other).status_code == 409
        return {"refused": failed["id"], "approved": first[0]["id"], "stale": other[0]["id"]}
    finally:
        managers[0].__exit__(None, None, None)


@pytest.fixture
def history(tmp_path):
    folder = tmp_path / "history"
    return folder, lived(folder)


def seen(layer, ids: dict[str, str]) -> dict[str, str]:
    paths = ["/api/record", "/api/gap-reports", "/api/playbooks"]
    paths += [f"/api/proposals/{each}" for each in ids.values()]
    return {path: layer.as_owner("GET", path).text for path in paths}


@pytest.mark.parametrize("deleted_while", ["running", "stopped"])
def test_rebuild_gives_approvals_and_refusals_back(history, deleted_while):
    folder, ids = history
    database = folder / "playbooks.sqlite"
    stored = firm.records(database)
    with running_layer(folder) as layer:
        before = seen(layer, ids)
        states = {p["id"]: p["state"] for p in gating.record(layer)["proposals"]}
        assert states == {proposal: state for state, proposal in ids.items()}
        assert sorted(s for s, _ in approving.reports(layer).values()) == ["open", "resolved"]
        if deleted_while == "running":
            database.unlink()
            assert layer.as_owner("POST", "/api/rebuild").status_code == 200
            assert seen(layer, ids) == before
    if deleted_while == "stopped":
        database.unlink()
    with running_layer(folder) as layer:
        assert seen(layer, ids) == before

    assert firm.records(database) == stored


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE proposals SET state = 'gate_passed', decided_line = NULL WHERE state = 'approved'",
        "UPDATE proposals SET state = 'refused' WHERE state = 'approved'",
        "UPDATE proposals SET state = 'approved' WHERE state = 'stale'",
        "UPDATE proposals SET state = 'approved' WHERE state = 'refused'",
        "UPDATE proposals SET decided_line = 1",
        "UPDATE gap_reports SET state = 'open', proposal_id = NULL WHERE state = 'resolved'",
        "UPDATE gap_reports SET proposal_id = 'x' WHERE state = 'resolved'",
        "UPDATE gap_reports SET state = 'resolved' WHERE state = 'open'",
    ],
)
def test_a_decision_is_never_rewritten(history, statement):
    folder, _ = history
    with closing(sqlite3.connect(folder / "playbooks.sqlite")) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(statement)


def gated_and_stopped(folder: Path, move: str) -> tuple[str, dict, dict]:
    """The records as they are just before the move, and the request that makes it."""
    managers = []

    def entered(**options):
        managers.append(running_layer(folder, **options))
        return managers[-1].__enter__()

    try:
        if move == "approved, with a new playbook":
            layer, proposal, run = approving.new_playbook(entered)
        else:
            layer, proposal, run = approving.first_cycle_passed(entered)
        if move == "stale":
            assert layer.retire("feed-outage", 1).status_code == 200
        path = f"/api/proposals/{proposal['id']}"
        if move == "refused":
            return f"{path}/refuse", OWNER_HEADERS, {"reason": "not_needed"}
        ruled = approving.rulings(layer, proposal)
        body = {"sha256": proposal["sha256"], "gate_run": run["id"], "rulings": ruled}
        return f"{path}/approve", OWNER_HEADERS, body
    finally:
        managers[0].__exit__(None, None, None)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE proposals SET state = 'approved' WHERE state = 'gate_passed'",
        "UPDATE proposals SET state = 'approved', decided_line = 1 WHERE state = 'gate_passed'",
        "UPDATE proposals SET state = 'approved', decided_line = 999 WHERE state = 'gate_passed'",
        "UPDATE proposals SET state = 'refused' WHERE state = 'gate_passed'",
        "UPDATE proposals SET state = 'stale' WHERE state = 'gate_passed'",
        "UPDATE gap_reports SET state = 'resolved' WHERE state = 'cited'",
        "UPDATE gap_reports SET state = 'open', proposal_id = NULL WHERE state = 'cited'",
    ],
)
def test_nothing_is_decided_without_the_line_that_decides_it(tmp_path, statement):
    folder = tmp_path / "passed"
    gated_and_stopped(folder, "approved")
    with closing(sqlite3.connect(folder / "playbooks.sqlite")) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(statement)


@pytest.mark.parametrize("move", [*EVENT, "approved, with a new playbook"])
@pytest.mark.parametrize("point", POINTS)
def test_a_decision_killed_at_each_point_happened_or_left_no_trace(tmp_path, move, point):
    folder = tmp_path / "killed"
    request = gated_and_stopped(folder, move)
    kind = move.partition(",")[0]
    event = EVENT[kind]
    records, before = firm.records(folder / "playbooks.sqlite"), logged(folder)
    playbooks = folder / "playbooks"
    files, folders = gating.approved_files(playbooks), sorted(os.listdir(playbooks))

    kill_during(folder, event, point, request)

    # Killed before its commit, whatever the point, with the old set still in the database.
    assert firm.records(folder / "playbooks.sqlite") == records
    if kind == "approved":
        assert gating.approved_files(playbooks).keys() > files.keys(), "killed before the files"
    happened = point in HAPPENED
    started(folder)

    assert logged(folder) == before + ([event] if happened else [])
    after = gating.approved_files(playbooks)
    assert {name: after[name] for name in files} == files
    if not happened or kind != "approved":
        assert after == files, "a stray file was left"
        assert sorted(os.listdir(playbooks)) == folders, "a stray folder was left"
    with running_layer(folder) as layer:
        (proposal,) = gating.record(layer)["proposals"]
        cited = {state for state, _ in approving.reports(layer).values()}
        versions = set(approving.in_use(layer).values())
        if not happened:
            assert proposal["state"] == "gate_passed" and cited <= {"cited"}
            assert 2 not in versions
        else:
            assert proposal["state"] == kind
            assert cited <= {"resolved" if kind == "approved" else "open"}
            assert (2 in versions) == (kind == "approved")
        if kind == "approved" and not happened:
            # Nothing it left is in the way of the approval made again.
            again = layer.request("POST", request[0], request[1], json=request[2])
            assert again.status_code == 200, again.text
    stored = firm.records(folder / "playbooks.sqlite")
    (folder / "playbooks.sqlite").unlink()
    started(folder)
    assert firm.records(folder / "playbooks.sqlite") == stored


def test_an_approval_that_failed_between_two_files_leaves_nothing(tmp_path, monkeypatch):
    folder = tmp_path / "between"
    path, headers, body = gated_and_stopped(folder, "approved")
    playbooks = folder / "playbooks"
    files = gating.approved_files(playbooks)
    write = Store._write_new
    written = []

    def second_fails(store, playbook, content) -> None:
        if written:
            raise OSError("the disk is full")
        written.append(playbook.id)
        write(store, playbook, content)

    monkeypatch.setattr(Store, "_write_new", second_fails)
    with running_layer(folder) as layer:
        assert layer.request("POST", path, headers, json=body).status_code == 500
        assert approving.in_use(layer) == {"feed-outage": 1, "missing-cross-check": 1}
        assert gating.record(layer)["proposals"][0]["state"] == "gate_passed"
    monkeypatch.undo()
    started(folder)

    assert gating.approved_files(playbooks) == files
    with running_layer(folder) as layer:
        assert layer.request("POST", path, headers, json=body).status_code == 200
