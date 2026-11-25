"""The record of each approval and refusal: what changed, why, on what evidence, who."""

import copy
import io

import approving
import gating
import pytest
from layer import ANALYST, running_layer
from pydantic import ValidationError

from backend import models
from cli import main

FOUR = ("what_changed", "why", "evidence", "who")


def decided_layer(folder):
    """An approval, a refusal of one that failed, one of one never gated, and one gone stale."""
    second: dict = {}
    manager = None

    def entered(**options):
        nonlocal manager
        manager = running_layer(folder, **options)
        return manager.__enter__()

    failing = gating.honest() | {"disagreements": []}
    layer = approving.scripted(entered, failing, gating.honest(), gating.honest(), lambda: second)
    assert manager is not None
    try:
        gating.first_cycle(layer)
        failed = approving.drafted(layer)
        gating.gated(layer, failed["id"])
        assert approving.refuse(layer, failed["id"], "against_the_sources").status_code == 200
        ungated = approving.drafted(layer)
        assert approving.refuse(layer, ungated["id"], "not_needed").status_code == 200
        first, first_run = approving.passed(layer)
        report = approving.another_report(layer)
        second.update(approving.resting_on(report["reference"]))
        other, other_run = approving.passed(layer)
        approving.approved(layer, first, first_run)
        assert approving.approve(layer, other, other_run).status_code == 409
        return {
            "record": gating.record(layer),
            "approved": first["id"],
            "refused": failed["id"],
            "ungated": ungated["id"],
            "stale": other["id"],
            "second": copy.deepcopy(second),
        }
    finally:
        manager.__exit__(None, None, None)


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    return decided_layer(tmp_path_factory.mktemp("decisions") / "layer")


def decision(recorded: dict, which: str) -> dict:
    (found,) = [p for p in recorded["record"]["proposals"] if p["id"] == recorded[which]]
    return found["decision"]


def rested(proposal: dict) -> set[str]:
    return {each for change in proposal["changes"] for each in change["rests_on"]}


def tells_all_four(record: dict, proposal_id: str, proposed: dict, outcomes: dict) -> None:
    """What the record must show of one decision."""
    (found,) = [each for each in record["proposals"] if each["id"] == proposal_id]
    told = found["decision"]
    assert told.get("what_changed"), "the record does not say what changed"
    assert told["what_changed"]["changes"] == proposed["changes"]
    if told["decision"] == "approved":
        assert len(told["what_changed"]["versions"]) == 2
    assert told.get("why"), "the record does not say why"
    assert {each["reference"] for each in told["why"]["rests_on"]} == rested(proposed)
    assert told.get("evidence"), "the record does not say on what evidence"
    assert {c["check"]: c["outcome"] for c in told["evidence"]["checks"]} == outcomes
    assert told.get("who") == "playbook_owner", "the record does not say who"


PASSED = dict.fromkeys([*gating.CHECKS, "held_out"], "passed")
FAILED = PASSED | {"support": "failed", "held_out": "not_run"}
EXPECTED = {
    "approved": (gating.honest(), PASSED),
    "refused": (gating.honest() | {"disagreements": []}, FAILED),
    "ungated": (gating.honest(), {}),
    "stale": (None, PASSED),
}


def expected(recorded: dict, which: str) -> tuple[dict, dict]:
    proposed, outcomes = EXPECTED[which]
    return proposed or recorded["second"], outcomes


@pytest.mark.parametrize("which", EXPECTED)
def test_the_record_shows_what_why_the_evidence_and_who(recorded, which):
    tells_all_four(recorded["record"], recorded[which], *expected(recorded, which))


@pytest.mark.parametrize("left_out", FOUR)
@pytest.mark.parametrize("which", EXPECTED)
def test_a_record_without_any_one_of_the_four_fails(recorded, which, left_out):
    broken = copy.deepcopy(recorded["record"])
    (found,) = [each for each in broken["proposals"] if each["id"] == recorded[which]]
    del found["decision"][left_out]

    with pytest.raises(AssertionError, match="the record does not say"):
        tells_all_four(broken, recorded[which], *expected(recorded, which))
    # And the backend cannot give out such a record.
    with pytest.raises(ValidationError):
        models.Decision.model_validate(found["decision"])


@pytest.mark.parametrize(
    "which, field, part",
    [
        ("approved", "what_changed", "versions"),
        ("approved", "what_changed", "changes"),
        ("approved", "why", "rests_on"),
        ("approved", "evidence", "checks"),
        ("refused", "why", "reason"),
    ],
)
def test_a_decision_with_one_of_the_four_empty_is_no_record(recorded, which, field, part):
    told = copy.deepcopy(decision(recorded, which))
    told[field][part] = None if part == "reason" else []

    with pytest.raises(ValidationError):
        models.Decision.model_validate(told)


def test_why_names_each_source_by_kind_and_hash_with_the_ruling(recorded):
    why = decision(recorded, "approved")["why"]

    kinds = {each["reference"]: each["kind"] for each in why["rests_on"]}
    assert kinds[approving.CORRECTION] == "correction"
    assert kinds[approving.GAP_REPORT] == "gap_report"
    assert kinds[approving.FOUNDING] == "memo"
    assert all(len(each["sha256"]) == 64 for each in why["rests_on"])
    assert why["rulings"] and why["reason"] is None
    assert decision(recorded, "refused")["why"]["reason"] == "against_the_sources"
    assert decision(recorded, "ungated")["evidence"]["gate_run"] is None


SAID = {
    "what": ("what changed:", "what it would have changed:"),
    "why": ("    why:",),
    "evidence": ("    evidence:",),
    "who": ("    who: playbook_owner",),
}


def printed(record: dict) -> dict[str, str]:
    """What `playbooks record` prints under each proposal, by its ID."""
    listed = main.describe_record(record).split("\nLedger:")[0].split("\n  ")
    blocks: dict[str, list[str]] = {}
    current = ""
    for part in listed:
        if part[:1].strip() and part[:32].isalnum():
            current = part[:32]
        blocks.setdefault(current, []).append(f"  {part}")
    return {name: "\n".join(lines) for name, lines in blocks.items()}


@pytest.mark.parametrize("which", EXPECTED)
def test_playbooks_record_prints_the_four(recorded, which):
    said = printed(recorded["record"])[recorded[which]]

    for wanted in SAID.values():
        assert any(each in said for each in wanted), wanted
    proposed, outcomes = expected(recorded, which)
    for change in proposed["changes"]:
        assert f"{change['change']} {change['playbook']}/{change['step']}" in said
    for reference in rested(proposed):
        assert reference in said
    for check, outcome in outcomes.items():
        assert f"      {check}: {main.SHOWN.get(outcome, outcome)}" in said


def test_the_command_prints_the_record_from_the_backend(make_layer, monkeypatch, capsys):
    layer, proposal, run = approving.first_cycle_passed(make_layer)
    approving.approved(layer, proposal, run)
    monkeypatch.setenv("PLAYBOOKS_URL", layer.url)
    monkeypatch.setenv("PLAYBOOKS_ANALYST_TOKEN", ANALYST)
    monkeypatch.setattr("sys.stdin", io.StringIO())

    assert main.main(["record"]) == 0

    said = capsys.readouterr().out
    for wanted in ("what changed:", "why:", "evidence:", "who: playbook_owner", "ruled for"):
        assert wanted in said


def test_the_printed_change_shows_a_condition_and_a_line_that_changed(recorded):
    said = printed(recorded["record"])[recorded["approved"]]

    for change in gating.HONEST["changes"]:
        assert change["line"] in said
    assert "custodian A, feed_status FAILED" in said or "feed_status FAILED, custodian A" in said
