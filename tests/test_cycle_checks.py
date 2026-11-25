"""What `make cycle` and `make record-cycle` require of the approval's rulings, before a recording
is kept: what the first cycle's playbook owner rules, and nothing that holds only for one proposal's
shape. The records here are shaped like a live cycle's and the scripted one's."""

import pytest

from scripts import cycle

CORRECTION = "correction-hnfpafekmljplgcg"
REPORT = "gap-report-gjphhadjnfmdejde"
CROSS_CHECK = "memo-missing-cross-check-founding"
FEED = "memo-feed-outage-founding"
LATER = "memo-custodian-b-feed-escalation"
DATED = {FEED: "2026-08-03", CROSS_CHECK: "2026-08-03", LATER: "2026-10-19"}


def ruling(change: int | None, between: list[str], ruled_for: str) -> dict:
    return {"change": change, "between": between, "ruled_for": ruled_for}


# A live curator listed the custodian B memo's disagreement with the founding memo as well: the
# owner persona rules for the later memo there, and for the correction and the gap report.
LIVE = [
    ruling(1, [LATER, FEED], LATER),
    ruling(2, [LATER, FEED], LATER),
    ruling(3, [CORRECTION, CROSS_CHECK], CORRECTION),
    ruling(3, [REPORT, CROSS_CHECK], REPORT),
]
SCRIPTED = [
    ruling(3, [CORRECTION, CROSS_CHECK], CORRECTION),
    ruling(3, [REPORT, CROSS_CHECK], REPORT),
]


def faults(rulings: list[dict]) -> list[str]:
    return cycle.ruling_faults({"why": {"rulings": rulings}}, DATED, CORRECTION)


@pytest.mark.parametrize("rulings", [LIVE, SCRIPTED, list(reversed(LIVE))])
def test_rulings_as_the_first_cycles_owner_rules_hold(rulings):
    assert faults(rulings) == []


def test_a_ruling_for_the_founding_cross_check_memo_does_not_hold():
    for wrong in (
        ruling(3, [CORRECTION, CROSS_CHECK], CROSS_CHECK),
        ruling(3, [REPORT, CROSS_CHECK], CROSS_CHECK),
    ):
        rulings = [each for each in LIVE if each["between"] != wrong["between"]] + [wrong]
        assert faults(rulings), wrong


def test_a_ruling_for_any_memo_over_a_correction_or_gap_report_does_not_hold():
    rulings = [*LIVE, ruling(2, [CORRECTION, LATER], LATER)]

    assert faults(rulings)


@pytest.mark.parametrize("missing", [CORRECTION, REPORT])
def test_no_ruling_for_the_correction_or_for_the_gap_report_does_not_hold(missing):
    rulings = [each for each in LIVE if missing not in each["between"]]

    said = faults(rulings)

    assert said and all("over the founding cross-check memo" in each for each in said)


def test_no_ruling_at_all_does_not_hold():
    assert faults([])


def test_between_memos_alone_the_ruling_is_for_the_later_one():
    earlier = [*SCRIPTED, ruling(1, [LATER, FEED], FEED)]

    assert faults(earlier) == [
        f"the approval rules for {FEED} over a later memo, {LATER}, where memos alone disagree"
    ]


def test_between_memos_of_one_date_either_is_the_later_one():
    rulings = [*SCRIPTED, ruling(1, [FEED, CROSS_CHECK], FEED)]

    assert faults(rulings) == []


def test_a_ruling_for_the_gap_report_over_another_memo_does_not_stand_for_the_founding_one():
    rulings = [ruling(3, [CORRECTION, CROSS_CHECK], CORRECTION), ruling(1, [REPORT, FEED], REPORT)]

    assert faults(rulings) == [
        "the approval does not rule for a gap report over the founding cross-check memo"
    ]


def test_a_ruling_between_nothing_breaks_no_rule_and_stands_for_none():
    assert faults([*SCRIPTED, ruling(1, [], LATER)]) == []
    assert faults([ruling(3, [], CORRECTION)]) == [
        "the approval does not rule for the correction over the founding cross-check memo",
        "the approval does not rule for a gap report over the founding cross-check memo",
    ]
