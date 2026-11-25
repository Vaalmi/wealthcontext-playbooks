"""Gate runs and checked proposals are ledger lines, whole at every point and after a rebuild."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import curation
import firm
import gating
import pytest
import stand_ins
from killed import POINTS
from layer import OWNER, running_layer
from stub_model import call
from test_curation_restore import kill_during, logged, started

from backend import ledger_events
from backend.journal import CannotApply

OWNER_HEADERS = {"Authorization": f"Bearer {OWNER}"}
FAILING = gating.honest() | {"disagreements": []}


def lived(folder: Path) -> list[str]:
    """A gate that failed, one that passed, one that could not run, and a proposal checked."""
    proposals = iter([FAILING, gating.honest(), curation.one()])
    with running_layer(
        folder,
        agent=gating.trial_agent,
        curates=lambda: [lambda: call("submit_proposal", **next(proposals))],
    ) as layer:
        gating.first_cycle(layer)
        failing = curation.curated(layer)["proposal"]["id"]
        assert gating.gated(layer, failing)["outcome"] == "failed"
        passing = curation.curated(layer)["proposal"]["id"]
        assert gating.gated(layer, passing)["outcome"] == "passed"
        refused = curation.curated(layer)["proposal"]["id"]
        case_set = layer.settings.cases / "case-set.yaml"
        kept = case_set.read_bytes()
        case_set.unlink()
        assert gating.gate(layer, refused).status_code == 503
        case_set.write_bytes(kept)
        named = gating.hostile(gating.HOSTILE / "personal-name-in-a-line.yaml")
        checked = gating.checked(layer, gating.sent(named))["id"]
        return [failing, passing, refused, checked]


@pytest.fixture
def history(tmp_path):
    folder = tmp_path / "history"
    return folder, lived(folder)


def seen(layer) -> dict[str, str]:
    paths = ["/api/record", "/api/gap-reports"]
    return {path: layer.as_owner("GET", path).text for path in paths}


@pytest.mark.parametrize("deleted_while", ["running", "stopped"])
def test_rebuild_gives_gate_runs_and_checked_proposals_back(history, deleted_while):
    folder, ids = history
    database = folder / "playbooks.sqlite"
    stored = firm.records(database)
    with running_layer(folder) as layer:
        before = seen(layer)
        states = {p["id"]: p["state"] for p in gating.record(layer)["proposals"]}
        states_now = ["gate_failed", "gate_passed", "drafted", "checked"]
        assert states == dict(zip(ids, states_now, strict=True))
        (refused,) = [p for p in gating.record(layer)["proposals"] if p["id"] == ids[2]]
        assert [r["reason"] for r in refused["gate_refusals"]] == ["cases_unreadable"]
        if deleted_while == "running":
            database.unlink()
            assert layer.as_owner("POST", "/api/rebuild").status_code == 200
            assert seen(layer) == before
    if deleted_while == "stopped":
        database.unlink()
    with running_layer(folder) as layer:
        assert seen(layer) == before

    assert firm.records(database) == stored


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE proposals SET state = 'gate_failed' WHERE state = 'drafted'",
        "UPDATE proposals SET state = 'gate_passed' WHERE state = 'drafted'",
        "UPDATE proposals SET state = 'drafted' WHERE state = 'gate_failed'",
        "UPDATE proposals SET state = 'drafted' WHERE state = 'gate_passed'",
        "UPDATE proposals SET state = 'gate_passed' WHERE state = 'gate_failed'",
        "UPDATE gate_refusals SET reason = 'no_trial_model'",
        "DELETE FROM gate_refusals",
        "UPDATE proposals SET state = 'drafted' WHERE state = 'checked'",
        "UPDATE proposals SET content = NULL",
        "UPDATE proposals SET run_id = NULL",
        "DELETE FROM proposals",
        "UPDATE gate_runs SET outcome = 'passed'",
        "UPDATE gate_runs SET checks = '[]'",
        "DELETE FROM gate_runs",
    ],
)
def test_a_gate_run_is_only_added_to(history, statement):
    folder, _ = history
    with closing(sqlite3.connect(folder / "playbooks.sqlite")) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(statement)


def test_a_report_opens_again_only_when_its_proposal_failed_the_gate(tmp_path):
    folder = tmp_path / "cited"
    with running_layer(
        folder,
        agent=gating.trial_agent,
        curates=lambda: [call("submit_proposal", **gating.honest())],
    ) as layer:
        gating.first_cycle(layer)
        curation.curated(layer)
    with closing(sqlite3.connect(folder / "playbooks.sqlite")) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "UPDATE gap_reports SET state = 'open', proposal_id = NULL WHERE state = 'cited'"
            )


# Killed at each point


def prepared(folder: Path, event: str) -> tuple[str, dict, dict | None]:
    # A held-out trial is recorded only for a proposal that passes every other check.
    proposal = gating.honest() if event == "held_out_trial" else FAILING
    with running_layer(
        folder,
        agent=gating.trial_agent,
        curates=lambda: [call("submit_proposal", **proposal)],
    ) as layer:
        gating.first_cycle(layer)
        if event == "proposal_checked":
            return "/api/checked-proposals", OWNER_HEADERS, gating.honest()
        proposal_id = curation.curated(layer)["proposal"]["id"]
        if event == "gate_refused":
            # With no case set, the gate cannot run, and records why.
            (layer.settings.cases / "case-set.yaml").unlink()
        return f"/api/proposals/{proposal_id}/gate-runs", OWNER_HEADERS, None


def failing_trials(url: str, key: str):
    return stand_ins.trial_agent(url, key, fails=True)


@pytest.mark.parametrize("move", ["gate_run", "gate_refused", "held_out_trial", "proposal_checked"])
@pytest.mark.parametrize("point", POINTS)
def test_a_gate_move_killed_at_each_point_happened_or_left_no_trace(tmp_path, move, point):
    folder = tmp_path / "killed"
    request = prepared(folder, move)
    records, before = firm.records(folder / "playbooks.sqlite"), logged(folder)

    kill_during(folder, move, point, request, failing_trials if move == "held_out_trial" else None)

    assert firm.records(folder / "playbooks.sqlite") == records
    happened = point in ("all but the line end", "after the line")
    started(folder)
    assert logged(folder) == before + ([move] if happened else [])
    after = firm.records(folder / "playbooks.sqlite")
    if happened:
        assert after != records
        if move == "gate_run":
            assert ("gate_failed",) in {(row[2],) for row in after["proposals"]}
            assert {row[2] for row in after["gap_reports"]} == {"open"}
        if move == "gate_refused":
            assert {row[2] for row in after["proposals"]} == {"drafted"}
            assert len(after["gate_refusals"]) == 1
        if move == "held_out_trial":
            assert len(after["held_out_trials"]) == 1 and after["gate_runs"] == []
    else:
        assert after == records, "the move left a trace"
    (folder / "playbooks.sqlite").unlink()
    started(folder)
    assert firm.records(folder / "playbooks.sqlite") == after


def test_a_gate_killed_after_a_trial_missed_fails_the_next_gate_with_that_miss(tmp_path):
    folder = tmp_path / "missed"
    request = prepared(folder, "held_out_trial")
    kill_during(folder, "held_out_trial", "after the line", request, failing_trials)
    started(folder)
    lines = (folder / "playbooks" / "ledger.jsonl").read_text().splitlines()
    (trial,) = [e for e in map(json.loads, lines) if e["event"] == "held_out_trial"]

    with running_layer(folder, agent=gating.trial_agent) as layer:
        proposal_id = trial["data"]["proposal_id"]
        run = gating.gated(layer, proposal_id)

    # The miss stands: no trial runs again, and the gate run is the stopped one's.
    assert run["outcome"] == "failed" and run["id"] == trial["data"]["gate_run_id"]
    assert run["held_out"]["trials"] == [trial["data"]["trial"]]
    (held,) = [c for c in run["checks"] if c["check"] == "held_out"]
    assert [f["line"] for f in held["failures"]] == [
        "Regression case made-crm-unavailable, trial 1 of 3: it ended failed (model_failed), "
        "so it gave no notes to grade.",
        "The gate that ran these trials stopped before it recorded its result.",
    ]


def test_a_gate_run_that_ended_neither_passed_nor_failed_cannot_be_applied():
    line = SimpleNamespace(data={"gate_run": {"outcome": "undecided"}})

    with pytest.raises(CannotApply, match="neither passed nor failed"):
        ledger_events._gate_run(cast(Any, None), cast(Any, line))
