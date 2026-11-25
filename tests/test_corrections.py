"""A correction is a checked record, and its review is the triage agent's."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import firm
import pytest
from knowledge import (
    BREAKS,
    HOLD_NOT,
    REASON,
    SUMMARY,
    USAGE,
    chose,
    chosen,
    committed_correction,
    correct,
    corrected,
    correction,
    events,
    files_on_review,
    gap_reports,
    ledger,
    review,
    review_ended,
    settled_run,
    stored,
    uncovered,
)
from layer import CROSS_CHECK_DAY, OWNER, ROOT, address, listen
from stand_ins import idle_agent
from stub_model import call, says

from backend import triage


@pytest.fixture
def idle(make_layer):
    return make_layer(agent=lambda *_: idle_agent())


@pytest.fixture
def later(monkeypatch):
    ahead = [timedelta(0)]
    monkeypatch.setattr(triage, "_clock", lambda: datetime.now(UTC) + ahead[0])

    def pass_time(**of) -> None:
        ahead[0] += timedelta(**of)

    return pass_time


def nowhere() -> str:
    bound = listen()
    url = address(bound)
    bound.close()
    return url


def again(layer, correction_id: str):
    return layer.as_analyst("POST", f"/api/corrections/{correction_id}/request-review")


def agent_move(layer, correction_id: str, rest: str, body=None, attempt: int = 1):
    path = f"/api/agent/corrections/{correction_id}/attempts/{attempt}/{rest}"
    return layer.as_agent("POST", path, json=body)


# Recording it


def test_a_correction_is_fixed_fields_and_one_reason(idle):
    run = settled_run(idle)

    made = corrected(idle, run)

    assert (made["state"], made["attempt"], made["asked"]) == ("recorded", 1, "accepted")
    (source,) = idle.as_analyst("GET", "/api/sources").json()["sources"]
    committed = committed_correction()
    assert {name: source[name] for name in committed} == committed
    assert made["source_id"] == source["id"] == committed["id"]
    line = ledger(idle)[-1]
    assert (line["event"], line["who"]) == ("correction_recorded", "analyst")
    assert set(line["data"]) == {"correction_id", "run_id", "note_id", "source"}
    assert set(line["data"]["source"]) == {
        "id",
        "sha256",
        "kind",
        "date",
        "desk",
        "scope",
        "correction",
        "text",
    }
    assert line["data"]["source"]["correction"] == {
        "version": 1,
        "step": "custody-cross-check-missing",
        "field": "hold_report",
        "should_be": False,
    }
    assert not re.search(r"WC-|CRM-", (idle.settings.playbooks / "ledger.jsonl").read_text())


def test_it_reads_the_chosen_step_where_the_note_gives_no_action(make_layer, tmp_path):
    layer = make_layer(
        playbooks=firm.unapproved(firm.copy(tmp_path / "unapproved")), agent=lambda *_: idle_agent()
    )
    run = settled_run(layer)
    assert chosen(run)["action"] is None

    same = {"change": {"field": "hold_report", "should_be": True}, "reason": "It is held."}
    assert correct(layer, run, same).status_code == 422
    assert corrected(layer, run)["state"] == "recorded"


@pytest.mark.parametrize(
    "change",
    [
        {"field": "tell", "should_be": "custodian-b-service"},
        {"field": "by", "should_be": "within 4 hours"},
        {"field": "hold_report", "should_be": False},
    ],
)
def test_each_of_the_three_fields_can_be_corrected(idle, change):
    made = corrected(idle, settled_run(idle), {"change": change, "reason": "The rule is wrong."})

    found = correction(idle, made["id"])
    assert (found["field"], found["should_be"]) == (change["field"], change["should_be"])


@pytest.mark.parametrize(
    "change",
    [
        {"field": "tell", "should_be": "quillon-vantree"},
        {"field": "tell", "should_be": "custody-operations"},
        {"field": "by", "should_be": "by lunchtime"},
        {"field": "by", "should_be": "next_business_day 10:00 ET"},
        {"field": "hold_report", "should_be": "no"},
        {"field": "hold_report", "should_be": 0},
        {"field": "hold_report", "should_be": True},
        {"field": "line", "should_be": "Tell Quillon Vantree"},
        {"field": "hold_report", "should_be": False, "transcript": "They said no"},
    ],
)
def test_a_change_out_of_its_closed_form_or_that_changes_nothing_is_refused(idle, change):
    response = correct(idle, settled_run(idle), {"change": change, "reason": REASON})

    assert response.status_code == 422
    assert events(idle) == firm.FOUNDING_EVENTS
    assert b"vantree" not in stored(idle).lower()


def test_the_playbook_owner_records_no_correction(idle):
    run = settled_run(idle)
    path = f"/api/triage-runs/{run['id']}/notes/{chosen(run)['id']}/corrections"

    response = idle.as_owner("POST", path, json=HOLD_NOT)

    assert response.status_code == 403
    assert idle.as_agent("POST", path, json=HOLD_NOT).status_code == 401
    assert events(idle) == firm.FOUNDING_EVENTS


@pytest.mark.parametrize("ends", ["working", "failed"])
def test_a_run_that_is_not_completed_is_not_corrected(idle, ends):
    run = settled_run(idle, complete=False)
    if ends == "failed":
        path = f"/api/agent/triage-runs/{run['id']}/attempts/1/fail"
        assert idle.as_agent("POST", path, json={"reason": "cost_cap"}).status_code == 200

    response = correct(idle, run)

    assert response.status_code == 409
    assert f"The run is {ends}" in response.json()["detail"]
    assert events(idle) == firm.FOUNDING_EVENTS


def test_a_note_with_no_step_chosen_is_not_corrected(idle):
    run = settled_run(idle)

    response = correct(idle, run, note_id=uncovered(run)[0])

    assert response.status_code == 409
    assert "has no step chosen" in response.json()["detail"]
    assert events(idle) == firm.FOUNDING_EVENTS


def test_a_note_of_another_run_is_not_found(idle):
    first, second = settled_run(idle), settled_run(idle)

    assert correct(idle, first, note_id=chosen(second)["id"]).status_code == 404
    unknown = first | {"id": "0" * 32}
    assert correct(idle, unknown).status_code == 404


REASONS = BREAKS | {
    "over length": ("x" * 301, "too_long"),
    "two lines": ("Hold it.\nOr not.", "not_plain_text"),
}


@pytest.mark.parametrize("text, rule", REASONS.values(), ids=list(REASONS))
def test_a_reason_that_breaks_a_rule_is_refused(idle, text, rule):
    response = correct(idle, settled_run(idle), HOLD_NOT | {"reason": text})

    assert response.status_code == 422
    assert f"(rule {rule})" in response.json()["detail"]
    assert text not in response.text
    assert idle.as_analyst("GET", "/api/sources").json() == {"sources": []}
    assert idle.as_analyst("GET", "/api/corrections").json() == {"corrections": []}
    line = ledger(idle)[-1]
    assert (line["event"], line["data"]["rule"]) == ("source_refused", rule)
    assert line["data"]["kind"] == "correction"
    assert (
        text.split("\n")[0].encode() not in (idle.settings.playbooks / "ledger.jsonl").read_bytes()
    )


def test_the_same_correction_again_is_the_same(idle):
    run = settled_run(idle)
    first = corrected(idle, run)

    second = corrected(idle, run)

    assert (second["id"], second["asked"]) == (first["id"], "not_asked")
    assert events(idle) == [*firm.FOUNDING_EVENTS, "correction_recorded"]


def test_one_content_on_two_runs_is_one_source_and_two_records(idle):
    first, second = corrected(idle, settled_run(idle)), corrected(idle, settled_run(idle))

    assert first["id"] != second["id"]
    assert first["source_id"] == second["source_id"]
    assert len(idle.as_analyst("GET", "/api/sources").json()["sources"]) == 1
    assert len(idle.as_analyst("GET", "/api/corrections").json()["corrections"]) == 2


def test_a_correction_moves_no_playbook_set(idle):
    before = idle.as_analyst("GET", "/api/playbooks").json()
    run = settled_run(idle)
    working = idle.start()["id"]

    made = corrected(idle, run)
    assert review(idle, made["id"]).status_code == 200

    assert idle.as_analyst("GET", "/api/playbooks").json() == before
    assert idle.as_agent("GET", f"/api/agent/triage-runs/{working}/attempts/1").status_code == 200


# The agent's review


def test_the_agent_files_one_gap_report_naming_playbook_version_code_and_trace(make_layer):
    layer = make_layer(replies=chose, gaps=files_on_review())
    run = layer.triage(CROSS_CHECK_DAY)

    found = review_ended(layer, corrected(layer, run)["id"])

    (report,) = gap_reports(layer)
    served = [e for op, e in layer.wealthcontext.served if op == "get_data_quality_report"]
    assert report["trace_id"] == served[-1]["trace_id"]
    assert report["trace_id"] == run["notes"][0]["receipt"]["reads"][0]["trace_id"]
    assert (report["playbook"], report["version"]) == ("missing-cross-check", 1)
    assert report["code"] == "RECONCILIATION_UNAVAILABLE"
    assert report["feed"] == {
        "source_family": "CUSTODY",
        "custodian": "B",
        "feed_status": "PARTIAL",
    }
    assert (report["summary"], report["state"]) == (SUMMARY, "open")
    assert report["correction_id"] == found["id"]
    assert (found["state"], found["outcome"]) == ("reviewed", "filed")
    assert found["gap_report"] == report["reference"]
    assert [(s["tool"], s["outcome"]) for s in found["steps"]] == [("file_gap_report", "accepted")]
    assert found["usage"]["input_tokens"]


def test_the_agent_can_file_none(make_layer):
    layer = make_layer(replies=chose)

    found = review_ended(layer, corrected(layer, layer.triage(CROSS_CHECK_DAY))["id"])

    assert (found["state"], found["outcome"], found["gap_report"]) == ("reviewed", "none", None)
    assert gap_reports(layer) == []


def test_the_scripted_recording_replays_the_review(make_layer):
    layer = make_layer(model=f"replay:{ROOT / 'recordings' / 'scripted-triage.json'}")
    run = layer.triage(CROSS_CHECK_DAY)

    found = review_ended(layer, corrected(layer, run)["id"])

    assert found["state"] == "reviewed" and found["gap_report"]
    assert found["source_id"] == committed_correction()["id"]


def test_the_model_is_shown_no_identifier_trace_or_time_and_no_current_action(make_layer):
    layer = make_layer(replies=chose, gaps=files_on_review())
    run = layer.triage(CROSS_CHECK_DAY)
    made = corrected(layer, run)
    review_ended(layer, made["id"])

    seen = "".join(
        str([[message.text for message in prompt] for prompt in stub.gap_prompts])
        + str(stub.gap_offered)
        for stub in layer.stubs
    )
    forbidden = {run["id"], made["id"], *(note["id"] for note in run["notes"])}
    forbidden |= {subject for note in run["notes"] for subject in note["subjects"]}
    for read in run["notes"][0]["receipt"]["reads"]:
        forbidden |= {read["result_id"], read["trace_id"]}
    assert not [each for each in forbidden if each in seen]
    for shape in (r"WC-", r"CRM-", r"RUN-", r"[0-9a-f]{16}", r"\d{4}-\d{2}-\d{2}", r"\d:\d\d"):
        assert not re.search(shape, seen), shape
    assert "Custody Operations" not in seen and "custody-operations" not in seen


def test_two_runs_reviews_send_the_same_request(make_layer):
    layer = make_layer(replies=chose)
    asked = []
    for _ in range(2):
        review_ended(layer, corrected(layer, layer.triage(CROSS_CHECK_DAY))["id"])
        (prompt,) = layer.stubs[-1].gap_prompts
        asked.append([(message.type, message.content) for message in prompt])

    assert asked[0] == asked[1]


def test_a_refused_summary_goes_back_to_the_model_once(make_layer):
    def answer(asked: list):
        if '"correction"' not in asked[1].text:
            return call("no_gap_report")
        refused = any(message.type == "tool" for message in asked)
        return call("file_gap_report", summary=SUMMARY if refused else "It was WC-AC-0016 again.")

    layer = make_layer(replies=chose, gaps=answer)
    found = review_ended(layer, corrected(layer, layer.triage(CROSS_CHECK_DAY))["id"])

    result = layer.stubs[-1].gap_prompts[1][-1]
    assert result.status == "error" and "client identifier" in result.text
    assert found["outcome"] == "filed"
    assert [step["outcome"] for step in found["steps"]] == ["refused", "accepted"]
    ledgered = (layer.settings.playbooks / "ledger.jsonl").read_text()
    assert "WC-AC-0016" not in ledgered and "gap_report_refused" in ledgered


@pytest.mark.parametrize(
    "wrong",
    [
        lambda: call("file_gap_report", summary="It was WC-AC-0016 again."),
        lambda: says("It is a gap."),
        lambda: call("release_report"),
        lambda: call("file_gap_report", summary=7),
        lambda: call("file_gap_report", summary="\ud800"),
        lambda: call("file_gap_report", summary=SUMMARY, trace_id="0" * 32),
        lambda: call("choose_step", option="missing-cross-check/custody-cross-check-missing"),
    ],
)
def test_a_second_refusal_ends_the_review_with_no_report(make_layer, wrong):
    def answer(asked: list):
        return wrong() if '"correction"' in asked[1].text else call("no_gap_report")

    layer = make_layer(replies=chose, gaps=answer)
    found = review_ended(layer, corrected(layer, layer.triage(CROSS_CHECK_DAY))["id"])

    assert (found["state"], found["outcome"]) == ("reviewed", "refused_twice")
    assert len(layer.stubs[-1].gap_prompts) == 2
    assert [step["outcome"] for step in found["steps"]] == ["refused", "refused"]
    assert gap_reports(layer) == []
    assert len(layer.as_analyst("GET", "/api/sources").json()["sources"]) == 1


def test_a_review_that_stops_says_why_and_can_be_asked_for_again(make_layer):
    given = []

    def answer(asked: list):
        if '"correction"' not in asked[1].text:
            return call("no_gap_report")
        given.append(True)
        return RuntimeError("down") if len(given) == 1 else call("file_gap_report", summary=SUMMARY)

    layer = make_layer(replies=chose, gaps=answer)
    made = corrected(layer, layer.triage(CROSS_CHECK_DAY))
    stopped = review_ended(layer, made["id"])

    assert (stopped["state"], stopped["asked"]) == ("recorded", False)
    assert stopped["stopped"] == "The model could not be called."

    for _ in range(200):
        asking = again(layer, made["id"])
        if asking.status_code == 200:
            break
        assert "already working" in asking.text
    found = review_ended(layer, made["id"])
    assert (found["state"], found["stopped"]) == ("reviewed", None)
    assert found["attempt"] >= 2


# The agent down


def test_with_the_agent_down_the_correction_stands_and_request_review_asks_again(make_layer):
    first = make_layer(replies=chose)
    run = first.triage(CROSS_CHECK_DAY)
    down = make_layer(after=first, agent_url=nowhere())

    made = corrected(down, run)

    assert (made["state"], made["asked"]) == ("recorded", "not_started")
    assert made["why"] == "The triage agent cannot be reached."
    (source,) = down.as_analyst("GET", "/api/sources").json()["sources"]
    assert source["id"] == made["source_id"]
    assert not correction(down, made["id"])["asked"]

    still_down = again(down, made["id"])
    assert still_down.status_code == 503
    assert "The correction stands" in still_down.json()["detail"]
    spent = correction(down, made["id"])
    assert (spent["attempt"], spent["asked"]) == (2, False)

    up = make_layer(after=down, replies=list, gaps=files_on_review())
    asked = again(up, made["id"])

    assert asked.status_code == 200 and asked.json()["attempt"] == 3
    found = review_ended(up, made["id"])
    assert (found["state"], found["outcome"]) == ("reviewed", "filed")
    assert len(gap_reports(up)) == 1


def test_an_agent_with_no_model_does_not_undo_the_correction(make_layer):
    first = make_layer(agent=lambda *_: idle_agent())
    run = settled_run(first)
    layer = make_layer(after=first, model="")

    made = corrected(layer, run)

    assert made["asked"] == "not_started" and "has no model" in made["why"]
    assert correction(layer, made["id"])["state"] == "recorded"


def test_nothing_heard_for_five_minutes_and_not_before(idle, later):
    made = corrected(idle, settled_run(idle))

    early = again(idle, made["id"])
    assert early.status_code == 409 and "has not stalled" in early.json()["detail"]
    later(minutes=4, seconds=50)
    assert again(idle, made["id"]).status_code == 409
    later(seconds=10)
    assert correction(idle, made["id"])["stalled"]
    assert again(idle, made["id"]).json()["attempt"] == 2
    assert again(idle, made["id"]).status_code == 409


def test_a_refused_call_is_hearing_from_the_agent(idle, later):
    made = corrected(idle, settled_run(idle))
    later(minutes=4)
    assert review(idle, made["id"], "It was WC-AC-0016 again.").status_code == 422
    later(minutes=2)

    assert not correction(idle, made["id"])["stalled"]
    assert again(idle, made["id"]).status_code == 409


def test_a_reviewed_correction_is_not_reviewed_again(idle, later):
    made = corrected(idle, settled_run(idle))
    assert review(idle, made["id"], None).status_code == 200
    later(minutes=6)

    for response in (again(idle, made["id"]), review(idle, made["id"], turn=2)):
        assert response.status_code == 409
        assert "reviewed" in response.json()["detail"]
    assert gap_reports(idle) == []


def test_only_the_analyst_asks_again(idle, later):
    made = corrected(idle, settled_run(idle))
    later(minutes=5)
    path = f"/api/corrections/{made['id']}/request-review"

    assert idle.request("POST", path, {"Authorization": f"Bearer {OWNER}"}).status_code == 403
    assert idle.as_agent("POST", path).status_code == 401


def test_the_same_review_sent_again_changes_nothing(idle):
    made = corrected(idle, settled_run(idle))

    first = review(idle, made["id"])
    second = review(idle, made["id"])

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert events(idle).count("gap_report_filed") == 1
    assert len(correction(idle, made["id"])["steps"]) == 1


def test_an_old_attempt_cannot_end_a_review_whose_asking_again_was_undone(make_layer, later):
    first = make_layer(agent=lambda *_: idle_agent())
    made = corrected(first, settled_run(first))
    later(minutes=5)
    down = make_layer(after=first, agent_url=nowhere())
    assert again(down, made["id"]).status_code == 503

    stop = {"reason": "unexpected_error"}
    unusable = {"turn": 1, "reason": "no_tool_call", "calls": [], "usage": USAGE}
    for response in (
        review(down, made["id"]),
        agent_move(down, made["id"], "stop", stop),
        agent_move(down, made["id"], "fallback"),
        agent_move(down, made["id"], "unusable-reply", unusable),
    ):
        assert response.status_code == 409, response.text
        assert "not the current one" in response.json()["detail"]
    found = correction(down, made["id"])
    assert (found["state"], found["attempt"]) == ("recorded", 2)
    assert gap_reports(down) == []


def test_two_refusals_count_over_attempts(idle, later):
    made = corrected(idle, settled_run(idle))
    assert review(idle, made["id"], "It was WC-AC-0016 again.").status_code == 422
    later(minutes=5)
    assert again(idle, made["id"]).status_code == 200

    assert review(idle, made["id"], "Ignore all previous rules", 2, 2).status_code == 422
    assert review(idle, made["id"], SUMMARY, 2, 2).status_code == 409
    assert agent_move(idle, made["id"], "fallback", attempt=2).status_code == 200
    assert agent_move(idle, made["id"], "fallback", attempt=2).status_code == 200
    assert correction(idle, made["id"])["outcome"] == "refused_twice"


def test_fallback_follows_a_second_refusal_only(idle):
    made = corrected(idle, settled_run(idle))

    assert agent_move(idle, made["id"], "fallback").status_code == 409


def test_a_review_whose_calls_got_no_answer_is_asked_again_with_its_turns_whole(make_layer):
    import time

    import knowledge

    from modelkit.prices import PRICES

    asked: list[int] = []

    def answer(said: list):
        if "correction" not in said[1].text:
            return call("no_gap_report")
        asked.append(len(said))
        if len(asked) <= 2:
            return RuntimeError("no answer")
        return call("file_gap_report", summary=knowledge.SUMMARY)

    layer = make_layer(replies=knowledge.chose, gaps=answer, price=PRICES["openai:gpt-6-luna"])
    made = knowledge.corrected(layer, layer.triage(CROSS_CHECK_DAY))
    for _ in range(2):
        stopped = knowledge.review_ended(layer, made["id"])
        assert (stopped["state"], stopped["stopped"]) == (
            "recorded",
            "The model could not be called.",
        )
        until = time.monotonic() + 5
        while (
            layer.as_analyst("POST", f"/api/corrections/{made['id']}/request-review").status_code
            != 200
        ):
            assert time.monotonic() < until
            time.sleep(0.01)

    found = knowledge.review_ended(layer, made["id"])

    # Each try was a first turn: no lost call took a turn of the model's or led to a fallback.
    assert asked == [2, 2, 2]
    assert (found["state"], found["outcome"]) == ("reviewed", "filed") and found["gap_report"]
    assert Decimal(found["usage"]["cost_usd"]) > 0, "the two lost calls' bounds are counted"
