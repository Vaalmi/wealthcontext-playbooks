"""The guardrails: no step tells anyone to do what WealthContext's guarantees forbid.

A step's fixed fields can say only what their closed forms allow, so the four forbidden
instructions can hide only in words: the step's line and its names. Those are matched
against a closed list of each, and the line holds no figure. No text of the proposal holds
words addressed to an agent. That the steps in use do not overlap is the overlap
check's.
"""

import unicodedata

from pydantic import TypeAdapter, ValidationError

from backend import models, personal_data, text_rules
from backend.gate.effect import Given, shown, where
from backend.playbook_format import LINE_LENGTH, By
from backend.proposals import names

_BY = TypeAdapter(By)


def _out_of_form(change: models.AddStep | models.ChangeStep, given: Given) -> str | None:
    if change.tell not in given.desks:
        return "its tell is not on the list of desks"
    try:
        _BY.validate_python(change.by)
    except ValidationError:
        return "its by is none of the three forms of a deadline"
    line = change.line
    if len(line) > LINE_LENGTH or not line.strip() or any(map(_control, line)):
        return "its line is not one visible line of at most 160 characters"
    return None


def _control(character: str) -> bool:
    return unicodedata.category(character).startswith("C")


def _in_words(change: models.StepChange) -> list[tuple[str, str]]:
    """The rules a change's words break, each with why."""
    found = []
    words = names(change)
    if not isinstance(change, models.RemoveStep):
        words.append(change.line)
        if text_rules.figure_in(change.line):
            found.append(("financial_figure", "its line carries a number, currency or percentage"))
    for text in words:
        if instruction := text_rules.forbidden_instruction(personal_data.plain_words(text)):
            found.append(("forbidden_instruction", f"its words tell someone to {instruction}"))
            break
    sent = [change.model_dump(mode="json"), names(change)]
    if any(personal_data.addressed_to_an_agent(text) for text in _texts(sent)):
        found.append(("addressed_to_an_agent", "it carries words addressed to an agent"))
    return found


def _texts(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, inner in value.items() for text in [key, *_texts(inner)]]
    if isinstance(value, list):
        return [text for inner in value for text in _texts(inner)]
    return []


def check(given: Given) -> list[models.Failure]:
    failures = []
    for number, change in enumerate(given.proposal.changes, start=1):
        found = _in_words(change)
        if not isinstance(change, models.RemoveStep) and (wrong := _out_of_form(change, given)):
            found.insert(0, ("field_out_of_form", wrong))
        failures += [
            models.Failure(
                change=number,
                step=shown(change),
                rule=rule,
                line=f"{where(number, change)}: {why}.",
            )
            for rule, why in found
        ]
    for number, told in enumerate(given.proposal.disagreements, start=1):
        if any(map(personal_data.addressed_to_an_agent, _texts(told.model_dump(mode="json")))):
            failures.append(
                models.Failure(
                    change=None,
                    step=None,
                    rule="addressed_to_an_agent",
                    line=f"Disagreement {number}: it carries words addressed to an agent.",
                )
            )
    return failures
