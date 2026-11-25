"""Curator runs and proposals are ledger lines, whole at every point and after a rebuild."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import curation
import firm
import httpx2
import knowledge
import pytest
from killed import POINTS
from layer import (
    AGENT_KEY,
    ANALYST,
    CURATOR_KEY,
    CURATOR_MODEL,
    OWNER,
    ROOT,
    TRIAGE_MODEL,
    address,
    listen,
    running_layer,
    serve,
)
from stand_ins import StandInWealthContext, idle_agent

OWNER_HEADERS = {"Authorization": f"Bearer {OWNER}"}
CURATOR_HEADERS = {"X-Curator-Key": CURATOR_KEY}
EVERY_EVENT = [
    "bundle_refused",
    "curator_run_started",
    "curator_run_undone",
    "curator_run_started_again",
    "curator_restart_undone",
    "curator_calls",
    "proposal_recorded",
    "curator_found_no_change",
    "curator_run_failed",
]
ENDING_A_RUN = "bundle_refused_ending_a_run"
CASE = "A stale price on a closed position must give no step."


def nowhere() -> str:
    bound = listen()
    url = address(bound)
    bound.close()
    return url


def lived(folder: Path, monkeypatch) -> list[str]:
    """Every kind of move on curator runs, made through the API."""
    later = curation.later(monkeypatch)
    runs = []
    with running_layer(folder, agent=lambda *_: idle_agent(), curator=curation.idle) as layer:
        report = curation.first_cycle(layer)["report"]
        proposing = curation.started(layer)
        assert curation.unusable(layer, proposing).status_code == 200
        sent = curation.proposed(report["reference"])
        assert curation.propose(layer, proposing, sent, turn=2).status_code == 200
        unchanged = curation.started(layer)
        assert curation.no_change(layer, unchanged).status_code == 200
        failing = curation.started(layer)
        assert curation.fail(layer, failing).status_code == 200
        runs += [proposing, unchanged, failing, curation.started(layer)]
        later(minutes=5)
        assert curation.curate(layer).status_code == 202
        later(minutes=5)
    with running_layer(folder, curator_url=nowhere()) as layer:
        assert curation.curate(layer).status_code == 503  # a restart undone
        assert curation.fail(layer, runs[-1], attempt=3).status_code == 200
        assert curation.curate(layer).status_code == 503  # a run undone
        assert knowledge.intake(layer, knowledge.memo(CASE, id="memo-with-a-case")).is_success
        (layer.settings.cases / "fixture.txt").write_text(CASE)
        assert curation.curate(layer).status_code == 500
    return runs


@pytest.fixture
def history(tmp_path, monkeypatch):
    folder = tmp_path / "history"
    return folder, lived(folder, monkeypatch)


def seen(layer, runs: list[str]) -> dict[str, str]:
    paths = ["/api/record", "/api/gap-reports", "/api/sources", "/api/playbooks"]
    paths += [f"/api/curator-runs/{run}" for run in runs]
    return {path: layer.as_owner("GET", path).text for path in paths}


def test_the_history_holds_every_kind_of_line(history):
    folder, _ = history
    lines = (folder / "playbooks" / "ledger.jsonl").read_text().splitlines()

    assert set(EVERY_EVENT) <= {json.loads(line)["event"] for line in lines}


@pytest.mark.parametrize("deleted_while", ["not deleted", "running", "stopped"])
def test_rebuild_gives_runs_proposals_and_cited_reports_back(history, deleted_while):
    folder, runs = history
    database = folder / "playbooks.sqlite"
    stored = firm.records(database)
    ledger = (folder / "playbooks" / "ledger.jsonl").read_bytes()
    with running_layer(folder) as layer:
        before = seen(layer, runs)
        assert '"cited"' in before["/api/gap-reports"]
        assert '"drafted"' in before[f"/api/curator-runs/{runs[0]}"]
        if deleted_while == "running":
            database.unlink()
        if deleted_while != "stopped":
            assert layer.as_owner("POST", "/api/rebuild").status_code == 200
            assert seen(layer, runs) == before
    if deleted_while == "stopped":
        database.unlink()
    with running_layer(folder) as layer:
        assert seen(layer, runs) == before

    assert firm.records(database) == stored
    assert (folder / "playbooks" / "ledger.jsonl").read_bytes() == ledger


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE curator_runs SET items = '[]'",
        "UPDATE curator_runs SET state = 'working' WHERE state = 'completed'",
        "DELETE FROM curator_runs",
        "UPDATE curator_steps SET line = 'x'",
        "DELETE FROM curator_steps",
        "UPDATE proposals SET content = '{}'",
        "DELETE FROM proposals",
        "UPDATE gap_reports SET state = 'open', proposal_id = NULL WHERE state = 'cited'",
        "UPDATE gap_reports SET state = 'dismissed' WHERE state = 'cited'",
        "UPDATE gap_reports SET proposal_id = 'x' WHERE state = 'cited'",
    ],
)
def test_a_curation_is_only_added_to(history, statement):
    folder, _ = history
    with closing(sqlite3.connect(folder / "playbooks.sqlite")) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(statement)


# Killed at each point


def prepared(folder: Path, event: str) -> tuple[str, dict, dict | None]:
    """The records as they are just before the move, and the request that makes it."""
    start = ("/api/curator-runs", OWNER_HEADERS, None)
    with running_layer(folder, agent=lambda *_: idle_agent(), curator=curation.idle) as layer:
        report = curation.first_cycle(layer)["report"]
        if event == "bundle_refused":
            assert knowledge.intake(layer, knowledge.memo(CASE, id="memo-with-a-case")).is_success
            (layer.settings.cases / "fixture.txt").write_text(CASE)
        if event == ENDING_A_RUN:
            assert knowledge.intake(layer, knowledge.memo(CASE, id="memo-with-a-case")).is_success
        if event in ("bundle_refused", "curator_run_started", "curator_run_undone"):
            return start
        run_id = curation.started(layer)
        if event == ENDING_A_RUN:
            (layer.settings.cases / "fixture.txt").write_text(CASE)
            return start
        if event in ("curator_run_started_again", "curator_restart_undone"):
            return start  # the killed backend reads every run as stalled
        body: dict = {"turn": 1, "usage": curation.USAGE}
        if event == "curator_calls":
            body |= {"reason": "no_tool_call", "calls": []}
            return curation.work_path(run_id, "/unusable-reply"), CURATOR_HEADERS, body
        if event == "proposal_recorded":
            body |= {"proposal": curation.proposed(report["reference"])}
            return curation.work_path(run_id, "/proposal"), CURATOR_HEADERS, body
        if event == "curator_found_no_change":
            return curation.work_path(run_id, "/no-change"), CURATOR_HEADERS, body
        assert event == "curator_run_failed", event
        failed = {"reason": "unexpected_error"}
        return curation.work_path(run_id, "/fail"), CURATOR_HEADERS, failed


def kill_during(folder: Path, event: str, point: str, request: tuple, agent=None) -> None:
    """`agent(backend_url, key)` makes the triage agent; by default one that does nothing."""
    path, headers, body = request
    theirs, agents, curators, ours = listen(), listen(), listen(), listen()
    wealthcontext = StandInWealthContext()
    environment = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "tests")]),
        "PLAYBOOKS_DATABASE": str(folder / "playbooks.sqlite"),
        "PLAYBOOKS_FOLDER": str(folder / "playbooks"),
        "PLAYBOOKS_TRIAGE_LOG": str(folder / "triage-log.jsonl"),
        "PLAYBOOKS_CASES": str(folder / "cases"),
        # The repository's firm sources, wherever the test runs from.
        "PLAYBOOKS_SOURCES": str(ROOT / "sources"),
        "PLAYBOOKS_ANALYST_TOKEN": ANALYST,
        "PLAYBOOKS_OWNER_TOKEN": OWNER,
        "TRIAGE_AGENT_KEY": AGENT_KEY,
        "CURATOR_KEY": CURATOR_KEY,
        "CURATOR_MODEL": CURATOR_MODEL,
        "TRIAGE_MODEL": TRIAGE_MODEL,
        "WEALTHCONTEXT_URL": address(theirs),
        "WEALTHCONTEXT_TOKEN": wealthcontext.token,
        "TRIAGE_AGENT_URL": address(agents),
        "CURATOR_URL": address(curators),
    }
    triage_agent = agent(address(ours), AGENT_KEY) if agent else idle_agent()
    apps = [(wealthcontext.app, theirs), (triage_agent, agents)]
    if event in ("curator_run_undone", "curator_restart_undone"):
        curators.close()
    else:
        apps.append((idle_agent(), curators))
    with serve(*apps):
        url, descriptor = address(ours), ours.fileno()
        command = [sys.executable, str(ROOT / "tests" / "killed.py"), point, event]
        process = subprocess.Popen(
            [*command, "serve", str(descriptor)], env=environment, pass_fds=[descriptor]
        )
        ours.close()
        try:
            with pytest.raises(httpx2.TransportError):
                with httpx2.Client(base_url=url, timeout=30, trust_env=False) as http:
                    http.post(path, headers=headers, json=body)
            assert process.wait(10) == -signal.SIGKILL
        finally:
            process.kill()
            process.wait()


def logged(folder: Path) -> list[str]:
    found = []
    for line in (folder / "playbooks" / "ledger.jsonl").read_bytes().split(b"\n"):
        try:
            found.append(json.loads(line)["event"])
        except ValueError:
            continue
    return found


def started(folder: Path) -> None:
    with running_layer(folder, curator=curation.idle) as layer:
        assert layer.as_owner("GET", "/api/record").status_code == 200


FOLLOWS = {
    "curator_run_undone": ["curator_run_started"],
    "curator_restart_undone": ["curator_run_started_again"],
}


@pytest.mark.parametrize("move", [*EVERY_EVENT, ENDING_A_RUN])
@pytest.mark.parametrize("point", POINTS)
def test_a_move_killed_at_each_point_happened_or_left_no_trace(tmp_path, move, point):
    folder = tmp_path / "killed"
    request = prepared(folder, move)
    event = "bundle_refused" if move == ENDING_A_RUN else move
    records, before = firm.records(folder / "playbooks.sqlite"), logged(folder)
    # An undo follows the move it undoes, in a transaction of its own.
    first = FOLLOWS.get(event, [])

    kill_during(folder, event, point, request)

    left = firm.records(folder / "playbooks.sqlite")
    if first:
        # Only the move it undoes was committed, or the kill came too late to test anything.
        assert firm.applied_lines(left) == firm.applied_lines(records) + len(first)
    else:
        assert left == records
    happened = point in ("all but the line end", "after the line")
    started(folder)

    assert logged(folder) == before + first + ([event] if happened else [])
    after = firm.records(folder / "playbooks.sqlite")
    if not happened and not first:
        assert after == records, "the move left a trace"
    if happened:
        assert after != records
    (folder / "playbooks.sqlite").unlink()
    started(folder)
    assert firm.records(folder / "playbooks.sqlite") == after
