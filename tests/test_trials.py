"""A held-out trial's moves, as the triage agent makes them: checked as a triage run's are."""

from typing import Literal

import pytest

from backend import models, triage
from backend.gate.trials import NO_GAP_REPORT, Option, TrialNote, Trials
from backend.playbook_format import When
from backend.refusals import Refusal
from backend.vocabulary import FeedFacts

USAGE = models.Usage(model="stub:triage", input_tokens=5, output_tokens=1, cost_usd="0.000100")
FACTS = FeedFacts(source_family="PAS", custodian="B", feed_status="FAILED")


def option(custodian: Literal["A", "B"]) -> Option:
    return Option(
        playbook="feed-outage",
        step=f"custodian-{custodian.lower()}-feed",
        code="SOURCE_UNAVAILABLE",
        when=When(source_family="PAS", custodian=custodian, feed_status="FAILED"),
        tell="portfolio-accounting",
        by="same_day 20:00 ET",
        hold_report=True,
        line="Ask for the fetch again.",
    )


def opened() -> tuple[Trials, str, str]:
    trials = Trials()
    note = TrialNote(
        id="n1",
        exception="e1",
        code="SOURCE_UNAVAILABLE",
        facts=FACTS,
        warnings=2,
        options=[option("A"), option("B")],
    )
    trial = trials.open("a-case", 1, [note])
    return trials, trial.id, note.id


def chose(step: str | None, turn: int = 1) -> models.RecordChoice:
    choice = (
        models.StepChoice(kind="step", playbook="feed-outage", step=step)
        if step
        else models.NoStepChoice(kind="no_step")
    )
    return models.RecordChoice(turn=turn, choice=choice, usage=USAGE)


def refused(call) -> tuple[int, str]:
    with pytest.raises(Refusal) as raised:
        call()
    return raised.value.status, raised.value.line


def test_the_work_shows_no_action_and_asks_no_gap_question():
    trials, run_id, _ = opened()

    work = trials.work(run_id, 1)

    (note,) = work.notes
    assert note.warning_count == 2 and note.feed == FACTS and work.uncovered == []
    assert "portfolio-accounting" not in work.model_dump_json()
    assert refused(lambda: trials.work(run_id, 2))[0] == 409


def test_a_choice_is_checked_both_ways_with_triages_own_lines():
    trials, run_id, note_id = opened()

    misfit = refused(lambda: trials.record_choice(run_id, 1, note_id, chose("custodian-a-feed")))
    none = refused(lambda: trials.record_choice(run_id, 1, note_id, chose(None, turn=2)))

    assert misfit == (
        422,
        triage.DOES_NOT_FIT + "its custodian is A, and the exception's is B.",
    )
    assert none == (422, triage.A_STEP_APPLIES)
    assert trials.work(run_id, 1).notes[0].refusals == [misfit[1], none[1]]
    assert trials.record_fallback(run_id, 1, note_id).state == "unverified"
    assert trials.complete(run_id, 1).state == "completed"
    assert trials.close(run_id).notes[0].state == "unverified"


def test_a_move_out_of_turn_counts_for_nothing_and_the_same_move_again_changes_nothing():
    trials, run_id, note_id = opened()

    out_of_turn = refused(lambda: trials.record_choice(run_id, 1, note_id, chose(None, turn=2)))
    first = trials.record_choice(run_id, 1, note_id, chose("custodian-b-feed"))
    again = trials.record_choice(run_id, 1, note_id, chose("custodian-b-feed"))

    assert out_of_turn == (409, triage.NOT_ITS_TURN)
    assert first.state == again.state == "chosen"
    assert trials.work(run_id, 1).notes == []
    trial = trials.close(run_id)
    assert [s["outcome"] for s in trial.steps] == ["refused", "accepted"]
    assert trial.notes[0].refusals == []


def test_an_unusable_reply_is_a_refused_turn_and_a_fallback_needs_two():
    trials, run_id, note_id = opened()
    reply = models.UnusableReply(turn=1, reason="no_tool_call", calls=[], usage=USAGE)

    assert refused(lambda: trials.record_fallback(run_id, 1, note_id))[0] == 409
    assert (
        trials.record_unusable_reply(run_id, 1, note_id, reply).refusal
        == (triage.UNUSABLE["no_tool_call"])
    )
    assert refused(lambda: trials.complete(run_id, 1)) == (
        409,
        "The run is working and a note is still pending.",
    )
    assert trials.fail(run_id, 1, models.FailTriage(reason="model_failed")).state == "failed"
    assert refused(lambda: trials.complete(run_id, 1)) == (409, "The run is failed.")


def test_a_trial_files_no_gap_report():
    trials, run_id, note_id = opened()

    found = refused(
        lambda: trials.refuse_gap_question(run_id, 1, note_id, "file_gap_report", 1, USAGE)
    )

    assert found == (409, NO_GAP_REPORT)
    (step,) = trials.close(run_id).steps
    assert (step["tool"], step["outcome"], step["line"]) == (
        "file_gap_report",
        "refused",
        NO_GAP_REPORT,
    )


def test_a_trial_nothing_is_heard_from_ends_stalled():
    now = [0.0]
    trials = Trials(clock=lambda: now[0])
    note = TrialNote(
        id="n1",
        exception="e1",
        code="SOURCE_UNAVAILABLE",
        facts=FACTS,
        warnings=1,
        options=[option("B")],
    )
    run_id = trials.open("a-case", 1, [note]).id
    now[0] = 301.0

    trials.wait(run_id, 300.0)

    trial = trials.close(run_id)
    assert (trial.state, trial.failure) == ("failed", "stalled")
    assert refused(lambda: trials.work(run_id, 1)) == (404, "There is no such triage run.")
