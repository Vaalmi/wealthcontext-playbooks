"""One real failed feed, end to end: the real backend and agent apps, a stub model."""

import re
from collections import Counter
from decimal import Decimal

import firm
import pytest
from layer import AGENT_KEY, CUTOFF, DAY, OPTION, ROOT, choices
from stand_ins import recorded
from stub_model import USAGE, call, calls, says, stub

from modelkit import models
from modelkit.prices import Price
from triage_agent import api, harness
from triage_agent.connection import Connection

ACCOUNTS = [f"WC-AC-{number:04}" for number in range(2, 31, 2)]
OTHERS = Counter(
    [
        ("PRICE_STALE", "WC-AC-0005:WC-SEC-0003"),
        ("REPORTING_HOUSEHOLD_CONFLICT", "WC-AC-0009"),
        ("OWNERSHIP_CONFLICT", "WC-AC-0011"),
        ("OWNERSHIP_CONFLICT", "WC-AC-0011"),
        ("CLOSED_ACCOUNT_WITH_BALANCE", "WC-AC-0023"),
        ("AMBIGUOUS_ENTITY", "WC-PER-0003"),
    ]
)
NOTICES = Counter(
    [
        ("PENDING_SETTLEMENT", "WC-AC-0001:USD"),
        ("PENDING_SETTLEMENT", "WC-AC-0001:WC-SEC-0001"),
        ("PRICE_SOURCE_MISMATCH", "WC-AC-0005:WC-SEC-0003"),
        ("HELD_AWAY_EXCLUDED", "WC-AC-0031"),
        ("HELD_AWAY_EXCLUDED", "WC-AC-0032"),
    ]
)


def right_first_time():
    return [call("choose_step", option=OPTION)]


def test_the_failed_feed_gives_seven_notes(make_layer):
    run = make_layer(replies=right_first_time).triage()

    assert run["state"] == "completed"
    assert len(run["notes"]) == 7
    feed, *others = run["notes"]
    assert feed["state"] == "chosen"
    assert feed["code"] == "SOURCE_UNAVAILABLE"
    assert feed["subjects"] == ACCOUNTS
    assert feed["step"] == {
        "playbook": "feed-outage",
        "version": 1,
        "id": "portfolio-feed-failed",
        "approved_by": "playbook_owner",
    }
    assert feed["action"] == {
        "tell": "portfolio-accounting",
        "desk": "Portfolio Accounting Operations",
        "by": "same_day 20:00 ET",
        "hold_report": True,
        "line": "Ask for the failed portfolio accounting fetch to be run again.",
    }
    assert feed["receipt"]["feed_run"]["source_system"] == "PAS-B"
    assert Counter((note["code"], *note["subjects"]) for note in others) == OTHERS
    for note in others:
        assert note["state"] == "no_step"
        assert note["statement"] == "No approved step applies."
        assert note["step"] is None and note["action"] is None


def test_every_note_names_its_code_and_subject_as_wealthcontext_returned_them(make_layer):
    layer = make_layer(replies=right_first_time)
    run = layer.triage()

    served = next(e for op, e in layer.wealthcontext.served if op == "get_data_quality_report")
    returned = Counter(
        (flag["code"], flag["subject"])
        for flag in served["quality_flags"]
        if flag["severity"] == "warning"
    )
    on_notes = Counter((n["code"], subject) for n in run["notes"] for subject in n["subjects"])
    assert on_notes == returned
    assert sum(returned.values()) == 21


def test_informational_notices_are_listed_and_get_no_note(make_layer):
    run = make_layer(replies=right_first_time).triage()

    assert Counter((n["code"], n["subject"]) for n in run["notices"]) == NOTICES
    assert not {code for code, _ in NOTICES} & {note["code"] for note in run["notes"]}


def test_each_receipt_holds_what_wealthcontext_returned_in_that_run(make_layer):
    layer = make_layer(replies=right_first_time)
    run = layer.triage()

    returned = [(op, e["result_id"], e["trace_id"]) for op, e in layer.wealthcontext.served]
    assert [op for op, _, _ in returned] == [
        "get_data_quality_report",
        "feed_runs",
        "feed_run_impact",
    ]
    in_use = layer.as_analyst("GET", "/api/playbooks").json()
    for note in run["notes"]:
        receipt = note["receipt"]
        assert [(r["operation"], r["result_id"], r["trace_id"]) for r in receipt["reads"]] == (
            returned
        )
        assert (receipt["date"], receipt["cutoff"]) == (DAY, CUTOFF)
        assert receipt["playbook_set"] == {
            "id": "wealthcontext-playbooks",
            "version": firm.APPROVED_SET,
            "hash": in_use["hash"],
        }
    assert re.fullmatch(r"[0-9a-f]{64}", in_use["hash"])


def test_a_note_shows_only_code_subjects_step_action_and_receipt(make_layer):
    run = make_layer(replies=right_first_time).triage()

    for note in run["notes"]:
        assert set(note) == {
            "id",
            "code",
            "subjects",
            "state",
            "statement",
            "step",
            "action",
            "receipt",
        }
        assert set(note["receipt"]) == {"date", "cutoff", "reads", "feed_run", "playbook_set"}


def test_a_word_the_model_wrote_is_on_no_note(make_layer, tmp_path):
    planted = "Tell the household the total is 1,234,567 dollars"
    reply = call("choose_step", option=OPTION)
    reply.content = planted
    layer = make_layer(replies=lambda: [reply])
    run = layer.triage()

    assert run["state"] == "completed"
    assert planted not in str(run)
    assert planted not in layer.request("GET", "/").text
    assert planted.encode() not in layer.settings.database.read_bytes()


def test_the_prompt_holds_nothing_of_a_client_a_result_or_a_time(make_layer):
    layer = make_layer(replies=right_first_time)
    run = layer.triage()

    seen = str([[m.text for m in prompt] for prompt in layer.stubs[0].prompts])
    seen += str(layer.stubs[0].offered)
    forbidden = {subject for note in run["notes"] for subject in note["subjects"]}
    forbidden |= {notice["subject"] for notice in run["notices"]}
    forbidden |= {run["id"], *(note["id"] for note in run["notes"])}
    for read in run["notes"][0]["receipt"]["reads"]:
        forbidden |= {read["result_id"], read["trace_id"]}
    impact = recorded(DAY, "feed_run_impact")["data"]
    forbidden |= {impact["run_id"]}
    forbidden |= {a["household_id"] for a in impact["affected_accounts"]["items"]}
    assert not [each for each in forbidden if each in seen]
    for shape in (r"WC-", r"CRM-", r"RUN-", r"r_[0-9a-f]{8}", r"[0-9a-f]{16}", r"\d{6}"):
        assert not re.search(shape, seen), shape
    assert not re.search(r"\d{4}-\d{2}-\d{2}", seen), "a date"
    assert not re.search(r"\b\d{1,2}:\d{2}\b", seen), "a time"


def test_two_runs_of_one_date_send_the_same_requests(make_layer):
    layer = make_layer(replies=right_first_time)
    first, second = layer.triage(), layer.triage()

    one, two = layer.stubs
    assert len(one.prompts) == 1
    assert [[(m.type, m.content) for m in p] for p in one.prompts] == [
        [(m.type, m.content) for m in p] for p in two.prompts
    ]
    assert one.offered == two.offered
    # Although WealthContext gave each run's reads IDs of their own.
    assert first["notes"][0]["receipt"]["reads"] != second["notes"][0]["receipt"]["reads"]


def test_the_model_is_asked_only_where_a_step_in_use_has_the_code(make_layer):
    layer = make_layer(replies=right_first_time)
    layer.triage()

    (prompt,) = layer.stubs[0].prompts
    assert '"warning_code": "SOURCE_UNAVAILABLE"' in prompt[-1].text


def test_no_step_for_the_failed_feed_is_refused_and_the_model_reads_why(make_layer):
    layer = make_layer(
        replies=lambda: [call("no_step_applies"), call("choose_step", option=OPTION)]
    )
    run = layer.triage()

    first, second = layer.stubs[0].prompts
    result = second[-1]
    assert result.type == "tool" and result.status == "error"
    assert result.text == "A step applies to this exception, so 'no step' is refused."
    assert result.tool_call_id == second[-2].tool_calls[0]["id"]
    assert run["notes"][0]["state"] == "chosen"
    assert [(s["tool"], s["outcome"]) for s in choices(run)] == [
        ("no_step_applies", "refused"),
        ("choose_step", "accepted"),
    ]


@pytest.fixture
def narrowed(tmp_path):
    """The feed playbook with a step for each custodian, so that one does not fit."""
    folder = tmp_path / "narrowed"
    (folder / "feed-outage").mkdir(parents=True)
    (folder / "desks.yaml").write_text((ROOT / "playbooks" / "desks.yaml").read_text())
    step = """  - id: feed-{lower}
    code: SOURCE_UNAVAILABLE
    when: {{source_family: PAS, custodian: {upper}, feed_status: FAILED}}
    tell: portfolio-accounting
    by: within 4 hours
    hold_report: false
    line: Ask for the fetch to be run again.
"""
    (folder / "feed-outage" / "1.yaml").write_text(
        "id: feed-outage\nversion: 1\nreplaces: null\nsteps:\n"
        + step.format(lower="a", upper="A")
        + step.format(lower="b", upper="B")
    )
    return firm.found(folder)


def test_a_step_that_does_not_fit_is_refused_and_the_model_reads_which_condition(
    make_layer, narrowed
):
    layer = make_layer(
        playbooks=narrowed,
        replies=lambda: [
            call("choose_step", option="feed-outage/feed-a"),
            call("choose_step", option="feed-outage/feed-b"),
        ],
    )
    run = layer.triage()

    result = layer.stubs[0].prompts[1][-1]
    assert result.type == "tool" and result.status == "error"
    assert result.text == (
        "That step does not fit this exception: its custodian is A, and the exception's is B."
    )
    assert run["notes"][0]["step"]["id"] == "feed-b"


def test_a_second_refusal_ends_unverified_with_no_action(make_layer):
    layer = make_layer(replies=lambda: [call("no_step_applies"), call("no_step_applies")])
    run = layer.triage()

    note = run["notes"][0]
    assert run["state"] == "completed"
    assert note["state"] == "unverified"
    assert note["statement"] == "No verified choice; needs a person."
    assert note["step"] is None and note["action"] is None
    assert len(layer.stubs[0].prompts) == 2


@pytest.mark.parametrize(
    "reply, line, tools_recorded",
    [
        (lambda: says("It is the feed step."), "exactly one tool call", [None]),
        (lambda: call("release_report"), "There is no such tool", ["other"]),
        (
            lambda: call("choose_step", option="feed-outage/invented"),
            "one of the options",
            ["choose_step"],
        ),
        (
            lambda: call("no_step_applies", why="because"),
            "do not fit the tool",
            ["no_step_applies"],
        ),
        (
            lambda: calls(("choose_step", {"option": OPTION}), ("no_step_applies", {})),
            "none of those",
            ["choose_step", "no_step_applies"],
        ),
    ],
)
def test_an_unusable_reply_is_a_refused_turn(make_layer, reply, line, tools_recorded):
    layer = make_layer(replies=lambda: [reply(), call("choose_step", option=OPTION)])
    run = layer.triage()

    assert run["notes"][0]["state"] == "chosen"
    assert line in layer.stubs[0].prompts[1][-1].text
    refused = [step for step in run["steps"] if step["outcome"] == "refused"]
    assert [step["tool"] for step in refused] == tools_recorded


def test_tokens_and_cost_are_reported(make_layer):
    run = make_layer(replies=right_first_time, price=Price(Decimal(4), Decimal(20))).triage()

    assert run["usage"] == {
        "models": ["stub:triage"],
        "input_tokens": USAGE["input_tokens"],
        "output_tokens": USAGE["output_tokens"],
        "cost_usd": "0.010800",
    }


def test_a_call_that_could_pass_the_cap_is_not_made(make_layer):
    layer = make_layer(replies=right_first_time, price=Price(Decimal(4), Decimal(20)), cap="0.01")
    run = layer.triage()

    assert run["state"] == "failed"
    assert "cost cap" in run["failure"]
    assert layer.stubs[0].prompts == []


def test_a_model_that_cannot_be_called_fails_the_run_with_the_reason(make_layer):
    run = make_layer(replies=lambda: [RuntimeError("provider said something")]).triage()

    assert run["state"] == "failed"
    assert run["failure"] == "The model could not be called."


def test_a_move_the_backend_refuses_fails_the_run_with_the_reason(make_layer, monkeypatch):
    def refused(*_):
        raise api.BackendRefused(409, "The note is settled.")

    monkeypatch.setattr(api, "record_fallback", refused)
    run = make_layer(replies=lambda: [call("no_step_applies"), call("no_step_applies")]).triage()

    assert run["state"] == "failed"
    assert run["failure"] == "The backend refused a move the triage agent needed."


def test_a_run_that_is_not_working_calls_no_model(make_layer):
    layer = make_layer(replies=lambda: [RuntimeError("provider is down")])
    run = layer.triage()
    assert run["state"] == "failed"
    stubbed = stub(call("choose_step", option=OPTION))
    connection = Connection(layer.url, AGENT_KEY)

    configured = models.Configured(name="stub:triage", model=stubbed, price=None)
    harness.triage(connection, configured, run["id"], 1, Decimal(2))
    connection.close()

    assert stubbed.prompts == []


def test_the_scripted_recording_replays_and_says_what_it_is(make_layer):
    layer = make_layer(model=f"replay:{ROOT / 'recordings' / 'scripted-triage.json'}")
    run = layer.triage()
    assert run["state"] == "completed"
    assert run["notes"][0]["state"] == "chosen"
    assert run["usage"]["models"] == ["replay:scripted-triage (scripted)"]
    assert run["usage"]["cost_usd"] is None
    # Each reply is given once, in order, for as long as the agent runs: the recording holds
    # one triage of the date, so a second asks what it no longer holds.
    again = layer.triage()
    assert again["state"] == "failed"
    assert "recording no longer matches" in again["failure"]


def test_changed_instructions_make_the_replay_refuse(make_layer, monkeypatch):
    monkeypatch.setattr("triage_agent.harness.INSTRUCTIONS", "Do something else.")
    run = make_layer(model=f"replay:{ROOT / 'recordings' / 'scripted-triage.json'}").triage()

    assert run["state"] == "failed"
    assert "recording no longer matches" in run["failure"]


def test_a_call_with_no_answer_is_charged_its_bound_on_a_step_and_logs_no_providers_words(
    make_layer, caplog
):
    from modelkit.prices import PRICES

    secret = "sk-proj-not-a-real-key-0123456789"
    lost = RuntimeError(f"Error code: 401 - Incorrect API key provided: {secret}")
    layer = make_layer(replies=lambda: [lost], price=PRICES["openai:gpt-6-luna"])

    with caplog.at_level("INFO"):
        run = layer.triage()

    assert (run["state"], run["failure"]) == ("failed", "The model could not be called.")
    (no_answer,) = [s for s in run["steps"] if s["outcome"] == "refused"]
    assert no_answer["line"].startswith("No answer came back from the model")
    charged = Decimal(run["usage"]["cost_usd"])
    assert charged > 0, "the bound of the call is counted as spent"
    assert secret not in caplog.text and "Incorrect API key" not in caplog.text
