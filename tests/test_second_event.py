"""The missing cross-check of 2026-10-09, end to end, and how warnings are grouped."""

from collections import Counter

import pytest
from layer import (
    CROSS_CHECK_DAY,
    CROSS_CHECK_OPTION,
    CUTOFF,
    DAY,
    GROUPING_DAY,
    OPTION,
    ROOT,
)
from stub_model import call

from scripts import cycle

CROSS_CHECK_STEP = {
    "playbook": "missing-cross-check",
    "version": 1,
    "id": "custody-cross-check-missing",
    "approved_by": "playbook_owner",
}
FEED_STEP = {
    "playbook": "feed-outage",
    "version": 1,
    "id": "portfolio-feed-failed",
    "approved_by": "playbook_owner",
}


def test_the_missing_cross_check_gives_six_notes(make_layer):
    layer = make_layer(replies=lambda: [call("choose_step", option=CROSS_CHECK_OPTION)])
    run = layer.triage(CROSS_CHECK_DAY)

    assert run["state"] == "completed"
    assert len(run["notes"]) == 6
    missing, *others = run["notes"]
    assert missing["state"] == "chosen"
    assert missing["code"] == "RECONCILIATION_UNAVAILABLE"
    assert missing["subjects"] == ["WC-AC-0016"]
    assert missing["step"] == CROSS_CHECK_STEP
    assert missing["action"] == {
        "tell": "custody-operations",
        "desk": "Custody Operations",
        "by": "next_business_day 10:00 ET",
        "hold_report": True,
        "line": "Ask for the missing custody positions file so the cross-check can run.",
    }
    feed = missing["receipt"]["feed_run"]
    assert (feed["source_system"], feed["feed"], feed["status"]) == (
        "CUSTODY-B",
        "custody_snapshots",
        "PARTIAL",
    )
    assert Counter((note["code"], *note["subjects"]) for note in others) == Counter(
        [
            ("REPORTING_HOUSEHOLD_CONFLICT", "WC-AC-0009"),
            ("OWNERSHIP_CONFLICT", "WC-AC-0011"),
            ("OWNERSHIP_CONFLICT", "WC-AC-0011"),
            ("CLOSED_ACCOUNT_WITH_BALANCE", "WC-AC-0023"),
            ("AMBIGUOUS_ENTITY", "WC-PER-0003"),
        ]
    )
    for note in others:
        assert note["state"] == "no_step"
        assert note["statement"] == "No approved step applies."
        assert note["step"] is None and note["action"] is None
    assert [(notice["code"], notice["subject"]) for notice in run["notices"]] == [
        ("HELD_AWAY_EXCLUDED", "WC-AC-0031"),
        ("HELD_AWAY_EXCLUDED", "WC-AC-0032"),
    ]


def test_the_scripted_recording_replays_both_dates(make_layer):
    layer = make_layer(model=f"replay:{ROOT / 'recordings' / 'scripted-triage.json'}")

    events = (
        (DAY, 7, FEED_STEP, cycle.FAILED_FEED, cycle.MISSING_CROSS_CHECK),
        (CROSS_CHECK_DAY, 6, CROSS_CHECK_STEP, cycle.MISSING_CROSS_CHECK, cycle.FAILED_FEED),
    )
    for day, count, step, event, other in events:
        run = layer.triage(day, CUTOFF)
        assert cycle.differences(run, event) == []
        assert cycle.differences(run, other)
        assert run["state"] == "completed"
        assert len(run["notes"]) == count
        assert [note["step"] for note in run["notes"] if note["state"] == "chosen"] == [step]


@pytest.fixture
def grouped(make_layer):
    layer = make_layer(
        replies=lambda: (
            [
                # One for each exception that has a step to choose among.
                lambda: answer_for(layer.stubs[-1].prompts[-1][-1].text),
            ]
            * 4
        )
    )
    return layer, layer.triage(GROUPING_DAY)


def answer_for(asked: str):
    if '"feed_facts": null' in asked:
        return call("no_step_applies")
    if '"source_family": "PAS"' in asked.split('"steps"')[0]:
        return call("choose_step", option=OPTION)
    return call("choose_step", option=CROSS_CHECK_OPTION)


def notes_naming(run: dict, account: str) -> list[dict]:
    return [note for note in run["notes"] if account in note["subjects"]]


def shown(note: dict) -> tuple:
    feed = note["receipt"]["feed_run"]
    return note["code"], feed["source_system"] if feed else None, note["state"]


def test_a_failed_feeds_account_with_a_conflict_gets_two_notes(grouped):
    _, run = grouped

    found = notes_naming(run, "WC-AC-0002")

    assert Counter(map(shown, found)) == Counter(
        [("SOURCE_UNAVAILABLE", "PAS-B", "chosen"), ("OWNERSHIP_CONFLICT", None, "no_step")]
    )
    conflict = next(note for note in found if note["code"] == "OWNERSHIP_CONFLICT")
    assert conflict["subjects"] == ["WC-AC-0002"]
    assert conflict["action"] is None


def test_an_account_in_two_failed_feeds_appears_in_each_feeds_note(grouped):
    _, run = grouped

    found = notes_naming(run, "WC-AC-0004")

    assert Counter(map(shown, found)) == Counter(
        [
            ("SOURCE_UNAVAILABLE", "PAS-B", "chosen"),
            ("RECONCILIATION_UNAVAILABLE", "CUSTODY-B", "chosen"),
        ]
    )
    by_code = {note["code"]: note for note in found}
    assert by_code["SOURCE_UNAVAILABLE"]["subjects"] == ["WC-AC-0002", "WC-AC-0004"]
    assert by_code["SOURCE_UNAVAILABLE"]["step"] == FEED_STEP
    assert by_code["RECONCILIATION_UNAVAILABLE"]["subjects"] == ["WC-AC-0004", "WC-AC-0008"]
    assert by_code["RECONCILIATION_UNAVAILABLE"]["step"] == CROSS_CHECK_STEP


@pytest.mark.parametrize(
    "account, code",
    [
        # Its source is CRM, though the failed portfolio feed affected the account.
        ("WC-AC-0006", "SOURCE_UNAVAILABLE"),
        # Its reason is ambiguous transactions, though the failed custody feed affected it.
        ("WC-AC-0010", "RECONCILIATION_UNAVAILABLE"),
    ],
)
def test_a_warning_the_feed_did_not_produce_is_a_note_by_itself(grouped, account, code):
    _, run = grouped

    (note,) = notes_naming(run, account)

    assert shown(note) == (code, None, "no_step")
    assert note["subjects"] == [account]
    assert note["action"] is None


def test_the_grouped_day_has_one_note_for_each_exception(grouped):
    layer, run = grouped

    assert run["state"] == "completed"
    assert len(run["notes"]) == 5
    assert len(layer.stubs[0].prompts) == 4
    served = next(e for op, e in layer.wealthcontext.served if op == "get_data_quality_report")
    assert Counter((n["code"], s) for n in run["notes"] for s in n["subjects"]) == Counter(
        (flag["code"], flag["subject"]) for flag in served["quality_flags"]
    )
