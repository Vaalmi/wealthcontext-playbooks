"""Models are a setting: built, priced and replayed, with no model called."""

import json
import os
from decimal import Decimal
from typing import Any, cast

import pytest
from conftest import NEVER_SET
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from layer import ROOT

from backend.models import Usage
from modelkit import models
from modelkit.prices import PRICES
from modelkit.replay import Recording, RecordingMismatch, ReplayModel, request_hash
from triage_agent import api, tools


def test_the_checks_run_with_no_provider_key_set():
    assert not [name for name in NEVER_SET if os.environ.get(name)]


def test_a_live_model_needs_its_key_from_the_setting():
    with pytest.raises(models.Unavailable) as refused:
        models.configured("anthropic:claude-opus-5-5", {})
    assert refused.value.reason == "no_model"


def test_a_model_with_no_price_cannot_hold_a_cap():
    with pytest.raises(models.Unavailable) as refused:
        models.configured("anthropic:unpriced", {"ANTHROPIC_API_KEY": "not-a-key"})
    assert refused.value.reason == "no_price"


@pytest.mark.parametrize("setting", ["", "anthropic", "unknown:model", "replay:missing.json"])
def test_a_setting_that_names_no_model_is_refused(setting):
    with pytest.raises(models.Unavailable):
        models.configured(setting, {"ANTHROPIC_API_KEY": "not-a-key"})


def offered() -> list:
    anything = api.When(source_family="any", custodian="any", feed_status="any")
    note = api.PendingNote(
        id="x",
        code="PRICE_STALE",
        feed=None,
        warning_count=1,
        options=[api.Offered(playbook="prices", step="stale", when=anything, line="Tell pricing.")],
        refusals=[],
    )
    return tools.build(tools.NoteContext(cast(Any, None), "run", 1, note))


def test_the_providers_request_is_one_strict_call_with_no_forced_choice():
    built = models.configured("anthropic:claude-opus-5-5", {"ANTHROPIC_API_KEY": "not-a-key"})
    bound = built.offering(offered())

    payload = cast(Any, built.model)._get_request_payload(
        [SystemMessage("Choose."), HumanMessage("Now.")], **bound.kwargs
    )

    assert payload["model"] == "claude-opus-5-5"
    assert payload["max_tokens"] == 4096
    assert payload["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    for sampling in ("temperature", "top_p", "top_k"):
        assert payload.get(sampling) is None
    for tool in payload["tools"]:
        assert tool["strict"] is True
        assert tool["input_schema"]["additionalProperties"] is False


def test_the_providers_request_for_a_gap_report_is_one_strict_call_too():
    built = models.configured("anthropic:claude-opus-5-5", {"ANTHROPIC_API_KEY": "not-a-key"})
    context = tools.ReviewContext(cast(Any, None), "correction", 1)
    bound = built.offering(tools.build_gap(context))

    payload = cast(Any, built.model)._get_request_payload([HumanMessage("Now.")], **bound.kwargs)

    filing, none = payload["tools"]
    assert filing["strict"] is none["strict"] is True
    assert filing["input_schema"]["properties"] == {"summary": {"type": "string"}}
    assert filing["input_schema"]["additionalProperties"] is False


def test_a_refusal_goes_back_as_an_error_result_with_the_reply_whole():
    built = models.configured("anthropic:claude-opus-5-5", {"ANTHROPIC_API_KEY": "not-a-key"})
    made = {"id": "toolu_1", "name": "no_step_applies", "input": {}}
    thought = {"type": "thinking", "thinking": "", "signature": "signed"}
    reply = AIMessage(
        content=[thought, {"type": "tool_use", **made}],
        tool_calls=[{"id": "toolu_1", "name": "no_step_applies", "args": {}}],
    )
    refused = ToolMessage("A step applies.", tool_call_id="toolu_1", status="error", artifact=True)

    payload = cast(Any, built.model)._get_request_payload(
        [SystemMessage("Choose."), HumanMessage("Now."), reply, refused]
    )

    assert payload["messages"][1]["content"] == [thought, {"type": "tool_use", **made}]
    (result,) = payload["messages"][2]["content"]
    assert result["is_error"] is True and result["tool_use_id"] == "toolu_1"


def test_every_model_the_agent_can_run_has_a_name_the_backend_takes():
    names = list(PRICES)
    names += [f"{model}:{effort}" for model, price in PRICES.items() for effort in price.efforts]
    for file in (ROOT / "recordings").glob("*.json"):
        names.append(models.configured(f"replay:{file}", {}).name)

    for name in names:
        Usage(model=name, input_tokens=None, output_tokens=None, cost_usd=None)


def test_tokens_become_dollars_at_the_checked_price():
    price = models.configured("anthropic:claude-opus-5-5", {"ANTHROPIC_API_KEY": "not-a-key"}).price

    assert price is not None
    assert price.dollars(1_000_000, 0) == Decimal(4)
    assert price.dollars(0, 1_000_000) == Decimal(20)


def test_the_most_a_call_could_cost_counts_every_retry():
    built = models.configured("anthropic:claude-opus-5-5", {"ANTHROPIC_API_KEY": "not-a-key"})

    most = built.most_it_could_cost([HumanMessage("Now.")], offered())

    # Three tries of at least 4096 tokens out, at twenty dollars a million.
    assert most > Decimal("0.24576")
    assert most < Decimal("0.5")


def test_tokens_are_read_from_the_reply():
    used = {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}

    assert models.tokens(AIMessage("", usage_metadata=cast(Any, used))) == (7, 3)
    assert models.tokens(AIMessage("")) == (None, None)


def test_a_replay_gives_its_replies_in_order_then_refuses(tmp_path):
    asked = [HumanMessage("Choose.")]
    file = tmp_path / "two.json"
    request = request_hash(asked, [])
    replies = [{"request": request, "text": text, "tool_calls": []} for text in ("a", "b")]
    file.write_text(json.dumps({"origin": "scripted", "about": "test", "replies": replies}))
    model = ReplayModel(recording=Recording.read(file))

    assert [model.invoke(asked).text, model.invoke(asked).text] == ["a", "b"]
    with pytest.raises(RecordingMismatch):
        model.invoke(asked)
    with pytest.raises(RecordingMismatch):
        model.invoke([HumanMessage("Something else.")])


def _scripted(*replies: AIMessage):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    class Scripted(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    return Scripted(messages=iter(replies))


def test_a_recording_replays_each_reply_a_model_gave_in_order(tmp_path):
    from modelkit.recorder import Keeper, RecordingModel

    file = tmp_path / "recorded.json"
    keeper = Keeper(file, "recorded", "test")
    given = [
        AIMessage("", tool_calls=[{"name": "choose_step", "args": {"option": "a/b"}, "id": "1"}]),
        AIMessage(
            "not JSON",
            invalid_tool_calls=[
                {
                    "type": "invalid_tool_call",
                    "name": "choose_step",
                    "args": "{",
                    "id": "2",
                    "error": None,
                }
            ],
        ),
    ]
    recording = RecordingModel(inner=_scripted(*given), keeper=keeper)
    offered = tools.build(tools.NoteContext(cast(Any, None), "r", 1, _note()))
    asked = [SystemMessage("Choose."), HumanMessage("One exception.")]
    live = [recording.bind_tools(offered).invoke(asked) for _ in given]

    written = Recording.read(file)
    assert written.origin == "recorded"
    assert [r.request for r in written.replies] == [
        request_hash(asked, [_tool(t) for t in offered])
    ] * 2
    replay = models.Configured("replay", ReplayModel(recording=written), None)
    again = [replay.offering(offered).invoke(asked) for _ in given]
    assert [m.tool_calls[0]["args"] for m in (live[0], again[0])] == [{"option": "a/b"}] * 2
    assert again[1].text == "not JSON" and again[1].invalid_tool_calls[0]["args"] == "{"
    with pytest.raises(RecordingMismatch):
        replay.offering(offered).invoke(asked)


def test_a_live_model_set_to_record_writes_its_replies_and_lasts(tmp_path):
    file = tmp_path / "live.json"
    environment = {"ANTHROPIC_API_KEY": "not-a-key", models.RECORD_TO: str(file)}

    built = models.configured("anthropic:claude-opus-5-5", environment)

    assert built.lasting and built.name == "anthropic:claude-opus-5-5"
    assert built.price is PRICES["anthropic:claude-opus-5-5"]
    assert not file.exists()  # nothing is written until a reply is given
    assert not models.configured("anthropic:claude-opus-5-5", {"ANTHROPIC_API_KEY": "k"}).lasting


@pytest.mark.parametrize("app", ["triage_agent", "curator"])
def test_an_agent_keeps_one_replay_for_all_its_asks(app):
    """Three trials of a case send one request: each is given the next recorded reply."""
    from fastapi.testclient import TestClient

    from curator import entrypoint as curator_app
    from triage_agent import entrypoint as triage_app

    built = []

    def configure(setting, environment):
        built.append(models.Configured(setting, _scripted(), None, lasting=setting == "lasting"))
        return built[-1]

    for setting, times in (("lasting", 1), ("not lasting", 2)):
        built.clear()
        if app == "triage_agent":
            given = triage_app.Settings("http://127.0.0.1:9", setting, Decimal(1), "k", {})
            made = triage_app.create_app(given, configure)
        else:
            settled = curator_app.Settings("http://127.0.0.1:9", setting, Decimal(1), "k", {})
            made = curator_app.create_app(settled, configure)
        agent = TestClient(made)
        for number in range(2):
            asked: dict[str, Any] = {"kind": "trial", "run_id": f"{number:032x}", "attempt": 1}
            asked |= {"model": setting, "cap_usd": "1.000000"}
            if app == "curator":
                asked = {"kind": "curation", "run_id": f"{number:032x}", "attempt": 1}
                asked["model"] = setting
            answered = agent.post("/invocations", json=asked, headers=_key(app))
            assert answered.json() == {"accepted": True}
        assert len(built) == times


def _key(app: str) -> dict[str, str]:
    return {"X-Triage-Agent-Key" if app == "triage_agent" else "X-Curator-Key": "k"}


def _tool(tool):
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return convert_to_openai_tool(tool)


def _note() -> api.PendingNote:
    offered = api.Offered(
        playbook="a",
        step="b",
        when=api.When(source_family="any", custodian="any", feed_status="any"),
        line="A line.",
    )
    return api.PendingNote(
        id="x", code="PRICE_STALE", feed=None, warning_count=1, options=[offered], refusals=[]
    )


def test_a_request_hash_tells_apart_replies_whose_malformed_arguments_differ():
    def asked(arguments: str) -> list:
        invalid = {"type": "invalid_tool_call", "name": "choose_step", "args": arguments}
        reply = AIMessage("", invalid_tool_calls=[{**invalid, "id": "1", "error": None}])
        return [HumanMessage("Choose."), reply, ToolMessage("Refused.", tool_call_id="1")]

    assert request_hash(asked("{"), []) != request_hash(asked("{different arguments"), [])
    assert request_hash([HumanMessage("Choose.")], []) == request_hash(
        [HumanMessage("Choose.")], []
    )


def test_a_reply_with_no_text_and_no_call_replays_to_its_retry(tmp_path):
    """A provider's reply can be a list holding an empty text block. Its retry replays."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from modelkit import spending
    from modelkit.recorder import Keeper, RecordingModel
    from triage_agent import harness

    file = tmp_path / "recorded.json"
    keeper = Keeper(file, "recorded", "test")
    empty = AIMessage(content=[{"type": "text", "text": ""}])
    chosen = AIMessage(
        "", tool_calls=[{"name": "choose_step", "args": {"option": "a/b"}, "id": "c"}]
    )

    class Live(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    def asked(model) -> list[str]:
        sent: list[str] = []
        note = _note()
        context = tools.NoteContext(cast(Any, _Backend(sent)), "r", 1, note)
        configured = models.Configured("m", model, None)
        question = harness.Question(
            configured, spending.Budget(Decimal(1)), context, tools.build(context)
        )
        harness._ask(question, harness.INSTRUCTIONS, harness.request_for(note), [])
        return sent

    assert asked(RecordingModel(inner=Live(messages=iter([empty, chosen])), keeper=keeper)) == [
        "unusable-reply",
        "choice",
    ]
    assert asked(ReplayModel(recording=Recording.read(file))) == ["unusable-reply", "choice"]


class _Backend:
    """A backend that takes every move: the unusable reply refused, the choice accepted."""

    def __init__(self, sent: list[str]):
        self.sent = sent

    def send(self, method: str, path: str, body: dict | None = None):
        import httpx2

        self.sent.append(path.rsplit("/", 1)[-1])
        answer = {"refusal": "Call one tool."} if path.endswith("unusable-reply") else {}
        return httpx2.Response(200, json=answer)


# OpenAI


def _openai(setting: str = "openai:gpt-6.1-terra:high", **options: Any) -> models.Configured:
    return models.configured(setting, {"OPENAI_API_KEY": "not-a-key"}, **options)


def _payload(built: models.Configured, *said: Any) -> dict:
    bound = built.offering(offered())
    messages = list(said) or [SystemMessage("Choose."), HumanMessage("Now.")]
    return cast(Any, built.model)._get_request_payload(messages, **bound.kwargs)


def test_an_openai_model_is_asked_on_the_responses_api_for_one_strict_required_call():
    payload = _payload(_openai())

    assert payload["model"] == "gpt-6.1-terra"
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["store"] is False
    assert payload["tool_choice"] == "required" and payload["parallel_tool_calls"] is False
    assert "input" in payload and "messages" not in payload
    for tool in payload["tools"]:
        assert tool["type"] == "function" and tool["strict"] is True
        assert tool["parameters"]["additionalProperties"] is False
    for sampling in ("temperature", "top_p"):
        assert payload.get(sampling) is None


def test_with_no_effort_named_none_is_sent_and_the_name_says_none():
    built = _openai("openai:gpt-6-luna")

    assert "reasoning" not in _payload(built)
    assert built.name == "openai:gpt-6-luna"
    assert _openai().name == "openai:gpt-6.1-terra:high"


@pytest.mark.parametrize(
    ("setting", "reason"),
    [
        ("openai:gpt-6.2-sol:none", "no_effort"),
        ("openai:gpt-6-luna:turbo", "no_effort"),
        ("openai:gpt-6-luna:", "no_effort"),
        ("openai:gpt-6-unpriced", "no_price"),
    ],
)
def test_an_openai_setting_with_no_price_or_an_effort_its_model_does_not_take_is_refused(
    setting, reason
):
    with pytest.raises(models.Unavailable) as refused:
        _openai(setting)
    assert refused.value.reason == reason
    with pytest.raises(models.Unavailable) as checked:
        models.check(setting)
    assert checked.value.reason == reason


def test_an_effort_on_a_model_that_takes_none_is_refused_not_mapped():
    with pytest.raises(models.Unavailable) as refused:
        models.configured("anthropic:claude-opus-5-5:high", {"ANTHROPIC_API_KEY": "not-a-key"})
    assert refused.value.reason == "no_effort"


def test_the_openai_key_and_address_are_the_settings_alone(monkeypatch):
    # In an agent's container its key is in its environment, as its own setting.
    monkeypatch.setenv("OPENAI_API_KEY", "a-key-in-the-environment")

    with pytest.raises(models.Unavailable) as refused:
        models.configured("openai:gpt-6-luna", {})
    assert refused.value.reason == "no_model"
    built = cast(Any, _openai("openai:gpt-6-luna").model)
    assert built.openai_api_base == models.OPENAI_URL
    assert str(built.root_client.base_url).rstrip("/") == models.OPENAI_URL
    assert built.openai_api_key.get_secret_value() == "not-a-key"
    # No proxy, certificate or netrc of the machine's is read.
    assert built.http_client.trust_env is False


@pytest.mark.parametrize(
    "stray",
    [
        "OPENAI_CUSTOM_HEADERS",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "OPENAI_PROXY",
        "OPENAI_LOG",
        "LANGCHAIN_OPENAI_TCP_KEEPALIVE",
    ],
)
def test_no_openai_model_is_built_beside_a_setting_its_client_would_read_in_place_of_ours(
    monkeypatch, stray
):
    monkeypatch.setenv(stray, "Authorization: Bearer ambient-key")

    with pytest.raises(models.Unavailable) as refused:
        _openai("openai:gpt-6-luna")
    assert refused.value.reason == "stray_setting"


def test_an_openai_model_is_asked_for_standard_processing_which_it_is_priced_at():
    assert _payload(_openai())["service_tier"] == "default"


def test_a_reasoning_models_limit_is_the_agents_and_the_reserve_and_bounds_each_call():
    triage, curator = _openai("openai:gpt-6-luna"), _openai(max_output_tokens=8192)

    assert triage.max_output_tokens == 4096 + models.REASONING_TOKENS
    assert curator.max_output_tokens == 8192 + models.REASONING_TOKENS
    assert _payload(curator)["max_output_tokens"] == 33_192
    most = curator.most_it_could_cost([HumanMessage("Now.")], offered())
    # Two tries of 33,192 tokens out at five dollars a million, and the input.
    assert curator.attempts == 2
    assert Decimal("0.33192") < most < Decimal("0.35")
    # An Anthropic model's limit is its agent's.
    claude = models.configured("anthropic:claude-opus-5-5", {"ANTHROPIC_API_KEY": "not-a-key"})
    assert claude.max_output_tokens == 4096


def test_a_refusal_goes_back_to_an_openai_model_as_its_calls_output():
    reply = AIMessage(
        content="", tool_calls=[{"id": "call_1", "name": "no_step_applies", "args": {}}]
    )
    refused = ToolMessage("A step applies.", tool_call_id="call_1", status="error", artifact=True)

    payload = _payload(_openai(), SystemMessage("Choose."), HumanMessage("Now."), reply, refused)

    call, result = payload["input"][2:]
    assert (call["type"], call["call_id"], call["name"]) == (
        "function_call",
        "call_1",
        "no_step_applies",
    )
    assert (result["type"], result["call_id"]) == ("function_call_output", "call_1")
    assert result["output"] == "A step applies."


def _spent_on(reply: AIMessage):
    from stub_model import stub

    from modelkit import spending

    luna = PRICES["openai:gpt-6-luna"]
    built = models.Configured(name="openai:gpt-6-luna", model=stub(reply), price=luna, forced=True)
    budget = spending.Budget(cap=Decimal(2))
    return spending.ask(built, budget, [HumanMessage("Now.")], offered()), budget, luna


def test_a_replys_reported_reasoning_tokens_are_charged_as_output():
    # As OpenAI reports them: output tokens with the reasoning among them.
    usage = {
        "input_tokens": 75,
        "output_tokens": 1186,
        "total_tokens": 1261,
        "output_token_details": {"reasoning": 1024},
    }
    reply = AIMessage(
        content="",
        tool_calls=[{"id": "call_1", "name": "no_step_applies", "args": {}}],
        usage_metadata=cast(Any, usage),
    )

    spent, budget, _ = _spent_on(reply)

    assert (spent.input_tokens, spent.output_tokens) == (75, 1186)
    assert spent.cost_usd == "0.000601"
    assert budget.spent == Decimal("0.0006005")


def test_a_reply_cut_off_at_its_limit_has_no_usable_call_and_is_charged_what_it_used():
    from curator import harness as curating
    from triage_agent import harness as triaging

    usage = {"input_tokens": 75, "output_tokens": 29_096, "total_tokens": 29_171}
    # Its call's arguments happen to parse: still it was being written when the limit came.
    reply = AIMessage(
        content="",
        tool_calls=[{"id": "call_1", "name": "no_step_applies", "args": {}}],
        response_metadata={
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
        },
        usage_metadata=cast(Any, usage),
    )

    spent, budget, _ = _spent_on(reply)

    # 75 in at $0.10 a million and 29,096 out at $0.50.
    assert budget.spent == Decimal("0.0145555")
    offered_now = {tool.name: tool for tool in offered()}
    assert triaging._unusable(spent.reply, offered_now) == "no_tool_call"
    assert curating._unusable(spent.reply) == "no_tool_call"


class _Lost(Exception):
    """A provider's error, carrying the key it was sent, as some do."""


def test_a_call_with_no_answer_is_charged_its_bound_and_carries_none_of_the_providers_words():
    from stub_model import stub

    from modelkit import spending

    luna = PRICES["openai:gpt-6-luna"]
    built = models.Configured(
        name="openai:gpt-6-luna",
        model=stub(_Lost("401: Incorrect API key provided: sk-not-a-real-key")),
        price=luna,
        forced=True,
    )
    budget = spending.Budget(cap=Decimal(2))
    most = built.most_it_could_cost([HumanMessage("Now.")], offered())

    with pytest.raises(spending.ModelFailed) as failed:
        spending.ask(built, budget, [HumanMessage("Now.")], offered())

    # One try, not tried again: a lost answer may have been billed, so its bound is charged.
    assert failed.value.charged == most / built.attempts == budget.spent
    assert str(failed.value) == "_Lost"
    assert failed.value.__cause__ is None and failed.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "said",
    [
        {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
        {"finish_reason": "length"},
        {"stop_reason": "max_tokens"},
    ],
)
def test_a_cut_off_reply_is_recorded_as_the_agent_is_given_it_with_no_call(tmp_path, said):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from modelkit.recorder import Keeper, RecordingModel

    cut = AIMessage(
        content="",
        tool_calls=[{"id": "call_1", "name": "choose_step", "args": {"option": "prices/stale"}}],
        response_metadata=said,
    )
    keeper = Keeper(tmp_path / "recording.json", "recorded", "a live cycle")
    model = RecordingModel(inner=GenericFakeChatModel(messages=iter([cut])), keeper=keeper)

    given = model.invoke([HumanMessage("Now.")])

    assert given.tool_calls == [] and given.invalid_tool_calls == []
    (kept,) = Recording.read(tmp_path / "recording.json").replies
    assert kept.tool_calls == [] and kept.invalid_tool_calls == []


def test_a_reasoning_models_call_ends_before_its_work_reads_stalled_or_its_command_stops_waiting():
    from backend import triage
    from backend.gate import held_out
    from cli import main as cli

    built = _openai("openai:gpt-6-luna")
    worst = built.attempts * cast(Any, built.model).request_timeout
    # Each try may be given as long as this, and the provider's client waits a little between.
    assert worst + 30 <= triage.STALLED_AFTER.total_seconds()
    assert worst + 30 <= held_out.STALLED_SECONDS
    for command in (
        [
            "correct",
            "--run",
            "r",
            "--note",
            "n",
            "--field",
            "by",
            "--should-be",
            "x",
            "--reason",
            "y",
        ],
        ["request-review", "--correction", "c"],
        ["triage", "--date", "2026-10-15", "--cutoff", "2026-10-31T16:00:00Z"],
    ):
        assert cli._commands().parse_args(command).wait >= worst


def test_every_openai_price_is_the_one_on_openais_page():
    # From https://developers.openai.com/api/docs/pricing, Standard, short context, 2026-11-23.
    expected = {
        "openai:gpt-6.2-sol": (Decimal(2), Decimal(10)),
        "openai:gpt-6.1-terra": (Decimal(1), Decimal(5)),
        "openai:gpt-6-luna": (Decimal("0.10"), Decimal("0.50")),
    }
    for model, (given, taken) in expected.items():
        assert (PRICES[model].input, PRICES[model].output) == (given, taken)
    # A reply of 75 tokens in and 1,186 out on GPT-6 Luna.
    assert PRICES["openai:gpt-6-luna"].dollars(75, 1186) == Decimal("0.0006005")
    efforts = {"low", "medium", "high", "xhigh", "max"}
    assert PRICES["openai:gpt-6.2-sol"].efforts == efforts
    assert (
        PRICES["openai:gpt-6.1-terra"].efforts
        == PRICES["openai:gpt-6-luna"].efforts
        == {
            "none",
            *efforts,
        }
    )


class _Busy(Exception):
    """A provider's answer that it was busy: it bills nothing for it."""

    status_code = 429


class _InTurn:
    """Replies in turn, an error among them raised, and the next still given after it."""

    def __init__(self, *replies: Any):
        self.replies = list(replies)

    def __iter__(self):
        return self

    def __next__(self):
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _luna(*replies: Any) -> models.Configured:
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    class Fake(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    return models.Configured(
        name="openai:gpt-6-luna",
        model=Fake(messages=_InTurn(*replies)),
        price=PRICES["openai:gpt-6-luna"],
        attempts=2,
        forced=True,
    )


def test_an_error_the_provider_answered_with_is_tried_again_and_charged_nothing(monkeypatch):
    from modelkit import spending

    monkeypatch.setattr(spending.time, "sleep", lambda _: None)
    usage = {"input_tokens": 100, "output_tokens": 29_096, "total_tokens": 29_196}
    chose = AIMessage(
        content="",
        tool_calls=[{"id": "call_1", "name": "no_step_applies", "args": {}}],
        usage_metadata=cast(Any, usage),
    )
    budget = spending.Budget(cap=Decimal(2))

    spent = spending.ask(_luna(_Busy("busy"), chose), budget, [HumanMessage("Now.")], offered())

    # Only the reply that came back is charged: 100 in and 29,096 out on GPT-6 Luna.
    assert spent.cost_usd == "0.014558" and budget.spent == Decimal("0.014558")


def test_a_try_with_no_answer_is_not_tried_again_however_many_tries_are_left(monkeypatch):
    from modelkit import spending

    monkeypatch.setattr(spending.time, "sleep", lambda _: None)
    chose = AIMessage(content="", tool_calls=[{"id": "c", "name": "no_step_applies", "args": {}}])
    built = _luna(TimeoutError("lost"), chose)
    budget = spending.Budget(cap=Decimal(2))
    most = built.most_it_could_cost([HumanMessage("Now.")], offered())

    with pytest.raises(spending.ModelFailed) as failed:
        spending.ask(built, budget, [HumanMessage("Now.")], offered())

    # Charged its one try's bound; the reply waiting behind it was never asked for.
    assert failed.value.charged == most / 2 == budget.spent
    assert cast(Any, built.model).messages.replies == [chose]


def test_the_providers_client_makes_no_tries_of_its_own():
    assert cast(Any, _openai().model).max_retries == 0
    claude = models.configured("anthropic:claude-opus-5-5", {"ANTHROPIC_API_KEY": "not-a-key"})
    assert cast(Any, claude.model).max_retries == 0


class _Unread(Exception):
    """A reply that came back, and could not be read: it may have been billed."""

    status_code = 200


def test_a_reply_that_came_back_and_could_not_be_read_is_charged_its_bound():
    from modelkit import spending

    built = _luna(_Unread("unread"))
    budget = spending.Budget(cap=Decimal(2))
    most = built.most_it_could_cost([HumanMessage("Now.")], offered())

    with pytest.raises(spending.ModelFailed) as failed:
        spending.ask(built, budget, [HumanMessage("Now.")], offered())

    assert failed.value.charged == most / 2 == budget.spent
