"""A move interrupted at any point either happened or did not.

A real backend process is killed at each point of each kind of move.
"""

import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import firm
import httpx2
import pytest
from killed import POINTS
from layer import (
    AGENT_KEY,
    ANALYST,
    CUTOFF,
    DAY,
    OWNER,
    ROOT,
    address,
    listen,
    running_layer,
    serve,
)
from stand_ins import StandInWealthContext, idle_agent

USAGE = {"model": "stub:triage", "input_tokens": 1, "output_tokens": 1, "cost_usd": None}
RIGHT = {"kind": "step", "playbook": "feed-outage", "step": "portfolio-feed-failed"}
HAPPENED = {
    "before the line": False,
    "part of the line": False,
    "all but the line end": True,
    "after the line": True,
}
assert tuple(HAPPENED) == POINTS
MOVES = [
    "run_started",
    "run_undone",
    "run_started_again",
    "restart_undone",
    "tool_calls",
    "note_fell_back",
    "run_completed",
    "run_failed",
    "version_retired",
    "versions_put_in_use",
]


@dataclass
class Prepared:
    folder: Path
    run_id: str = ""
    note_id: str = ""


def prepared(folder: Path, move: str) -> Prepared:
    """The records as they are just before the move."""
    ready = Prepared(folder)
    with running_layer(folder, agent=lambda *_: idle_agent()) as layer:
        if move == "version_retired":
            layer.put_in_use({"feed-outage": firm.steps("feed-outage", by="within 2 hours")})
        if move in ("run_started", "run_undone", "version_retired", "versions_put_in_use"):
            return ready
        ready.run_id = layer.start()["id"]
        ready.note_id = layer.run(ready.run_id)["notes"][0]["id"]
        base = f"/api/agent/triage-runs/{ready.run_id}/attempts/1/notes/{ready.note_id}"
        if move == "note_fell_back":
            for turn in (1, 2):
                body = {"turn": turn, "choice": {"kind": "no_step"}, "usage": USAGE}
                assert layer.as_agent("POST", f"{base}/choice", json=body).status_code == 422
        if move == "run_completed":
            body = {"turn": 1, "choice": RIGHT, "usage": USAGE}
            assert layer.as_agent("POST", f"{base}/choice", json=body).status_code == 200
    return ready


def request_for(move: str, ready: Prepared) -> tuple[str, dict, dict | None]:
    analyst = {"Authorization": f"Bearer {ANALYST}"}
    agent = {"X-Triage-Agent-Key": AGENT_KEY}
    base = f"/api/agent/triage-runs/{ready.run_id}/attempts/1"
    note = f"{base}/notes/{ready.note_id}"
    match move:
        case "run_started" | "run_undone" | "run_started_again" | "restart_undone":
            return "/api/triage-runs", analyst, {"date": DAY, "cutoff": CUTOFF}
        case "tool_calls":
            return f"{note}/choice", agent, {"turn": 1, "choice": RIGHT, "usage": USAGE}
        case "note_fell_back":
            return f"{note}/fallback", agent, None
        case "run_completed":
            return f"{base}/complete", agent, None
        case "run_failed":
            return f"{base}/fail", agent, {"reason": "model_failed"}
        case _:
            path = "/api/playbooks/feed-outage/versions/2/retire"
            return path, {"Authorization": f"Bearer {OWNER}"}, {"reason": "wrong_action"}


@contextmanager
def outside(move: str) -> Iterator[dict[str, str]]:
    """WealthContext, and a triage agent that says it started, or cannot be reached."""
    theirs, agents = listen(), listen()
    wealthcontext = StandInWealthContext()
    environment = {
        "WEALTHCONTEXT_URL": address(theirs),
        "WEALTHCONTEXT_TOKEN": wealthcontext.token,
        "TRIAGE_AGENT_URL": address(agents),
    }
    apps = [(wealthcontext.app, theirs)]
    if move in ("run_undone", "restart_undone"):
        agents.close()
    else:
        apps.append((idle_agent(), agents))
    with serve(*apps):
        yield environment


def killed_during(move: str, point: str, ready: Prepared) -> None:
    command = [sys.executable, str(ROOT / "tests" / "killed.py"), point, move]
    environment = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "tests")]),
        "PLAYBOOKS_DATABASE": str(ready.folder / "playbooks.sqlite"),
        "PLAYBOOKS_FOLDER": str(ready.folder / "playbooks"),
        "PLAYBOOKS_TRIAGE_LOG": str(ready.folder / "triage-log.jsonl"),
        "PLAYBOOKS_ANALYST_TOKEN": ANALYST,
        "PLAYBOOKS_OWNER_TOKEN": OWNER,
        "TRIAGE_AGENT_KEY": AGENT_KEY,
    }
    if move == "versions_put_in_use":
        done = subprocess.run([*command, "put_in_use"], env=environment, timeout=30)
        assert done.returncode == -signal.SIGKILL
        return

    with outside(move) as found:
        ours = listen()
        url, descriptor = address(ours), ours.fileno()
        process = subprocess.Popen(
            [*command, "serve", str(descriptor)],
            env=environment | found,
            pass_fds=[descriptor],
        )
        # Only the process holds it now, so its death ends the request.
        ours.close()
        try:
            path, headers, body = request_for(move, ready)
            with pytest.raises(httpx2.TransportError):
                with httpx2.Client(base_url=url, timeout=30, trust_env=False) as http:
                    http.post(path, headers=headers, json=body)
            assert process.wait(10) == -signal.SIGKILL
        finally:
            process.kill()
            process.wait()


def events(folder: Path) -> list[str]:
    found = []
    for path in (folder / "playbooks" / "ledger.jsonl", folder / "triage-log.jsonl"):
        for line in path.read_bytes().split(b"\n"):
            try:
                found.append(json.loads(line)["event"])
            except ValueError:
                continue  # a line end, or a line cut short
    return found


def state(folder: Path) -> dict:
    """What the next start shows of the one run and of the playbooks."""
    with running_layer(folder, agent=lambda *_: idle_agent()) as layer:
        listed = layer.backend.state.journal
        with listed.transaction() as connection:
            runs = connection.exec_driver_sql(
                "SELECT id, state, attempt, heard_at FROM triage_runs"
            ).all()
            notes = connection.exec_driver_sql(
                "SELECT state FROM triage_notes JOIN triage_exceptions "
                "ON triage_exceptions.id = triage_notes.exception_id ORDER BY position"
            ).all()
        found = layer.as_analyst("GET", "/api/playbooks").json()
        return {
            "runs": [tuple(run) for run in runs],
            "first note": notes[0][0] if notes else None,
            "in use": [(each["id"], each["version"]) for each in found["in_use"]],
        }


def check_it_happened(move: str, now: dict) -> None:
    run = now["runs"][0] if now["runs"] else None
    match move:
        case "run_started":
            assert run and run[1:3] == ("working", 1) and len(now["runs"]) == 1
        case "run_undone":
            assert run is None
        case "run_started_again":
            assert run and run[1:3] == ("working", 2)
        case "restart_undone":
            assert run and run[1:3] == ("working", 2)
        case "tool_calls":
            assert now["first note"] == "chosen"
        case "note_fell_back":
            assert now["first note"] == "unverified"
        case "run_completed":
            assert run and run[1] == "completed"
        case "run_failed":
            assert run and run[1] == "failed"
        case "version_retired":
            assert now["in use"] == [("feed-outage", 1), ("missing-cross-check", 1)]
        case _:
            assert now["in use"] == [("feed-outage", 2), ("missing-cross-check", 2)]


# An undo follows a commit of its own, so "did not happen" leaves that commit's records.
COMMITTED_FIRST = {"run_undone": "run_started", "restart_undone": "run_started_again"}


@pytest.mark.parametrize("move", MOVES)
@pytest.mark.parametrize("point", POINTS)
def test_a_move_killed_at_each_point_happened_or_left_no_trace(tmp_path, move, point):
    folder = tmp_path / "killed"
    ready = prepared(folder, move)
    before_files = firm.files(folder / "playbooks")
    before_log = (folder / "triage-log.jsonl").read_bytes()
    before_records = firm.records(folder / "playbooks.sqlite")
    before_events = events(folder)

    killed_during(move, point, ready)
    # The database had not committed the move: only the log can say that it happened.
    left = firm.records(folder / "playbooks.sqlite")
    if move in COMMITTED_FIRST:
        # Only the move it undoes was committed, or the kill came too late to test anything.
        assert firm.applied_lines(left) == firm.applied_lines(before_records) + 1
    else:
        assert left == before_records

    now = state(folder)

    expected = list(before_events)
    if move in COMMITTED_FIRST:
        expected.append(COMMITTED_FIRST[move])
    if HAPPENED[point]:
        expected.append(move)
        check_it_happened(move, now)
    assert sorted(events(folder)) == sorted(expected)
    records = firm.records(folder / "playbooks.sqlite")
    if not HAPPENED[point] and move not in COMMITTED_FIRST:
        assert records == before_records, "the move left a trace in the database"
        assert firm.files(folder / "playbooks") == before_files, "the move left a file"
        assert (folder / "triage-log.jsonl").read_bytes() == before_log
    if not HAPPENED[point] and move == "run_undone":
        assert [run[1:3] for run in now["runs"]] == [("working", 1)]
    if not HAPPENED[point] and move == "restart_undone":
        assert [run[1:3] for run in now["runs"]] == [("working", 2)]

    # And the two files alone give the same records.
    (folder / "playbooks.sqlite").unlink()
    assert state(folder) == now
    assert firm.records(folder / "playbooks.sqlite") == records
