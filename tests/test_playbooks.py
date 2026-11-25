"""The playbook files, their format, and matching by fixed fields."""

import copy
import json

import firm
import pytest
import yaml
from layer import ROOT
from pydantic import ValidationError

from backend import matching, store
from backend.playbook_format import Playbook, Step, content_hash
from backend.vocabulary import FeedFacts

PLAYBOOKS = ROOT / "playbooks"


def test_both_version_one_playbooks_load_and_are_in_use(tmp_path):
    folder = firm.copy(tmp_path / "playbooks")

    with firm.opened(folder, tmp_path) as (journal, kept), journal.transaction() as connection:
        loaded = kept.current(connection)

    assert [(each.id, each.version) for each in loaded.versions] == [
        ("feed-outage", 1),
        ("missing-cross-check", 1),
    ]
    for each in loaded.versions:
        assert each.sha256 == content_hash(
            (PLAYBOOKS / each.id / f"{each.version}.yaml").read_bytes()
        )


def test_the_set_that_was_founded_keeps_its_version_and_hash(tmp_path):
    folder = firm.unapproved(firm.copy(tmp_path / "playbooks"))

    with firm.opened(folder, tmp_path) as (journal, kept), journal.transaction() as connection:
        loaded = kept.current(connection)

    assert (loaded.id, loaded.version) == ("wealthcontext-playbooks", 1)
    assert loaded.hash == "08b501bb1cd73eb1f69f4c461595e1690d39b453fed052c4990c29290dfdec90"


def test_the_committed_ledger_names_what_each_version_rests_on():
    line, approval = (PLAYBOOKS / "ledger.jsonl").read_text().splitlines()
    founded = json.loads(line)

    assert founded["event"] == "playbooks_founded"
    assert founded["data"]["id"] == "wealthcontext-playbooks"
    assert founded["data"]["approved_by"] is None, "the first line is never written again"
    approved = json.loads(approval)
    assert (approved["event"], approved["who"]) == ("founding_approved", "playbook_owner")
    assert approved["at"].startswith("2026-11-23T"), "the founding approval is dated 2026-11-23"
    assert approved["data"] == {
        "founding": {"line": 1, "hash": founded["hash"]},
        "versions": founded["data"]["versions"],
        "desks_sha256": founded["data"]["desks_sha256"],
    }
    for version in founded["data"]["versions"]:
        memo = (ROOT / "sources" / "memos" / f"{version['id']}-founding.md").read_bytes()
        front = yaml.safe_load(memo.decode().split("---\n", 2)[1])
        assert version["rests_on"] == [{"id": front["id"], "sha256": content_hash(memo)}]


@pytest.mark.parametrize("name", ["feed-outage", "missing-cross-check"])
def test_each_playbook_rests_on_a_founding_memo_with_its_scope(name):
    text = (ROOT / "sources" / "memos" / f"{name}-founding.md").read_text()
    _, front, body = text.split("---\n", 2)
    memo = yaml.safe_load(front)
    (step,) = yaml.safe_load((PLAYBOOKS / name / "1.yaml").read_text())["steps"]

    assert memo["kind"] == "memo"
    assert memo["desk"] in yaml.safe_load((PLAYBOOKS / "desks.yaml").read_text())
    assert memo["scope"] == {"playbook": name, "code": step["code"], "when": step["when"]}
    assert step["by"].split(" ")[1] in body
    assert ("Hold the advisor's report" in body) is step["hold_report"]


def a_playbook() -> dict:
    return copy.deepcopy(yaml.safe_load((PLAYBOOKS / "feed-outage" / "1.yaml").read_text()))


@pytest.mark.parametrize(
    "line",
    [
        "Pay 10 to the desk",
        "Pay ten dollars",
        "Pay ten",
        "Release half of it",
        "Waive the fee in dollars",
        "Charge a percent",
        "Charge some %",
        "Send € to the desk",
        "Wait twenty-one days",
        "Pay ｔｅｎ",
        "Tell the desk\nand release it",
        "Pay te\u034fn",
        " ",
        "x" * 161,
    ],
)
def test_a_line_holds_no_figure_and_is_one_short_line(line):
    playbook = a_playbook()
    playbook["steps"][0]["line"] = line

    with pytest.raises(ValidationError):
        Playbook.model_validate(playbook)


@pytest.mark.parametrize("line", ["Ask for no report to be sent", "Tell all of the advisors"])
def test_a_line_may_use_the_words_a_playbook_needs(line):
    playbook = a_playbook()
    playbook["steps"][0]["line"] = line

    assert Playbook.model_validate(playbook).steps[0].line == line


@pytest.mark.parametrize(
    "field, value",
    [
        ("code", "NOT_A_CODE"),
        ("when", {"source_family": "PRICES", "custodian": "any", "feed_status": "any"}),
        ("when", {"source_family": "PAS", "custodian": "any", "feed_status": "SUCCESS"}),
        ("by", "by lunchtime"),
        ("by", "same_day 24:00 ET"),
        ("by", "within 0 hours"),
        ("hold_report", "yes please"),
        ("hold_report", 1),
        ("total", "shown"),
    ],
)
def test_a_step_has_only_its_fixed_fields(field, value):
    playbook = a_playbook()
    playbook["steps"][0][field] = value

    with pytest.raises(ValidationError):
        Playbook.model_validate(playbook)


@pytest.mark.parametrize(
    "by", ["same_day 09:30 ET", "within 4 hours", "next_business_day 10:00 ET"]
)
def test_the_three_forms_of_a_deadline(by):
    playbook = a_playbook()
    playbook["steps"][0]["by"] = by

    assert Playbook.model_validate(playbook).steps[0].by == by


@pytest.mark.parametrize(
    "file, content, said",
    [
        ("desks.yaml", "pricing: Pricing Desk\n", "feed-outage/1.yaml tells a desk"),
        ("desks.yaml", "pricing: 7\n", "desks.yaml is not a valid"),
        ("desks.yaml", "[unclosed", "desks.yaml is not a valid"),
        ("feed-outage/1.yaml", "id: feed-outage\n", "feed-outage/1.yaml is not a valid"),
        ("feed-outage/1.yaml", "[unclosed", "feed-outage/1.yaml is not a valid"),
    ],
)
def test_a_folder_that_does_not_hold_is_refused_naming_the_file(tmp_path, file, content, said):
    folder = firm.copy(tmp_path / "playbooks")
    (folder / file).write_text(content)
    firm.found(folder)

    with pytest.raises(store.StoreInvalid, match=said), firm.opened(folder, tmp_path):
        pass


def test_a_file_that_names_another_version_is_refused(tmp_path):
    folder = firm.copy(tmp_path / "playbooks")
    (folder / "feed-outage" / "1.yaml").write_text(
        (PLAYBOOKS / "missing-cross-check" / "1.yaml").read_text()
    )
    firm.found(folder)

    with pytest.raises(store.StoreInvalid, match="another playbook or version"):
        with firm.opened(folder, tmp_path):
            pass


B_FAILED = FeedFacts(source_family="PAS", custodian="B", feed_status="FAILED")


def a_step(**when) -> Step:
    step = a_playbook()["steps"][0]
    everything = {"source_family": "any", "custodian": "any", "feed_status": "any"}
    return Step.model_validate({**step, "when": {**everything, **when}})


def test_a_step_that_is_all_any_fits_with_or_without_feed_facts():
    assert matching.misfit(a_step(), "SOURCE_UNAVAILABLE", None) is None
    assert matching.misfit(a_step(), "SOURCE_UNAVAILABLE", B_FAILED) is None


def test_a_fixed_value_needs_feed_facts_that_match():
    step = a_step(custodian="B")

    assert matching.misfit(step, "SOURCE_UNAVAILABLE", B_FAILED) is None
    assert matching.misfit(step, "SOURCE_UNAVAILABLE", None) == (
        "it needs custodian B, and the exception has no feed facts."
    )
    assert matching.misfit(a_step(custodian="A"), "SOURCE_UNAVAILABLE", B_FAILED) == (
        "its custodian is A, and the exception's is B."
    )


def test_a_step_never_fits_another_code():
    assert matching.misfit(a_step(), "PRICE_STALE", None) == (
        "its warning code is not the exception's."
    )
