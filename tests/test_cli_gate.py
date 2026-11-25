"""`playbooks gate`, `playbooks check` and the proposals in `playbooks record`."""

import curation
import firm
import gating
import pytest
import yaml
from layer import ANALYST, OWNER
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


@pytest.fixture
def cycle(make_layer):
    layer = make_layer(
        agent=gating.trial_agent,
        curates=lambda: [call("submit_proposal", **gating.honest())],
    )
    gating.first_cycle(layer)
    return layer


def test_check_hands_a_hostile_file_to_the_checks_and_says_which_refused_it(cycle, command):
    file = gating.HOSTILE / "guardrails-figure-in-words.yaml"

    status, said, _ = command(cycle, "check", str(file))

    assert status == 1
    assert "checked, its text kept, and never to be approved" in said
    assert ": failed" in said and "  guardrails: failed" in said and "  support: passed" in said
    assert "its line carries a number, currency or percentage. (rule financial_figure)" in said
    assert "held_out" not in said


def test_check_of_an_honest_proposal_passes(cycle, command, tmp_path):
    file = tmp_path / "honest.json"
    file.write_text(yaml.safe_dump(gating.honest()))

    status, said, _ = command(cycle, "check", str(file))

    assert status == 0, said
    for check in gating.CHECKS:
        assert f"  {check}: passed" in said


def test_check_of_a_file_that_is_no_proposal_says_so(cycle, command, tmp_path):
    file = tmp_path / "list.yaml"
    file.write_text("- one\n- two\n")
    missing = tmp_path / "missing.yaml"

    assert command(cycle, "check", str(file))[0] == 1
    assert command(cycle, "check", str(missing))[2].startswith("The file cannot be read")
    _, _, error = command(cycle, "check", str(file))
    assert error.startswith("The file holds no proposal")
    dated = tmp_path / "dated.yaml"
    dated.write_text("changes:\n- by: 2026-10-19\ndisagreements: []\n")
    status, _, error = command(cycle, "check", str(dated))
    assert status == 1 and error.startswith("The file holds a value that is no text")


def test_gate_runs_every_check_and_the_held_out_cases(cycle, command):
    proposal_id = curation.curated(cycle)["proposal"]["id"]

    status, said, _ = command(cycle, "gate", "--proposal", proposal_id)

    assert status == 0
    assert f"of proposal {proposal_id}: passed" in said
    assert "  held_out: passed" in said
    assert "  held-out case set: " in said and "expected notes by A writer of these tests" in said
    assert "  trials' model: stub:triage (calls named stub:triage)" in said
    assert "    new-rule case made-pas-b-failed: failed with the playbooks in use, " in said
    assert "  trials: 24 run, 0 missed, cost $0.000000 as the provider reported it" in said
    assert "Every check passed: the proposal is gate_passed." in said
    assert f"  playbook set: wealthcontext-playbooks version {firm.APPROVED_SET}, " in said
    status, _, error = command(cycle, "gate", "--proposal", "no-such-proposal")
    assert status == 1 and error.startswith("404: ")


def test_record_lists_every_proposal_with_its_gate_results(cycle, command):
    # Checked first: a hostile file is written for the bundle before curation cites the report.
    command(cycle, "check", str(gating.HOSTILE / "personal-name-in-a-line.yaml"))
    proposal_id = curation.curated(cycle)["proposal"]["id"]
    command(cycle, "gate", "--proposal", proposal_id)

    _, said, _ = command(cycle, "record")

    assert f"  {proposal_id}: gate_passed, curator run " in said
    assert "    gate " in said and ": passed" in said
    assert ": checked, handed in" in said
    assert "its text is not kept: it carried what the checks keep out" in said
    assert ": failed, failed: personal" in said
    assert "gate run, by analyst" in said and "proposal checked, by analyst" in said
