"""The commands for firm sources, corrections and gap reports, against the real backend."""

import json
import re

import pytest
from knowledge import MEMOS, chose, files_on_review
from layer import ANALYST, CROSS_CHECK_DAY, CUTOFF, OWNER, address, listen

from cli import main
from scripts import cycle

TRIAGE = ["triage", "--date", CROSS_CHECK_DAY, "--cutoff", CUTOFF]


@pytest.fixture
def command(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PLAYBOOKS_ANALYST_TOKEN", ANALYST)
    monkeypatch.setenv("PLAYBOOKS_OWNER_TOKEN", OWNER)

    def run(layer, *arguments: str) -> tuple[int, str, str]:
        monkeypatch.setenv("PLAYBOOKS_URL", layer.url)
        status = main.main(list(arguments))
        said = capsys.readouterr()
        return status, said.out, said.err

    return run


def correcting(said: str, *more: str) -> list[str]:
    run = re.search(r"^  run ([0-9a-f]{32})$", said, re.M)
    note = re.search(r"^   note ([0-9a-f]{32})$", said, re.M)
    assert run and note, said
    return [
        "correct",
        *("--run", run[1], "--note", note[1]),
        *("--field", "hold_report", "--should-be", "no"),
        *("--reason", "A missing cross-check alone is no reason to hold an advisor's report."),
        *more,
    ]


def test_intake_says_what_it_took_in(make_layer, command):
    layer = make_layer()

    status, said, _ = command(layer, "intake", str(MEMOS / "custodian-b-feed-escalation.md"))

    assert status == 0
    assert "Taken in: memo-custodian-b-feed-escalation, memo of 2026-10-19" in said
    assert '"custodian": "B"' in said
    _, record, _ = command(layer, "record")
    assert "source taken in, by playbook_owner" in record
    assert "memo-custodian-b-feed-escalation" in record


def test_intake_refused_says_the_rule(make_layer, command):
    layer = make_layer()

    status, _, error = command(layer, "intake", str(MEMOS / "agent-instructions.md"))

    assert status == 1
    assert error.startswith("422: ") and "rule addressed_to_an_agent" in error
    _, record, _ = command(layer, "record")
    assert "source refused" in record and "rule: addressed_to_an_agent" in record


def test_intake_of_no_file_is_said(make_layer, command):
    status, _, error = command(make_layer(), "intake", "there-is-no-such-file.md")

    assert (status, error) == (1, "The file cannot be read as text.\n")


def test_correct_waits_for_the_review_and_names_the_report(make_layer, command):
    layer = make_layer(replies=chose, gaps=files_on_review())
    _, triaged, _ = command(layer, *TRIAGE)

    status, said, _ = command(layer, *correcting(triaged))

    assert status == 0
    assert "reviewed" in said and "hold_report should be no" in said
    assert "gap report filed: gap-report-" in said
    status, listed, _ = command(layer, "gap-reports")
    assert status == 0 and "playbook: missing-cross-check version 1" in listed
    (report,) = json.loads(command(layer, "gap-reports", "--json")[1])["gap_reports"]
    status, said, _ = command(
        layer, "dismiss", "--gap-report", report["id"], "--reason", "duplicate"
    )
    assert status == 0 and said == f"Dismissed gap report {report['id']}: duplicate.\n"
    assert "dismissed: duplicate" in command(layer, "gap-reports")[1]


def test_correct_as_the_owner_is_403(make_layer, command):
    layer = make_layer(replies=chose)
    _, triaged, _ = command(layer, *TRIAGE)

    status, _, error = command(layer, *correcting(triaged, "--as", "owner"))

    assert (status, error) == (1, "403: Only the analyst may do this.\n")


def test_dismiss_as_the_analyst_is_403(make_layer, command):
    arguments = ["dismiss", "--gap-report", "0" * 32, "--reason", "duplicate", "--as", "analyst"]

    status, _, error = command(make_layer(), *arguments)

    assert (status, error) == (1, "403: Only the playbook owner may do this.\n")


def test_with_the_agent_down_correct_says_so_and_request_review_asks_again(make_layer, command):
    first = make_layer(replies=chose)
    _, triaged, _ = command(first, *TRIAGE)
    bound = listen()
    url = address(bound)
    bound.close()
    down = make_layer(after=first, agent_url=url)

    status, said, error = command(down, *correcting(triaged))

    assert status == 1
    assert "The correction stands" in error and "can be asked for again" in said
    found = re.match(r"Correction ([0-9a-f]{32})", said)
    assert found
    up = make_layer(after=down, replies=list, gaps=files_on_review())
    status, said, _ = command(up, "request-review", "--correction", found[1])
    assert status == 0 and "gap report filed" in said


def test_a_hold_is_yes_or_no(make_layer, command):
    arguments = correcting(f"  run {'0' * 32}\n   note {'0' * 32}\n")
    arguments[arguments.index("no")] = "maybe"

    status, _, error = command(make_layer(), *arguments)

    assert (status, error) == (1, "A hold instruction should be yes or no.\n")


def test_the_cycles_correction_is_the_committed_one(make_layer, monkeypatch):
    layer = make_layer(replies=chose, gaps=files_on_review())

    def as_the_command(*arguments: str, at: str | None = None) -> dict:
        import contextlib
        import io

        monkeypatch.setenv("PLAYBOOKS_URL", layer.url)
        monkeypatch.setenv("PLAYBOOKS_ANALYST_TOKEN", ANALYST)
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            main.main([*arguments, "--json"])
        return json.loads(said.getvalue())

    monkeypatch.setattr(cycle, "command", as_the_command)

    assert cycle.correct(layer.triage(CROSS_CHECK_DAY)) == []
