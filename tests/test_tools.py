"""The tools' schemas: a closed list, no free text and no id."""

from typing import Any, cast

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

from triage_agent import api, tools


def note(*steps: str) -> api.PendingNote:
    anything = api.When(source_family="any", custodian="any", feed_status="any")
    return api.PendingNote(
        id="not-for-the-model",
        code="SOURCE_UNAVAILABLE",
        feed=None,
        warning_count=1,
        options=[
            api.Offered(playbook="feed-outage", step=step, when=anything, line="Tell the desk.")
            for step in steps
        ],
        refusals=[],
    )


def schemas(*steps: str) -> dict[str, dict]:
    context = tools.NoteContext(cast(Any, None), "run", 1, note(*steps))
    made = [convert_to_openai_tool(tool) for tool in tools.build(context)]
    return {tool["function"]["name"]: tool["function"]["parameters"] for tool in made}


@pytest.mark.parametrize("steps", [("general",), ("custodian-a", "custodian-b")])
def test_choose_step_takes_one_option_from_the_list(steps):
    schema = schemas(*steps)["choose_step"]

    assert list(schema["properties"]) == ["option"]
    assert schema["required"] == ["option"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["option"]["enum"] == [f"feed-outage/{step}" for step in steps]


def test_no_step_applies_takes_nothing():
    schema = schemas("general")["no_step_applies"]

    assert schema["properties"] == {}
    assert schema["additionalProperties"] is False


def gap_schemas() -> list[dict[str, dict]]:
    uncovered = api.UncoveredNote(
        id="not-for-the-model", code="PRICE_STALE", feed=None, warning_count=1, refusals=[]
    )
    contexts = (
        tools.GapContext(cast(Any, None), "run", 1, uncovered),
        tools.ReviewContext(cast(Any, None), "correction", 1),
    )
    found = []
    for context in contexts:
        made = [convert_to_openai_tool(tool) for tool in tools.build_gap(context)]
        found.append({t["function"]["name"]: t["function"]["parameters"] for t in made})
    return found


@pytest.mark.parametrize("made", gap_schemas(), ids=["a run's", "a review's"])
def test_a_gap_reports_one_argument_is_its_summary_and_no_id(made):
    assert set(made) == {"file_gap_report", "no_gap_report"}
    assert made["file_gap_report"]["properties"] == {"summary": {"type": "string"}}
    assert made["file_gap_report"]["required"] == ["summary"]
    assert made["no_gap_report"]["properties"] == {}
    for schema in made.values():
        assert schema["additionalProperties"] is False


def test_no_choice_tool_takes_an_id_or_free_text():
    made = schemas("general", "other")
    assert set(made) == {"choose_step", "no_step_applies"}
    for name, schema in made.items():
        for field, rules in schema["properties"].items():
            assert field != "id" and not field.endswith("_id"), name
            assert rules.get("enum"), f"{name}.{field} takes free text"
