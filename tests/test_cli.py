"""The `playbooks` command, against the real backend."""

import json
from types import SimpleNamespace

import pytest
from layer import ANALYST, CUTOFF, DAY, OPTION, OWNER
from stub_model import call

from cli import main

COMMAND = ["triage", "--date", DAY, "--cutoff", CUTOFF]


@pytest.fixture
def layer(make_layer, monkeypatch, tmp_path):
    layer = make_layer(replies=lambda: [call("choose_step", option=OPTION)])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PLAYBOOKS_URL", layer.url)
    monkeypatch.setenv("PLAYBOOKS_ANALYST_TOKEN", ANALYST)
    return layer


def test_triage_ends_completed_with_the_seven_notes(layer, capsys):
    assert main.main(COMMAND) == 0

    said = capsys.readouterr().out
    assert said.startswith(f"Triage of {DAY}, cutoff {CUTOFF}: completed")
    assert "feed run: PAS-B pas_snapshots FAILED" in said
    assert "step: feed-outage version 1, portfolio-feed-failed" in said
    assert said.count("No approved step applies.") == 6
    assert "5 informational notices, with no note:" in said


def test_triage_can_print_json(layer, capsys):
    assert main.main([*COMMAND, "--json"]) == 0

    assert len(json.loads(capsys.readouterr().out)["notes"]) == 7


def test_the_token_is_read_from_what_the_cycle_left(layer, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("PLAYBOOKS_ANALYST_TOKEN")
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local" / "playbooks.env").write_text(f"PLAYBOOKS_ANALYST_TOKEN={ANALYST}\n")

    assert main.main(COMMAND) == 0


def test_a_refusal_is_one_line_and_a_failing_exit(layer, monkeypatch, capsys):
    monkeypatch.setenv("PLAYBOOKS_ANALYST_TOKEN", OWNER)

    assert main.main(COMMAND) == 1
    assert capsys.readouterr().err == "403: Only the analyst may do this.\n"


def test_a_layer_that_is_not_running_is_said(layer, monkeypatch, capsys):
    monkeypatch.setenv("PLAYBOOKS_URL", "http://127.0.0.1:9")

    assert main.main(COMMAND) == 1
    assert "cannot be reached" in capsys.readouterr().err


def test_a_failed_run_is_said_and_a_failing_exit(make_layer, monkeypatch, tmp_path, capsys):
    failing = make_layer(replies=lambda: [RuntimeError("down")])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PLAYBOOKS_URL", failing.url)
    monkeypatch.setenv("PLAYBOOKS_ANALYST_TOKEN", ANALYST)

    assert main.main(COMMAND) == 1
    assert "The model could not be called." in capsys.readouterr().out


class _Cycling:
    """The page's button and the cycle's state, as the backend would answer them in turn."""

    def __init__(self, *states: dict):
        self.states = list(states)
        self.polls = 0

    def post(self, path: str, follow_redirects: bool = False):
        return SimpleNamespace(status_code=303)

    def request(self, method: str, path: str, json=None):
        self.polls += 1
        body = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return SimpleNamespace(status_code=200, json=lambda: body)


@pytest.mark.parametrize(
    ("last", "done"),
    [
        ({"state": "stopped", "step": 3, "line": "It stopped."}, [1, 2]),
        ({"state": "done", "step": 6, "line": None}, [1, 2, 3, 4, 5, 6]),
    ],
)
def test_cycle_pauses_between_reads_and_says_done_only_of_steps_that_ended(
    monkeypatch, capsys, last, done
):
    paused = []
    monkeypatch.setattr(main.time, "sleep", paused.append)
    http = _Cycling(
        {"state": "running", "step": 1, "line": None},
        {"state": "running", "step": 3, "line": None},
        last,
    )

    state = main.cycle(http, wait=60)  # type: ignore[arg-type]

    assert state == last and http.polls == 3
    assert paused == [0.25, 0.25]
    said = capsys.readouterr().err
    assert [int(line.split(".")[0]) for line in said.splitlines()] == done
