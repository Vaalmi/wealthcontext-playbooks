"""Every way a choice can go wrong: refused, recorded, and never on a note."""

import itertools
import json
import shutil
from collections.abc import Callable

import firm
import pytest
from langchain_core.messages import AIMessage
from layer import CROSS_CHECK_DAY, CROSS_CHECK_OPTION, DAY, OPTION, ROOT, choices
from stub_model import USAGE, call, calls, says

from cli.main import describe

EVENTS = [(DAY, OPTION), (CROSS_CHECK_DAY, CROSS_CHECK_OPTION)]
WRONG: dict[str, Callable[[], AIMessage]] = {
    "no step, where one applies": lambda: call("no_step_applies"),
    "a step that was not offered": lambda: call("choose_step", option="feed-outage/invented"),
    "words and no call": lambda: says("The step applies."),
    "a tool that does not exist": lambda: call("release_report"),
    "two calls at once": lambda: calls(("no_step_applies", {}), ("no_step_applies", {})),
}


@pytest.fixture
def narrowed(tmp_path):
    """Both playbooks, each narrowed to custodian A, with a custodian B step beside it."""
    folder = tmp_path / "narrowed"
    shutil.copytree(ROOT / "playbooks", folder)
    for file in folder.glob("*/1.yaml"):
        head, step = file.read_text().split("steps:\n")
        steps = [
            step.replace("custodian: any", f"custodian: {custodian}").replace(
                "  - id: ", f"  - id: {custodian.lower()}-", 1
            )
            for custodian in ("A", "B")
        ]
        file.write_text(f"{head}steps:\n{''.join(steps)}")
    return firm.found(folder)


@pytest.mark.parametrize("day, option", EVENTS)
@pytest.mark.parametrize("first, second", list(itertools.product(WRONG, repeat=2)))
def test_a_second_refused_choice_is_unverified_with_no_action(
    make_layer, day, option, first, second
):
    layer = make_layer(
        replies=lambda: [WRONG[first](), WRONG[second](), call("choose_step", option=option)]
    )
    run = layer.triage(day)

    note = run["notes"][0]
    assert run["state"] == "completed"
    assert note["state"] == "unverified"
    assert note["statement"] == "No verified choice; needs a person."
    assert note["step"] is None and note["action"] is None
    # The right answer was there to give, and the model was not asked a third time.
    assert len(layer.stubs[0].prompts) == 2
    assert {step["outcome"] for step in choices(run)} == {"refused"}


@pytest.mark.parametrize("day, option", EVENTS)
def test_a_second_step_that_does_not_fit_is_unverified_with_no_action(
    make_layer, narrowed, day, option
):
    playbook, step = option.split("/")
    misfit = f"{playbook}/a-{step}"
    layer = make_layer(
        playbooks=narrowed,
        replies=lambda: [call("choose_step", option=misfit), call("choose_step", option=misfit)],
    )
    run = layer.triage(day)

    note = run["notes"][0]
    assert note["state"] == "unverified"
    assert note["step"] is None and note["action"] is None
    assert [step["option"] for step in choices(run)] == [misfit, misfit]
    assert all("its custodian is A" in step["line"] for step in choices(run))
    page = layer.request("GET", "/").text
    unverified = page.split('data-state="unverified"')[1].split("</article>")[0]
    assert "No verified choice; needs a person." in unverified
    assert 'class="action"' not in unverified
    assert "tell " not in describe(run)


PLANTED = {
    "a figure": "$1,234,567.89",
    "a figure in words": "ten million dollars",
    "a name and an address": "Jordan Example jordan.example@example.com",
    "an account number": "998877665544",
}


def every_call(planted: str, offered: list[dict], option: str) -> list[tuple[str, AIMessage]]:
    """Each place in each offered tool's call that a model could put a text, and around it."""
    made = []
    for tool in offered:
        name, schema = tool["function"]["name"], tool["function"]["parameters"]
        valid = {field: rules["enum"][0] for field, rules in schema["properties"].items()}
        for field, value in valid.items():
            made += [
                (f"{name}.{field}", call(name, **{field: planted})),
                (f"{name}.{field}, after it", call(name, **{field: f"{value} {planted}"})),
                (f"{name}.{field}, a list", call(name, **{field: [value, planted]})),
                (f"{name}.{field}, nested", call(name, **{field: {"option": planted}})),
            ]
        made += [
            (f"{name}, an extra field", call(name, **valid, note=planted)),
            (f"{name}, a field's name", call(name, **{**valid, planted: "x"})),
        ]
        broken = {"name": name, "args": planted, "id": "broken", "error": planted}
        made.append((f"{name}, not JSON", AIMessage(content="", invalid_tool_calls=[broken])))
    made += [
        ("a tool's name", call(planted)),
        ("another tool's field", call("file_note", text=planted)),
        ("words and no call", says(planted)),
        (
            "a second call",
            calls(("choose_step", {"option": option}), ("file_note", {"text": planted})),
        ),
    ]
    return made


def beside_a_good_call(planted: str, option: str) -> list[tuple[str, AIMessage]]:
    """Calls that hold, with the text everywhere else in the reply."""
    worded = call("choose_step", option=option)
    worded.content = planted
    named = call("choose_step", option=option)
    named.tool_calls[0]["id"] = planted
    described = call("choose_step", option=option)
    described.response_metadata = {"stop_reason": planted}
    described.additional_kwargs = {"reasoning": planted}
    return [("words", worded), ("the call's id", named), ("the reply's metadata", described)]


def without_ids(run: dict) -> list[dict]:
    kept = ("code", "subjects", "state", "statement", "step", "action")
    return [
        {field: note[field] for field in kept}
        | {"feed_run": note["receipt"]["feed_run"], "set": note["receipt"]["playbook_set"]}
        for note in run["notes"]
    ]


@pytest.mark.parametrize("day, option", EVENTS)
@pytest.mark.parametrize("planted", PLANTED.values(), ids=list(PLANTED))
def test_no_note_carries_what_a_model_puts_into_a_tool_call(
    make_layer, caplog, day, option, planted
):
    script: list[AIMessage] = [call("choose_step", option=option)]
    layer = make_layer(replies=lambda: list(script))
    honest = without_ids(layer.triage(day))
    (offered,) = layer.stubs[0].offered
    assert {tool["function"]["name"] for tool in offered} == {"choose_step", "no_step_applies"}
    unverified = [
        honest[0]
        | {
            "state": "unverified",
            "statement": "No verified choice; needs a person.",
            "step": None,
            "action": None,
        },
        *honest[1:],
    ]

    names = [where for where, _ in every_call(planted, offered, option)]
    scripts = []
    for index, where in enumerate(names):
        # A reply is used once, so each is made again.
        first, again, before_a_right_one = (
            every_call(planted, offered, option)[index][1] for _ in range(3)
        )
        # Twice, which ends the note unverified, and once before a right answer.
        scripts.append((where, [first, again], unverified))
        right = call("choose_step", option=option)
        scripts.append((where, [before_a_right_one, right], honest))
    scripts += [(where, [it], honest) for where, it in beside_a_good_call(planted, option)]

    for where, replies, expected in scripts:
        script[:] = replies
        run = layer.triage(day)
        assert run["state"] == "completed", where
        assert without_ids(run) == expected, where
        assert planted not in json.dumps(run), where
        assert planted not in describe(run), where
        assert planted not in layer.request("GET", f"/triage-runs/{run['id']}").text, where
    assert planted.encode() not in layer.settings.database.read_bytes()
    assert planted not in caplog.text


def test_no_note_carries_the_figures_or_names_in_a_warnings_detail(make_layer):
    secrets = {"total": "$1,234,567.89", "title": "Jordan Example Family Trust"}

    def detailed(operation, envelope):
        if operation == "get_data_quality_report":
            for flag in envelope["quality_flags"]:
                flag["detail"] |= secrets
        return envelope

    layer = make_layer(replies=lambda: [call("choose_step", option=CROSS_CHECK_OPTION)])
    layer.wealthcontext.edit = detailed
    run = layer.triage(CROSS_CHECK_DAY)

    everywhere = [
        json.dumps(run),
        describe(run),
        layer.request("GET", "/").text,
        str(layer.stubs[0].prompts),
        layer.settings.database.read_bytes().decode("latin-1"),
    ]
    assert run["notes"][0]["state"] == "chosen"
    assert not [value for value in secrets.values() for text in everywhere if value in text]


@pytest.mark.parametrize(
    "replies, recorded",
    [
        (
            lambda: [call("choose_step", option=OPTION)],
            [(1, "choose_step", OPTION, "accepted")],
        ),
        (
            lambda: [call("no_step_applies"), call("choose_step", option=OPTION)],
            [(1, "no_step_applies", None, "refused"), (2, "choose_step", OPTION, "accepted")],
        ),
        (
            lambda: [call("choose_step", option="feed-outage/invented"), call("release_report")],
            [(1, "choose_step", None, "refused"), (2, "other", None, "refused")],
        ),
        (
            lambda: [
                calls(("no_step_applies", {}), ("release_report", {}), ("choose_step", {})),
                call("no_step_applies", why="because"),
            ],
            [
                (1, "no_step_applies", None, "refused"),
                (1, "other", None, "refused"),
                (1, "choose_step", None, "refused"),
                (2, "no_step_applies", None, "refused"),
            ],
        ),
        (
            lambda: [calls(*[("no_step_applies", {})] * 300), call("choose_step", option=OPTION)],
            [(1, "no_step_applies", None, "refused")] * 300
            + [(2, "choose_step", OPTION, "accepted")],
        ),
    ],
)
def test_each_tool_call_is_a_step_refused_ones_too(make_layer, replies, recorded):
    run = make_layer(replies=replies).triage()

    assert [(s["turn"], s["tool"], s["option"], s["outcome"]) for s in choices(run)] == recorded
    for step in choices(run):
        assert step["note_id"] == run["notes"][0]["id"]
        assert step["attempt"] == 1
        assert bool(step["line"]) is (step["outcome"] == "refused")
    # A reply's tokens are counted once, however many calls it held.
    turns = len({step["turn"] for step in choices(run)})
    assert run["usage"]["input_tokens"] == turns * USAGE["input_tokens"]


def test_a_misfit_is_a_step_with_the_option_it_named(make_layer, narrowed):
    misfit = "feed-outage/a-portfolio-feed-failed"
    layer = make_layer(
        playbooks=narrowed,
        replies=lambda: [
            call("choose_step", option=misfit),
            call("choose_step", option="feed-outage/b-portfolio-feed-failed"),
        ],
    )

    refused, accepted = choices(layer.triage())

    assert (refused["option"], refused["outcome"]) == (misfit, "refused")
    assert accepted["outcome"] == "accepted"
