"""`playbooks curate`, against the real backend and curator."""

import json

import curation
import pytest
from layer import ANALYST, OWNER, address, listen
from stand_ins import idle_agent
from stub_model import call

from cli import main


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


def test_curate_records_and_prints_the_manifest_and_the_proposal(make_layer, command):
    sent = {}
    layer = make_layer(
        agent=lambda *_: idle_agent(),
        curates=lambda: [lambda: call("submit_proposal", **sent)],
    )
    report = curation.first_cycle(layer)["report"]
    sent |= curation.proposed(report["reference"])

    status, said, _ = command(layer, "curate")

    assert status == 0
    assert "completed" in said and "  bundle " in said
    for shown in (
        f"firm_source {curation.MEMO}: ",
        f"gap_report {report['reference']}: ",
        "playbooks_in_use playbooks-in-use: ",
        "triage_outcome_summary triage-outcome-summary: ",
        "drafted, not in use",
        "2. add feed-outage/custodian-b-feed-failed, version 1",
        "3. remove missing-cross-check/custody-cross-check-missing, version 1",
        "disagreement between",
        "Tokens: 1200 in, 300 out",
        "Time: ",
    ):
        assert shown in said
    _, reports, _ = command(layer, "gap-reports")
    assert "cited, gap-report-" in reports and "cited by proposal: " in reports
    _, record, _ = command(layer, "record")
    assert "curator run started, by playbook_owner" in record
    assert "proposal recorded, by curator" in record


def test_no_change_is_a_success_and_says_so(make_layer, command):
    layer = make_layer(curates=lambda: [call("no_change")])

    status, said, _ = command(layer, "curate")

    assert status == 0
    assert "completed_no_change" in said and "No change, and no proposal." in said


def test_curate_as_json(make_layer, command):
    layer = make_layer(curates=lambda: [call("no_change")])

    status, said, _ = command(layer, "curate", "--json")

    found = json.loads(said)
    assert status == 0 and found["state"] == "completed_no_change"
    assert len(found["bundle"]["items"]) == 2


def test_curate_as_the_analyst_is_403(make_layer, command):
    status, _, error = command(make_layer(), "curate", "--as", "analyst")

    assert (status, error) == (1, "403: Only the playbook owner may do this.\n")


def test_curate_with_the_curator_down_is_503(make_layer, command):
    bound = listen()
    url = address(bound)
    bound.close()

    status, _, error = command(make_layer(curator_url=url), "curate")

    assert status == 1
    assert error == "503: The curator cannot be reached. The run was undone.\n"


def test_a_failed_run_exits_one_and_says_why(make_layer, command):
    layer = make_layer(curates=lambda: [RuntimeError("down")])

    status, said, _ = command(layer, "curate")

    assert status == 1 and "The model could not be called." in said


def test_a_stalled_run_is_started_again_by_the_same_command(make_layer, command, monkeypatch):
    later = curation.later(monkeypatch)
    layer = make_layer(curator=curation.idle)
    curation.started(layer)
    later(minutes=5)

    status, said, error = command(layer, "curate", "--wait", "0")

    assert status == 1 and "working" in said
    assert "Started the stalled run again: attempt 2." in error
