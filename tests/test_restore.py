"""The database can be lost: the ledger and the triage log give it back."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import firm
import pytest
from layer import (
    ANALYST,
    CROSS_CHECK_DAY,
    CUTOFF,
    GROUPING_DAY,
    OWNER,
    address,
    listen,
    running_layer,
)
from stand_ins import idle_agent

from backend import triage
from cli import main

USAGE = {"model": "stub:triage", "input_tokens": 9, "output_tokens": 2, "cost_usd": "0.000123"}
RIGHT = {"kind": "step", "playbook": "feed-outage", "step": "portfolio-feed-failed"}
NONE = {"kind": "no_step"}


@pytest.fixture
def later(monkeypatch):
    ahead = [timedelta(0)]
    monkeypatch.setattr(triage, "_clock", lambda: datetime.now(UTC) + ahead[0])

    def pass_five_minutes() -> None:
        ahead[0] += timedelta(minutes=5)

    return pass_five_minutes


def nowhere() -> str:
    bound = listen()
    url = address(bound)
    bound.close()
    return url


def lived(folder, later) -> list[str]:
    """Every kind of move there is, made through the API, over two lives of the backend."""

    def choose(layer, run_id, choice, turn=1, attempt=1) -> int:
        note = layer.run(run_id)["notes"][0]["id"]
        path = f"/api/agent/triage-runs/{run_id}/attempts/{attempt}/notes/{note}/choice"
        body = {"turn": turn, "choice": choice, "usage": USAGE}
        return layer.as_agent("POST", path, json=body).status_code

    def end(layer, run_id, how, body=None, attempt=1) -> None:
        path = f"/api/agent/triage-runs/{run_id}/attempts/{attempt}/{how}"
        assert layer.as_agent("POST", path, json=body).status_code == 200

    with running_layer(folder, agent=lambda *_: idle_agent()) as layer:
        chosen = layer.start()["id"]
        assert choose(layer, chosen, RIGHT) == 200
        end(layer, chosen, "complete")
        assert choose(layer, chosen, NONE, turn=2) == 409  # a step on a run that completed

        fell_back = layer.start()["id"]
        assert choose(layer, fell_back, NONE) == 422
        note = layer.run(fell_back)["notes"][0]["id"]
        of_note = f"/api/agent/triage-runs/{fell_back}/attempts/1/notes/{note}"
        body = {
            "turn": 2,
            "reason": "several_tool_calls",
            "calls": ["choose_step", "no_step_applies", "other"],
            "usage": USAGE,
        }
        assert layer.as_agent("POST", f"{of_note}/unusable-reply", json=body).status_code == 200
        assert layer.as_agent("POST", f"{of_note}/fallback").status_code == 200
        end(layer, fell_back, "complete")

        layer.put_in_use({"feed-outage": firm.steps("feed-outage", by="within 2 hours")})
        failed = layer.start()["id"]
        end(layer, failed, "fail", {"reason": "cost_cap"})
        assert choose(layer, failed, RIGHT) == 409

        replaced = layer.start()["id"]
        assert choose(layer, replaced, RIGHT) == 200
        end(layer, replaced, "complete")

        again = layer.start(CROSS_CHECK_DAY)["id"]
        stalled = layer.start(GROUPING_DAY)["id"]
        later()
        assert layer.start(CROSS_CHECK_DAY)["attempt"] == 2
        assert choose(layer, again, NONE) == 409  # a step of the attempt before
        assert layer.retire("feed-outage", 2, "source_withdrawn").status_code == 200

    with running_layer(folder, agent_url=nowhere()) as layer:
        refused = layer.as_analyst(
            "POST", "/api/triage-runs", json={"date": GROUPING_DAY, "cutoff": CUTOFF}
        )
        assert refused.status_code == 503, "the restart was undone"
        unstarted = layer.as_analyst(
            "POST",
            "/api/triage-runs",
            json={"date": "2026-10-15", "cutoff": "2026-10-30T16:00:00Z"},
        )
        assert unstarted.status_code == 503, "the run was undone"
        assert layer.retire("missing-cross-check", 1, "no_longer_applies").status_code == 200

    return [chosen, fell_back, failed, replaced, again, stalled]


@pytest.fixture
def history(tmp_path, later):
    folder = tmp_path / "history"
    return folder, lived(folder, later)


def test_the_history_holds_every_kind_of_line(history):
    folder, _ = history
    logged = (folder / "triage-log.jsonl").read_text().splitlines()
    ledger = (folder / "playbooks" / "ledger.jsonl").read_text().splitlines()

    assert {json.loads(line)["event"] for line in logged} == {
        "run_started",
        "run_undone",
        "run_started_again",
        "restart_undone",
        "tool_calls",
        "note_fell_back",
        "run_completed",
        "run_failed",
    }
    assert [json.loads(line)["event"] for line in ledger] == [
        *firm.FOUNDING_EVENTS,
        "versions_put_in_use",
        "version_retired",
        "version_retired",
    ]


@pytest.fixture
def command(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PLAYBOOKS_ANALYST_TOKEN", ANALYST)
    monkeypatch.setenv("PLAYBOOKS_OWNER_TOKEN", OWNER)

    def said(layer, *arguments: str) -> bytes:
        monkeypatch.setenv("PLAYBOOKS_URL", layer.url)
        assert main.main(list(arguments)) == 0
        return capsys.readouterr().out.encode()

    return said


def seen(layer, run_ids) -> list:
    return [layer.run(run_id) for run_id in run_ids]


@pytest.mark.parametrize("deleted_while", ["running", "stopped"])
def test_rebuild_on_a_deleted_database_gives_everything_back(history, command, deleted_while):
    folder, run_ids = history
    database = folder / "playbooks.sqlite"
    stored = firm.records(database)
    logs = (folder / "triage-log.jsonl").read_bytes(), firm.files(folder / "playbooks")

    said = b""
    with running_layer(folder) as layer:
        record = command(layer, "record")
        as_json = command(layer, "record", "--json")
        runs = seen(layer, run_ids)
        if deleted_while == "running":
            database.unlink()
            said = command(layer, "rebuild")
            assert command(layer, "record") == record
    if deleted_while == "stopped":
        database.unlink()
    with running_layer(folder) as layer:
        if deleted_while == "stopped":
            said = command(layer, "rebuild")
        assert command(layer, "record") == record
        assert command(layer, "record", "--json") == as_json
        assert seen(layer, run_ids) == runs

    assert said.startswith(b"Rebuilt the database from 5 ledger lines and ")
    assert firm.records(database) == stored
    assert logs == ((folder / "triage-log.jsonl").read_bytes(), firm.files(folder / "playbooks"))


def test_a_rebuild_over_a_database_that_is_there_changes_nothing(history, command):
    folder, _ = history
    stored = firm.records(folder / "playbooks.sqlite")

    with running_layer(folder) as layer:
        command(layer, "rebuild")
        command(layer, "rebuild")

    assert firm.records(folder / "playbooks.sqlite") == stored


def test_the_runs_come_back_able_to_go_on(history, later):
    folder, run_ids = history
    (folder / "playbooks.sqlite").unlink()
    later()

    with running_layer(folder, agent=lambda *_: idle_agent()) as layer:
        again, stalled = layer.start(CROSS_CHECK_DAY), layer.start(GROUPING_DAY)

    assert (again["id"], again["attempt"]) == (run_ids[4], 3)
    assert (stalled["id"], stalled["attempt"]) == (run_ids[5], 3)


def test_the_log_holds_each_note_as_it_was_settled(history):
    folder, run_ids = history
    logged = {}
    for text in (folder / "triage-log.jsonl").read_text().splitlines():
        data = json.loads(text)["data"]
        settled = data.get("settled")
        if isinstance(settled, list):
            logged.update((note["id"], note) for note in settled)
        elif settled:
            logged[settled["note"]["id"]] = settled["note"]

    with running_layer(folder) as layer:
        shown = [note for run in seen(layer, run_ids) for note in run["notes"]]

    settled_notes = {note["id"]: note for note in shown if note["state"] != "pending"}
    assert settled_notes
    for note_id, note in settled_notes.items():
        assert logged[note_id] == note


def test_the_record_says_what_who_when_and_why(history, command):
    folder, _ = history

    with running_layer(folder) as layer:
        said = command(layer, "record").decode()
        assert layer.request("GET", "/api/record").status_code == 401

    assert "1. " in said and "playbooks founded, by founding" in said
    assert "founding approved, by playbook_owner" in said
    assert "versions put in use, by playbook_owner" in said
    assert "memo-for-this-test" in said and "memo-feed-outage-founding" in said
    assert "version retired, by playbook_owner" in said
    assert "reason: source_withdrawn" in said
    assert "WC-AC-" not in said


def test_rebuild_is_the_playbook_owners(make_layer):
    layer = make_layer(agent=lambda *_: idle_agent())

    refused = layer.as_analyst("POST", "/api/rebuild")

    assert refused.status_code == 403
    assert layer.as_agent("POST", "/api/rebuild").status_code == 401


@pytest.mark.parametrize("changed", ["ledger.jsonl", "feed-outage/1.yaml", "triage log"])
def test_rebuild_from_files_that_changed_refuses_and_changes_nothing(history, changed):
    folder, _ = history
    with running_layer(folder) as layer:
        stored = firm.records(folder / "playbooks.sqlite")
        path = folder / "playbooks" / changed
        if changed == "triage log":
            path = folder / "triage-log.jsonl"
        content = bytearray(path.read_bytes())
        content[20] ^= 1
        path.write_bytes(content)

        response = layer.as_owner("POST", "/api/rebuild")

        assert response.status_code == 500
        assert path.name in response.json()["detail"]
        assert "WC-AC-" not in response.text
        assert firm.records(folder / "playbooks.sqlite") == stored


def test_a_rebuild_that_fails_part_way_changes_nothing(history):
    folder, _ = history
    with running_layer(folder) as layer:
        stored = firm.records(folder / "playbooks.sqlite")
        journal = layer.backend.state.journal
        kept = journal._kept[1]

        def broken(connection, line) -> None:
            if line.line == 5:
                raise RuntimeError("stopped part way")
            kept.apply(connection, line)

        journal._kept = (journal._kept[0], replace(kept, apply=broken))
        assert layer.as_owner("POST", "/api/rebuild").status_code == 500
        assert firm.records(folder / "playbooks.sqlite") == stored


def test_a_line_whose_commit_failed_is_applied_before_anything_else(make_layer):
    from sqlalchemy import event

    layer = make_layer(agent=lambda *_: idle_agent())
    engine = layer.backend.state.journal._engine
    layer.put_in_use({"feed-outage": firm.steps("feed-outage")})
    failed = []

    def fail_once(_connection) -> None:
        if not failed:
            failed.append(True)
            raise RuntimeError("the commit failed")

    event.listen(engine, "commit", fail_once)
    response = layer.retire("feed-outage", 2)
    event.remove(engine, "commit", fail_once)

    assert response.status_code == 500
    in_use = layer.as_analyst("GET", "/api/playbooks").json()
    assert in_use["version"] == firm.APPROVED_SET + 2
    assert in_use["in_use"][0]["version"] == 1
    lines = (layer.settings.playbooks / "ledger.jsonl").read_text().splitlines()
    assert len(lines) == len(firm.FOUNDING_EVENTS) + 2


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger_entries SET who = 'someone'",
        "UPDATE playbook_sets SET hash = 'x'",
        "UPDATE playbook_versions SET sha256 = 'x'",
        # Beside a move a version may make: who approved it is not changed with it.
        "UPDATE playbook_versions SET state = 'superseded', approved_by = 'someone' "
        "WHERE state = 'in_use'",
        "UPDATE playbook_versions SET state = 'in_use' WHERE state = 'retired'",
        "DELETE FROM ledger_entries",
        "DELETE FROM playbook_sets",
        "DELETE FROM playbook_versions",
    ],
)
def test_the_playbook_records_refuse_rewrites_and_deletes(history, statement):
    folder, _ = history

    with closing(sqlite3.connect(folder / "playbooks.sqlite")) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement)
