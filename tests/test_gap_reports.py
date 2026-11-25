"""Gap reports: the model gives a summary, and the backend gives everything else."""

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import firm
import pytest
from knowledge import (
    BREAKS,
    SUMMARY,
    USAGE,
    chose,
    corrected,
    events,
    file_report,
    gap_reports,
    ledger,
    review,
    secret_of,
    settled_run,
    stored,
    uncovered,
)
from layer import AGENT_KEY, CROSS_CHECK_DAY, CUTOFF
from stand_ins import idle_agent
from stub_model import call, says, stub

from backend import triage
from modelkit import models
from modelkit.prices import Price
from triage_agent import harness
from triage_agent.connection import Connection


@pytest.fixture
def idle(make_layer):
    return make_layer(agent=lambda *_: idle_agent())


@pytest.fixture
def working(idle):
    run = idle.run(idle.start(CROSS_CHECK_DAY)["id"])
    return run["id"], uncovered(run)


def files_on_conflict(asked: list):
    if "OWNERSHIP_CONFLICT" in asked[1].text:
        return call("file_gap_report", summary="No playbook says who handles conflicting owners.")
    return call("no_gap_report")


def dismiss(layer, report_id: str, reason: str = "not_a_gap"):
    path = f"/api/gap-reports/{report_id}/dismiss"
    return layer.as_owner("POST", path, json={"reason": reason})


# For an exception no playbook covers


def test_for_an_exception_no_playbook_covers_the_report_names_none(make_layer):
    layer = make_layer(replies=chose, gaps=files_on_conflict)

    run = layer.triage(CROSS_CHECK_DAY)

    assert run["state"] == "completed"
    # Two exceptions, one trace, code and subject: one report.
    (report,) = gap_reports(layer)
    assert (report["playbook"], report["version"]) == (None, None)
    assert (report["code"], report["feed"]) == ("OWNERSHIP_CONFLICT", None)
    assert report["trace_id"] == run["notes"][0]["receipt"]["reads"][0]["trace_id"]
    assert report["run_id"] == run["id"]
    assert report["note_id"] in uncovered(run)
    assert report["correction_id"] is None
    assert events(layer).count("gap_report_filed") == 1
    filed = [step for step in run["steps"] if step["tool"] == "file_gap_report"]
    assert [step["option"] for step in filed] == [report["reference"]] * 2
    assert all(step["outcome"] == "accepted" for step in filed)
    # The note stays as the backend settled it, with nothing the model wrote.
    assert all(note["state"] == "no_step" for note in run["notes"][1:])
    assert report["summary"] not in json.dumps(run["notes"])


def test_the_model_is_asked_only_whether_to_file_and_only_where_none_covers(make_layer):
    layer = make_layer(replies=chose)

    run = layer.triage(CROSS_CHECK_DAY)

    asked = layer.stubs[0].gap_prompts
    assert len(asked) == 5
    for prompt in asked:
        shown = json.loads(prompt[1].text)
        assert list(shown) == ["exception"]
        assert shown["exception"]["warning_code"] != "RECONCILIATION_UNAVAILABLE"
    for offered in layer.stubs[0].gap_offered:
        assert [t["function"]["name"] for t in offered] == ["file_gap_report", "no_gap_report"]
    assert sum(step["tool"] == "no_gap_report" for step in run["steps"]) == 5


def test_two_runs_ask_the_same_and_show_no_identifier_or_time(make_layer):
    layer = make_layer(replies=chose)

    run = layer.triage(CROSS_CHECK_DAY)
    layer.triage(CROSS_CHECK_DAY)

    first, second = (
        [[(message.type, message.content) for message in prompt] for prompt in stub.gap_prompts]
        for stub in layer.stubs
    )
    assert first == second
    seen = str(first) + str(layer.stubs[0].gap_offered)
    forbidden = {run["id"], *(note["id"] for note in run["notes"])}
    forbidden |= {subject for note in run["notes"] for subject in note["subjects"]}
    for read in run["notes"][0]["receipt"]["reads"]:
        forbidden |= {read["result_id"], read["trace_id"]}
    assert not [each for each in forbidden if each in seen]
    for shape in (r"WC-", r"CRM-", r"RUN-", r"[0-9a-f]{16}", r"\d{4}-\d{2}-\d{2}", r"\d:\d\d"):
        assert not re.search(shape, seen), shape


@pytest.mark.parametrize(
    "wrong",
    [
        lambda: call("file_gap_report", summary="It is WC-AC-0011 again."),
        lambda: says("It is a gap."),
        lambda: call("choose_step", option="feed-outage/portfolio-feed-failed"),
    ],
)
def test_two_refusals_file_none_and_the_run_still_completes(make_layer, wrong):
    def answer(asked: list):
        return wrong() if "AMBIGUOUS_ENTITY" in asked[1].text else call("no_gap_report")

    layer = make_layer(replies=chose, gaps=answer)

    run = layer.triage(CROSS_CHECK_DAY)

    (note_id,) = uncovered(run, "AMBIGUOUS_ENTITY")
    taken = [step for step in run["steps"] if step["note_id"] == note_id]
    assert run["state"] == "completed"
    assert [(step["turn"], step["outcome"]) for step in taken] == [(1, "refused"), (2, "refused")]
    assert gap_reports(layer) == []


def test_a_refused_filing_goes_back_to_the_model_with_the_rule(make_layer):
    def answer(asked: list):
        if "AMBIGUOUS_ENTITY" not in asked[1].text:
            return call("no_gap_report")
        if any(message.type == "tool" for message in asked):
            return call("file_gap_report", summary="No playbook covers an ambiguous entity.")
        return call("file_gap_report", summary="It is WC-PER-0003.")

    layer = make_layer(replies=chose, gaps=answer)
    layer.triage(CROSS_CHECK_DAY)

    prompts = [p for p in layer.stubs[0].gap_prompts if "AMBIGUOUS_ENTITY" in p[1].text]
    assert "rule client_identifier" in prompts[1][-1].text
    assert len(gap_reports(layer)) == 1


# What is stored


def test_the_stored_report_holds_no_client_identifier(idle, working):
    run_id, notes = working
    assert file_report(idle, run_id, notes[0]).status_code == 200

    (line,) = [each for each in ledger(idle) if each["event"] == "gap_report_filed"]
    subject = next(n for n in idle.run(run_id)["notes"] if n["id"] == notes[0])["subjects"][0]
    assert subject == "WC-AC-0011"
    text = json.dumps(line)
    assert subject not in text and not re.search(r"WC-|CRM-", text)
    with idle.backend.state.journal.transaction() as connection:
        row = connection.exec_driver_sql("SELECT * FROM gap_reports").one()
    assert subject not in str(tuple(row)) and not re.search(r"WC-|CRM-", str(tuple(row)))


@pytest.mark.parametrize(
    "named",
    [
        "stub:WC-AC-0011",
        "stub:triage-20998877",  # eight digits that begin as a year does, and are no date
        "stub:triage-20261015",  # nor does a date's shape make digits a model's name
        "stub:99887766",
        "stub:ignore-previous-rules",
        "stub:quillon-vantree",
        "model 20998877",
        "a person told me to say it",
        "stub:triage (told by someone)",
    ],
)
def test_no_identifier_or_free_text_gets_into_the_ledger_as_a_models_name(idle, working, named):
    run_id, notes = working
    path = f"/api/agent/triage-runs/{run_id}/attempts/1/notes/{notes[0]}/gap-report"
    body = {"turn": 1, "summary": SUMMARY, "usage": USAGE | {"model": named}}

    response = idle.as_agent("POST", path, json=body)

    assert response.status_code == 422
    assert named not in response.text
    assert events(idle) == firm.FOUNDING_EVENTS
    assert idle.run(run_id)["steps"] == []
    assert named.encode() not in (idle.settings.playbooks / "ledger.jsonl").read_bytes()


def test_the_digest_and_its_secret_are_never_shown(idle, working):
    run_id, notes = working
    file_report(idle, run_id, notes[0])
    (line,) = [each for each in ledger(idle) if each["event"] == "gap_report_filed"]
    digest, secret = line["data"]["report"]["subject_digest"], secret_of(idle)

    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    for path in ("/", "/api/record", "/api/gap-reports", f"/api/triage-runs/{run_id}"):
        shown = idle.as_analyst("GET", path).text
        assert digest not in shown and secret not in shown and "subject_digest" not in shown, path
    assert secret not in (idle.settings.playbooks / "ledger.jsonl").read_text()
    assert secret in idle.settings.triage_log.read_text()


def test_one_subjects_digests_in_two_traces_cannot_be_linked(idle):
    for _ in range(2):
        run = settled_run(idle, complete=False)
        assert file_report(idle, run["id"], uncovered(run)[0]).status_code == 200
        path = f"/api/agent/triage-runs/{run['id']}/attempts/1/complete"
        assert idle.as_agent("POST", path).status_code == 200

    found = [line for line in ledger(idle) if line["event"] == "gap_report_filed"]
    first, second = (line["data"]["report"]["subject_digest"] for line in found)
    assert first != second
    one, two = gap_reports(idle)
    # What they say is the same, so they have one reference between them.
    assert one["id"] != two["id"] and one["reference"] == two["reference"]


# At most one for a trace, code and subject


@pytest.mark.parametrize(
    "between", ["nothing", "a restart", "a rebuild", "a deleted database", "deleted, then rebuilt"]
)
def test_filing_again_returns_the_first(make_layer, idle, working, between):
    run_id, notes = working
    first = file_report(idle, run_id, notes[0]).json()
    layer = idle
    if between in ("a restart", "a deleted database"):
        if between == "a deleted database":
            idle.settings.database.unlink()
        layer = make_layer(after=idle, agent=lambda *_: idle_agent())
    if between == "deleted, then rebuilt":
        idle.settings.database.unlink()
    if between in ("a rebuild", "deleted, then rebuilt"):
        assert idle.as_owner("POST", "/api/rebuild").status_code == 200

    other_exception = file_report(layer, run_id, notes[1], "Said another way.")
    same_exception = file_report(layer, run_id, notes[0], "Said a third way.")

    assert other_exception.status_code == same_exception.status_code == 200
    assert other_exception.json() == same_exception.json() == first
    assert len(gap_reports(layer)) == 1
    assert gap_reports(layer)[0]["summary"] == SUMMARY
    assert events(layer).count("gap_report_filed") == 1


def test_filing_again_after_the_run_ended_returns_the_first(idle):
    run = settled_run(idle, complete=False)
    note = uncovered(run)[0]
    first = file_report(idle, run["id"], note).json()
    path = f"/api/agent/triage-runs/{run['id']}/attempts/1/complete"
    assert idle.as_agent("POST", path).status_code == 200

    assert file_report(idle, run["id"], note, "Said again.").json() == first
    assert len(gap_reports(idle)) == 1


def test_filing_again_after_it_was_dismissed_returns_it_and_opens_nothing(idle, working):
    run_id, notes = working
    first = file_report(idle, run_id, notes[0]).json()
    assert dismiss(idle, first["id"]).status_code == 200

    again = file_report(idle, run_id, notes[1]).json()

    assert (again["id"], again["state"]) == (first["id"], "dismissed")
    assert len(gap_reports(idle)) == 1


def test_two_corrections_of_one_note_share_the_first_report(idle):
    run = settled_run(idle)
    one = corrected(idle, run)
    other = {"change": {"field": "by", "should_be": "within 4 hours"}, "reason": "That is late."}
    two = corrected(idle, run, other)

    first = review(idle, one["id"]).json()
    second = review(idle, two["id"], "Said another way.").json()

    assert first["gap_report"] == second["gap_report"]
    assert len(gap_reports(idle)) == 1


def test_the_same_filing_sent_again_is_one_step(idle, working):
    run_id, notes = working

    assert file_report(idle, run_id, notes[0]).json() == file_report(idle, run_id, notes[0]).json()
    assert len(idle.run(run_id)["steps"]) == 1


# Refusals


SUMMARIES = BREAKS | {
    "over length": ("x" * 301, "too_long"),
    "two lines": ("No playbook.\nNone at all.", "not_plain_text"),
}


@pytest.mark.parametrize("text, rule", SUMMARIES.values(), ids=list(SUMMARIES))
def test_a_summary_that_breaks_a_rule_is_refused_with_a_line(idle, working, text, rule):
    run_id, notes = working

    response = file_report(idle, run_id, notes[0], text)

    assert response.status_code == 422
    assert f"(rule {rule})" in response.json()["detail"]
    assert text not in response.text
    assert gap_reports(idle) == []
    line = ledger(idle)[-1]
    assert (line["event"], line["data"]["rule"]) == ("gap_report_refused", rule)
    kept = text.split("\n")[0].encode()
    assert kept not in (idle.settings.playbooks / "ledger.jsonl").read_bytes()
    if "WC-" not in text:  # the triage log and the database name accounts of their own
        assert kept not in stored(idle)
    (step,) = idle.run(run_id)["steps"]
    assert (step["tool"], step["outcome"]) == ("file_gap_report", "refused")


@pytest.mark.parametrize("text, rule", SUMMARIES.values(), ids=list(SUMMARIES))
def test_a_reviews_summary_that_breaks_a_rule_is_refused_with_a_line(idle, text, rule):
    made = corrected(idle, settled_run(idle))

    response = review(idle, made["id"], text)

    assert response.status_code == 422
    assert f"(rule {rule})" in response.json()["detail"]
    assert gap_reports(idle) == []
    assert ledger(idle)[-1]["data"]["rule"] == rule
    assert (
        text.split("\n")[0].encode() not in (idle.settings.playbooks / "ledger.jsonl").read_bytes()
    )


def test_a_run_that_is_not_recorded_here_is_409(idle, working):
    _, notes = working

    response = file_report(idle, "f" * 32, notes[0])

    assert response.status_code == 409
    assert response.json() == {"detail": "That run is not one recorded here."}
    assert gap_reports(idle) == [] and events(idle) == firm.FOUNDING_EVENTS


def test_a_run_that_was_undone_is_not_recorded_here(make_layer):
    layer = make_layer(model="")
    body = {"date": CROSS_CHECK_DAY, "cutoff": CUTOFF}
    assert layer.as_analyst("POST", "/api/triage-runs", json=body).status_code == 503
    logged = json.loads(layer.settings.triage_log.read_text().splitlines()[0])["data"]

    response = file_report(layer, logged["run_id"], logged["exceptions"][1]["id"])

    assert response.status_code == 409
    assert gap_reports(layer) == []


def test_a_note_with_steps_files_only_through_a_correction(idle, working):
    run_id, _ = working
    note = uncovered(idle.run(run_id), "RECONCILIATION_UNAVAILABLE")[0]

    assert file_report(idle, run_id, note).status_code == 409
    assert gap_reports(idle) == []


def test_a_note_of_another_run_is_not_found(idle, working):
    _, notes = working
    other = idle.start()["id"]

    assert file_report(idle, other, notes[0]).status_code == 404


@pytest.mark.parametrize(
    "turn, attempt, said", [(2, 1, "not its next turn"), (1, 2, "not the current one")]
)
def test_a_filing_out_of_its_turn_or_attempt_is_409(idle, working, turn, attempt, said):
    run_id, notes = working

    response = file_report(idle, run_id, notes[0], turn=turn, attempt=attempt)

    assert response.status_code == 409 and said in response.json()["detail"]
    assert gap_reports(idle) == []


def test_once_answered_none_is_filed(idle, working):
    run_id, notes = working
    path = f"/api/agent/triage-runs/{run_id}/attempts/1/notes/{notes[0]}/no-gap-report"
    body = {"turn": 1, "usage": USAGE}
    assert idle.as_agent("POST", path, json=body).status_code == 200

    response = file_report(idle, run_id, notes[0])

    assert response.status_code == 409 and "already answered" in response.json()["detail"]
    assert gap_reports(idle) == []
    asking = idle.as_agent("GET", f"/api/agent/triage-runs/{run_id}/attempts/1").json()
    assert notes[0] not in [note["id"] for note in asking["uncovered"]]


def test_after_the_playbooks_changed_no_report_says_that_none_covers(idle, working):
    run_id, notes = working
    assert idle.retire("missing-cross-check", 1).status_code == 200

    assert file_report(idle, run_id, notes[0]).status_code == 409
    assert gap_reports(idle) == []


# A run's stall, with filings in it


def test_a_filing_is_hearing_from_the_agent(idle, monkeypatch):
    ahead = [timedelta(0)]
    monkeypatch.setattr(triage, "_clock", lambda: datetime.now(UTC) + ahead[0])
    run = idle.run(idle.start(CROSS_CHECK_DAY)["id"])
    ahead[0] = timedelta(minutes=4)
    assert file_report(idle, run["id"], uncovered(run)[0]).status_code == 200
    ahead[0] = timedelta(minutes=6)

    assert not idle.run(run["id"])["stalled"]
    body = {"date": CROSS_CHECK_DAY, "cutoff": CUTOFF}
    assert idle.as_analyst("POST", "/api/triage-runs", json=body).status_code == 409
    ahead[0] = timedelta(minutes=9)
    assert idle.run(run["id"])["stalled"]


def test_a_run_started_again_asks_only_what_has_no_answer_and_counts_what_was_spent(
    idle, monkeypatch
):
    ahead = [timedelta(0)]
    monkeypatch.setattr(triage, "_clock", lambda: datetime.now(UTC) + ahead[0])
    run = idle.run(idle.start(CROSS_CHECK_DAY)["id"])
    note = uncovered(run)[0]
    assert file_report(idle, run["id"], note).status_code == 200
    ahead[0] = timedelta(minutes=6)
    assert idle.start(CROSS_CHECK_DAY)["attempt"] == 2

    work = idle.as_agent("GET", f"/api/agent/triage-runs/{run['id']}/attempts/2").json()

    assert note not in [each["id"] for each in work["uncovered"]]
    assert len(work["uncovered"]) == 4
    assert work["spent_usd"] == USAGE["cost_usd"]


def test_an_agent_whose_attempt_is_over_pays_for_no_question_more(idle, monkeypatch):
    run = settled_run(idle, complete=False)
    connection = Connection(idle.url, AGENT_KEY)
    ahead = [timedelta(0)]
    monkeypatch.setattr(triage, "_clock", lambda: datetime.now(UTC) + ahead[0])

    def answer(_: list):
        if not ahead[0]:
            ahead[0] = timedelta(minutes=6)
            assert idle.start(CROSS_CHECK_DAY)["attempt"] == 2
        return call("no_gap_report")

    stubbed = stub(gaps=answer)
    configured = models.Configured("stub:triage", stubbed, Price(Decimal(4), Decimal(20)))
    harness.triage(connection, configured, run["id"], 1, Decimal(2))
    connection.close()

    assert len(stubbed.gap_prompts) == 2


# Dismissing


def test_the_playbook_owner_dismisses_an_open_report(idle, working):
    run_id, notes = working
    report = file_report(idle, run_id, notes[0]).json()

    response = dismiss(idle, report["id"], "already_covered")

    assert response.status_code == 200
    assert (response.json()["state"], response.json()["dismissed"]) == (
        "dismissed",
        "already_covered",
    )
    assert gap_reports(idle)[0]["state"] == "dismissed"
    line = ledger(idle)[-1]
    assert (line["event"], line["who"]) == ("gap_report_dismissed", "playbook_owner")
    assert line["data"] == {"gap_report_id": report["id"], "reason": "already_covered"}


def test_the_analyst_dismisses_none(idle, working):
    run_id, notes = working
    report = file_report(idle, run_id, notes[0]).json()
    path, body = f"/api/gap-reports/{report['id']}/dismiss", {"reason": "not_a_gap"}

    assert idle.as_analyst("POST", path, json=body).status_code == 403
    assert idle.as_agent("POST", path, json=body).status_code == 401
    assert gap_reports(idle)[0]["state"] == "open"


def test_dismissing_is_idempotent_and_final(idle, working):
    run_id, notes = working
    report = file_report(idle, run_id, notes[0]).json()

    first = dismiss(idle, report["id"])
    assert dismiss(idle, report["id"]).json() == first.json()
    other = dismiss(idle, report["id"], "duplicate")

    assert other.status_code == 409
    assert other.json() == {"detail": "The gap report is dismissed."}
    assert events(idle).count("gap_report_dismissed") == 1


def test_a_dismissals_reason_is_from_the_list(idle, working):
    run_id, notes = working
    report = file_report(idle, run_id, notes[0]).json()

    assert dismiss(idle, report["id"], "Quillon Vantree said so").status_code == 422
    assert dismiss(idle, "0" * 32).status_code == 404
