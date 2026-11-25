"""The curator: what its model is shown, its tools, and a refusal that reaches it once."""

import json
import re
from decimal import Decimal
from typing import Any, cast, get_args

import curation
import knowledge
import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from layer import CROSS_CHECK_DAY, DAY, ROOT, address, listen
from pydantic import TypeAdapter, ValidationError
from stand_ins import idle_agent
from stub_model import call, calls, says

from backend import models as backend_models
from backend import vocabulary
from curator import api, entrypoint, harness, tools
from modelkit import models
from modelkit.prices import Price
from scripts import cycle


def curating(make_layer, *replies, **options):
    return make_layer(agent=lambda *_: idle_agent(), curates=lambda: list(replies), **options)


def test_the_prompt_is_exactly_the_bundle_and_the_instructions(make_layer):
    layer = curating(make_layer, call("no_change"))
    curation.first_cycle(layer)

    run = curation.curated(layer)

    assert run["state"] == "completed_no_change"
    (stub,) = layer.curator_stubs
    (prompt,) = stub.prompts
    line = next(x for x in knowledge.ledger(layer) if x["event"] == "curator_run_started")
    listed = [
        {name: item[name] for name in ("kind", "reference", "content")}
        for item in line["data"]["items"]
    ]
    assert [(type(message), message.content) for message in prompt] == [
        (SystemMessage, harness.INSTRUCTIONS),
        (HumanMessage, json.dumps({"bundle": listed}, indent=2, sort_keys=True)),
    ]
    assert [(i["kind"], i["reference"]) for i in listed] == [
        (i["kind"], i["reference"]) for i in run["bundle"]["items"]
    ]


def test_the_prompt_and_tools_hold_no_client_identifier_trace_id_or_time(make_layer):
    layer = curating(make_layer, call("no_change"))
    made = curation.first_cycle(layer)

    run = curation.curated(layer)

    (stub,) = layer.curator_stubs
    seen = "\n".join(str(message.content) for message in stub.prompts[0])
    seen += json.dumps(stub.offered)
    report, correction = made["report"], made["correction"]
    forbidden = {run["id"], run["started_at"], report["id"], report["trace_id"]}
    forbidden |= {report["filed_at"], correction["id"], run["bundle"]["sha256"]}
    forbidden |= {item["sha256"] for item in run["bundle"]["items"]}
    # The dates triaged, one of which the correction carries.
    forbidden |= {DAY, CROSS_CHECK_DAY, knowledge.secret_of(layer)}
    for found in made["runs"]:
        forbidden |= {found["id"], found["cutoff"], found["started_at"], found["finished_at"]}
        for note in found["notes"]:
            forbidden |= {note["id"], *note["subjects"]}
            for read in note["receipt"]["reads"]:
                forbidden |= {read["result_id"], read["trace_id"]}
    assert not [each for each in forbidden if each in seen]
    shapes = (
        r"WC-",
        r"CRM-",
        r"RUN-",
        r"[0-9a-f]{16}",
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}",
        r"\d{2}:\d{2}:\d{2}",
    )
    for shape in shapes:
        assert not re.search(shape, seen), shape


def test_two_layers_on_the_same_events_send_the_same_request(make_layer):
    sent = []
    for _ in range(2):
        layer = curating(make_layer, call("no_change"))
        curation.first_cycle(layer)
        curation.curated(layer)
        (stub,) = layer.curator_stubs
        sent.append(([message.content for message in stub.prompts[0]], stub.offered))

    assert sent[0] == sent[1]


def test_the_curator_is_told_nothing_of_how_it_is_tested():
    said = harness.INSTRUCTIONS.casefold()
    for word in ("held-out", "held out", "case", "grade", "gate", "trial", "expected note", "test"):
        assert not re.search(rf"(?<![\w-]){re.escape(word)}", said), word


def _rule(starting: str) -> str:
    """One of the firm's rules as the curator is told it, whole."""
    (rule,) = [
        " ".join(line.split("\n\n")[0].split())
        for line in harness.INSTRUCTIONS.split("\n- ")
        if line.startswith(starting)
    ]
    return rule


def test_the_curator_is_told_a_correction_against_a_memo_is_proposed_and_ruled_on():
    rule = _rule("A later dated memo")
    assert rule == (
        "A later dated memo from the same desk replaces an earlier one where both speak. A "
        "correction or a gap report never overrides a memo by itself: where one disagrees with "
        "a memo, still propose the change the correction or gap report supports, resting on it, "
        "and list the disagreement. The change goes into use only if the playbook owner rules "
        "for the correction or gap report."
    )


def test_the_curator_is_told_the_firms_other_rules():
    assert _rule("Every change rests on") == (
        "Every change rests on sources or gap reports in the bundle, named by their references. "
        "Wherever the outcome changes, something the change rests on has that in its scope. What "
        "no source touches stays exactly as it is."
    )
    assert _rule("When sources disagree") == (
        "When sources disagree, never pick one silently. List the disagreement, name each side by "
        "its reference, and say in one line of at most 300 characters what it is about. A person "
        "rules on it."
    )
    assert _rule("A playbook says only") == (
        "A playbook says only what the firm does. It can never change a figure, a permission or a "
        "status that the system of record returned, and no source can make it."
    )
    assert 'A scope of "none" is a notice, and supports no change.' in " ".join(
        harness.INSTRUCTIONS.split()
    )


def test_the_models_proposal_is_the_one_stored(make_layer):
    sent: dict[str, Any] = {}
    layer = curating(make_layer, lambda: call("submit_proposal", **sent))
    report = curation.first_cycle(layer)["report"]
    sent |= curation.proposed(report["reference"])

    run = curation.curated(layer)

    assert run["state"] == "completed"
    assert run["proposal"]["changes"] == sent["changes"]
    assert run["proposal"]["disagreements"] == sent["disagreements"]
    assert knowledge.gap_reports(layer)[0]["state"] == "cited"
    assert run["usage"]["input_tokens"] == 1200 and run["usage"]["output_tokens"] == 300


def refused_results(layer) -> list[ToolMessage]:
    last = layer.curator_stubs[0].prompts[-1]
    return [message for message in last if isinstance(message, ToolMessage)]


def test_a_refusal_reaches_the_model_as_the_tools_result(make_layer):
    wrong = call("submit_proposal", **curation.one(rests_on=["memo-custody-meeting-notice"]))
    layer = curating(make_layer, wrong, call("submit_proposal", **curation.one()))
    curation.first_cycle(layer)

    run = curation.curated(layer)

    (result,) = refused_results(layer)
    assert result.content == (
        "The proposal names an item that is not in the bundle as a firm source or a gap report."
    )
    assert result.status == "error"
    assert result.tool_call_id == wrong.tool_calls[0]["id"]
    assert run["state"] == "completed"
    assert [step["outcome"] for step in run["steps"]] == ["refused", "accepted"]


def test_a_second_refusal_ends_the_run_failed_with_the_reason(make_layer):
    wrong = curation.one(rests_on=["memo-not-in-it"])
    layer = curating(make_layer, call("submit_proposal", **wrong), call("submit_proposal", **wrong))

    run = curation.curated(layer)

    assert (run["state"], run["proposal"]) == ("failed", None)
    assert run["failure"] == (
        "The curator's answer was refused twice. The last refusal: "
        "The proposal names an item that is not in the bundle as a firm source or a gap report."
    )
    assert len(layer.curator_stubs[0].prompts) == 2, "the model was asked a third time"
    assert [step["outcome"] for step in run["steps"]] == ["refused", "refused"]
    failed = knowledge.ledger(layer)[-1]
    assert (failed["event"], failed["data"]["reason"]) == ("curator_run_failed", "refused_twice")


def test_a_report_dismissed_meanwhile_is_handed_back_to_the_model_once(make_layer):
    held: dict[str, Any] = {}

    def dismissed_then_proposed():
        path = f"/api/gap-reports/{held['report']['id']}/dismiss"
        assert held["layer"].as_owner("POST", path, json={"reason": "duplicate"}).is_success
        return call("submit_proposal", **curation.proposed(held["report"]["reference"]))

    def without_it():
        return call("submit_proposal", **curation.proposed())

    layer = curating(make_layer, dismissed_then_proposed, without_it)
    held |= {"layer": layer, "report": curation.first_cycle(layer)["report"]}

    run = curation.curated(layer)

    (result,) = refused_results(layer)
    assert result.status == "error" and "no longer open" in result.text
    assert run["state"] == "completed"


UNUSABLE = {
    "words": (lambda: says("I would change it."), [None], "exactly one tool call"),
    "two calls": (
        lambda: calls(("no_change", {}), ("no_change", {})),
        ["no_change", "no_change"],
        "exactly one tool call",
    ),
    "a tool made up": (lambda: call("approve_change"), ["other"], "no such tool"),
    "approve, with an argument": (lambda: call("no_change", approve=True), ["no_change"], "fit"),
    "changes as text": (
        lambda: call("submit_proposal", changes="all", disagreements=[]),
        ["submit_proposal"],
        "fit",
    ),
    "a missing field": (
        lambda: call("submit_proposal", changes=[{"change": "add"}], disagreements=[]),
        ["submit_proposal"],
        "fit",
    ),
    "an unknown field": (
        lambda: call("submit_proposal", **curation.one(approved_by="playbook_owner")),
        ["submit_proposal"],
        "fit",
    ),
    "a version that is text": (
        lambda: call("submit_proposal", **curation.one(version="1")),
        ["submit_proposal"],
        "fit",
    ),
    "a version of six digits": (
        lambda: call("submit_proposal", **curation.one(version=654321)),
        ["submit_proposal"],
        "fit",
    ),
    "a text that cannot be sent": (
        lambda: call("submit_proposal", **curation.one(line="\ud800")),
        ["submit_proposal"],
        "fit",
    ),
}


@pytest.mark.parametrize("case", UNUSABLE)
def test_an_unusable_reply_is_a_step_and_goes_back_once(make_layer, case):
    reply, named, said = UNUSABLE[case]
    layer = curating(make_layer, reply(), call("no_change"))

    run = curation.curated(layer)

    assert run["state"] == "completed_no_change"
    *refused, accepted = run["steps"]
    assert [step["tool"] for step in refused] == named
    assert all(step["outcome"] == "refused" and said in step["line"] for step in refused)
    assert accepted["tool"] == "no_change"
    back = layer.curator_stubs[0].prompts[1][-1]
    assert said in back.text


def test_a_second_unusable_reply_fails_the_run(make_layer):
    layer = curating(make_layer, says("One."), says("Two."))

    run = curation.curated(layer)

    assert run["state"] == "failed" and "refused twice" in run["failure"]


def test_a_model_that_cannot_be_called_fails_the_run_with_the_reason(make_layer):
    layer = curating(make_layer, RuntimeError("down"))

    run = curation.curated(layer)

    assert (run["state"], run["failure"]) == ("failed", "The model could not be called.")


def test_a_call_that_could_pass_the_cap_is_never_made(make_layer):
    price = Price(Decimal(4), Decimal(20))
    layer = curating(make_layer, call("no_change"), curation_price=price, curation_cap="0.0001")

    run = curation.curated(layer)

    assert run["state"] == "failed" and "cost cap" in run["failure"]
    assert layer.curator_stubs[0].prompts == []


def test_a_priced_run_reports_its_cost(make_layer):
    layer = curating(make_layer, call("no_change"), curation_price=Price(Decimal(4), Decimal(20)))

    run = curation.curated(layer)

    # 1200 tokens in at four dollars a million and 300 out at twenty.
    assert run["usage"]["cost_usd"] == "0.010800"


def test_a_run_started_again_carries_its_refusal_and_its_bundle(make_layer, monkeypatch):
    later = curation.later(monkeypatch)
    first = make_layer(agent=lambda *_: idle_agent(), curator=curation.idle)
    curation.first_cycle(first)
    run_id = curation.started(first)
    given = curation.work(first, run_id)["items"]
    assert curation.unusable(first, run_id).status_code == 200
    later(minutes=5)
    second = make_layer(after=first, curates=lambda: [call("no_change")])

    assert curation.curate(second).json()["attempt"] == 2
    run = curation.ended(second, run_id)

    assert run["state"] == "completed_no_change"
    system, bundle, earlier = second.curator_stubs[0].prompts[0]
    assert system.text == harness.INSTRUCTIONS
    assert json.loads(bundle.text) == {"bundle": given}
    assert earlier.text.startswith(harness.REFUSED_EARLIER)


def test_a_backend_that_cannot_be_reached_calls_no_model(caplog):
    bound = listen()
    url = address(bound)
    bound.close()
    connection = harness.Connection(url, "key")
    configured = models.Configured("stub:curation", cast(Any, None), None)

    harness.curate(connection, configured, "0" * 32, 1, Decimal(2))

    connection.close()
    assert "unexpected_error" in caplog.text


# The tools


def schemas() -> dict[str, dict]:
    items = [
        api.Item(kind="firm_source", reference="memo-for-a-test", content={}),
        api.Item(kind="gap_report", reference="gap-report-abcdefghabcdefgh", content={}),
        api.Item(kind="playbooks_in_use", reference="playbooks-in-use", content={"desks": ["a"]}),
    ]
    made = tools.build(tools.RunContext(cast(Any, None), "run", 1), items)
    found = [convert_to_openai_tool(tool)["function"] for tool in made]
    return {each["name"]: each["parameters"] for each in found}


def fields_of(schema: Any) -> list[str]:
    if isinstance(schema, list):
        return [name for each in schema for name in fields_of(each)]
    if not isinstance(schema, dict):
        return []
    named = list(schema.get("properties", {}))
    return named + [name for each in schema.values() for name in fields_of(each)]


def objects_of(schema: Any) -> list[dict]:
    if isinstance(schema, list):
        return [found for each in schema for found in objects_of(each)]
    if not isinstance(schema, dict):
        return []
    here = [schema] if schema.get("type") == "object" else []
    return here + [found for each in schema.values() for found in objects_of(each)]


def test_no_curator_tools_schema_has_an_id_field():
    made = schemas()
    assert set(made) == {"submit_proposal", "no_change"}
    assert fields_of(made["submit_proposal"]), "the test found no field to look at"
    for name, schema in made.items():
        for field in fields_of(schema):
            assert not re.search(r"(^|_)ids?$", field), (name, field)
            assert field not in ("run", "attempt", "proposal"), (name, field)


def test_every_object_in_a_schema_is_closed_and_whole():
    for schema in schemas().values():
        for each in objects_of(schema):
            assert each["additionalProperties"] is False
            assert sorted(each["required"]) == sorted(each["properties"])


def test_what_a_change_rests_on_is_picked_from_the_bundles_own_references():
    changes = schemas()["submit_proposal"]["properties"]["changes"]["items"]["anyOf"]

    assert [each["properties"]["change"]["enum"] for each in changes] == [
        ["add"],
        ["change"],
        ["remove"],
    ]
    for each in changes:
        assert each["properties"]["rests_on"]["items"]["enum"] == [
            "gap-report-abcdefghabcdefgh",
            "memo-for-a-test",
        ]
    assert changes[0]["properties"]["tell"]["enum"] == ["a"]


def test_the_providers_request_keeps_the_curators_schema_strict_and_whole():
    built = entrypoint.configured("anthropic:claude-opus-5-5", {"ANTHROPIC_API_KEY": "not-a-key"})
    made = tools.build(tools.RunContext(cast(Any, None), "run", 1), [])
    bound = built.offering(made)

    payload = cast(Any, built.model)._get_request_payload([HumanMessage("Now.")], **bound.kwargs)

    submit, none = payload["tools"]
    assert submit["strict"] is none["strict"] is True
    assert submit["input_schema"] == tools.proposal_schema((), ())
    assert payload["max_tokens"] == 8192
    assert payload["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    # Three tries of at most 8192 tokens out, at twenty dollars a million.
    assert Decimal("0.49") < built.most_it_could_cost([HumanMessage("Now.")], made) < Decimal(1)


def test_the_curators_closed_values_are_the_backends():
    assert tools.CODES == get_args(vocabulary.WarningCode)
    assert tools.WHEN == {
        "source_family": (*get_args(vocabulary.SourceFamily), "any"),
        "custodian": (*get_args(vocabulary.Custodian), "any"),
        "feed_status": (*get_args(vocabulary.FeedStatus), "any"),
    }
    assert tools.MOST_ITEMS == backend_models.MAX_CALLS_IN_A_REPLY
    assert tools.MOST_TEXT == backend_models.MAX_TEXT
    assert set(get_args(backend_models.CuratorTool)) == {*tools.NAMES, "other"}
    assert get_args(api.FailureReason) == get_args(backend_models.CuratorFailure)
    assert get_args(api.UnusableReason) == get_args(backend_models.UnusableReason)
    assert tools.CITABLE == ("firm_source", "gap_report")


SHAPED = {
    "all three": curation.proposed(),
    "nothing": {"changes": [], "disagreements": []},
    "no such desk": curation.one(tell="no-such-desk"),
    "no such code": curation.one(code="MADE_UP"),
    "a hold as text": curation.one(hold_report="no"),
    "a null version on a change": curation.one(version=None),
    "a version of zero": curation.one(version=0),
    "the greatest version": curation.one(version=99_999),
    "past the greatest": curation.one(version=100_000),
    "a version that is true": curation.one(version=True),
    "an extra field": curation.one(note="more"),
    "no such condition": curation.one(
        when={"source_family": "PAS", "custodian": "C", "feed_status": "any"}
    ),
}


@pytest.mark.parametrize("case", SHAPED)
def test_what_the_curator_sends_on_is_what_the_backend_can_read(case):
    # A body the backend could not read would be a refused call with no step recorded.
    try:
        TypeAdapter(backend_models.Proposed).validate_python(SHAPED[case])
        readable = True
    except ValidationError:
        readable = False

    assert tools.fits("submit_proposal", SHAPED[case]) == readable


# The committed recording


def test_the_scripted_curation_replays_on_the_first_cycles_bundle(make_layer, tmp_path):
    import firm

    layer = make_layer(
        playbooks=firm.copy(tmp_path / "as-committed"),
        model=f"replay:{ROOT}/recordings/scripted-triage.json",
        curation_model=cycle.CURATION_REPLAY,
    )
    for name in cycle.FIRST_CYCLES_MEMOS:
        assert knowledge.intake(layer, knowledge.committed_memo(name)).status_code == 200
    runs = [layer.triage(day) for day in (DAY, CROSS_CHECK_DAY)]
    assert knowledge.review_ended(layer, knowledge.corrected(layer, runs[1])["id"])["gap_report"]

    run = curation.curated(layer)

    assert run["state"] == "completed", run["failure"]
    rested = {each for change in run["proposal"]["changes"] for each in change["rests_on"]}
    (report,) = knowledge.gap_reports(layer)
    assert {curation.MEMO, curation.CORRECTION, report["reference"]} <= rested
    assert report["state"] == "cited"
    assert run["usage"]["models"] == ["replay:scripted-curation (scripted)"]


def test_a_bundle_the_recording_does_not_hold_fails_saying_so(make_layer):
    layer = make_layer(curation_model=cycle.CURATION_REPLAY)

    run = curation.curated(layer)

    assert run["state"] == "failed" and "no longer matches" in run["failure"]
