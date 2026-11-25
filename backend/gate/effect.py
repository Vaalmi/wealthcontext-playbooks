"""What a proposal would do: the steps in use after it, and which step applies at each condition.

The proposal is read as it was sent. Nothing here refuses it: each check says what it finds.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import product
from typing import Any, get_args

from pydantic import TypeAdapter, ValidationError

from backend import matching, models, personal_data, text_rules
from backend.exceptions import PRODUCED
from backend.playbook_format import Slug, Step, When
from backend.proposals import PROPOSAL_TEXT_LENGTH, names
from backend.store import PlaybookSet
from backend.vocabulary import Custodian, FeedFacts, FeedStatus, SourceFamily, WarningCode

# The bound on each text of a proposal, as when a curator's is recorded.
TEXT_LENGTH = PROPOSAL_TEXT_LENGTH
CODES: tuple[str, ...] = get_args(WarningCode)
_SLUG = TypeAdapter(Slug)


@dataclass(frozen=True)
class Given:
    """What a check reads: the proposal, its bundle's items and the playbooks in use."""

    proposal: models.Proposed
    items: Sequence[Mapping[str, Any]]
    playbooks: PlaybookSet
    desks: Mapping[str, str]


@dataclass(frozen=True)
class Placed:
    """A step as it would stand in use, and what it tells a person to do."""

    playbook: str
    step: str
    code: str
    when: When
    action: tuple
    # The proposal's change that put it there, by its number. None: in use, and untouched.
    change: int | None

    def outcome(self) -> tuple:
        return (self.playbook, self.step, self.action)

    def as_step(self) -> Step:
        """As a version's file holds it, once the proposal that placed it is approved."""
        tell, by, hold_report, line = self.action
        fields = {"id": self.step, "code": self.code, "when": self.when, "tell": tell, "by": by}
        return Step.model_validate(fields | {"hold_report": hold_report, "line": line})


def is_name(text: str) -> bool:
    try:
        _SLUG.validate_python(text)
    except ValidationError:
        return False
    return True


def shown(change: models.StepChange) -> str | None:
    """A change's step as `playbook/step`, when its names can be shown, or nothing."""
    playbook, step, whole = names(change)
    if not (is_name(playbook) and is_name(step)) or personal_data.broken_rule(whole, TEXT_LENGTH):
        return None
    if text_rules.forbidden_instruction(personal_data.plain_words(whole)):
        return None
    return whole


def where(number: int, change: models.StepChange) -> str:
    named = shown(change)
    return f"Change {number}" + (f" ({named})" if named else "")


def in_use(playbooks: PlaybookSet) -> list[Placed]:
    return [
        Placed(
            playbook=each.playbook,
            step=each.step.id,
            code=each.step.code,
            when=each.step.when,
            action=(each.step.tell, each.step.by, each.step.hold_report, each.step.line),
            change=None,
        )
        for each in playbooks.steps
    ]


def after(before: Iterable[Placed], proposal: models.Proposed) -> list[Placed]:
    """The steps in use once the proposal's changes are made, each in the place it names.

    In the order the store gives steps in use: by playbook, and within one as its file will
    hold them. So the trials ask the model what triage will ask it once this is approved.
    """
    touched = {(change.playbook, change.step) for change in proposal.changes}
    steps = [each for each in before if (each.playbook, each.step) not in touched]
    for number, change in enumerate(proposal.changes, start=1):
        if isinstance(change, models.RemoveStep):
            continue
        steps.append(
            Placed(
                playbook=change.playbook,
                step=change.step,
                code=change.code,
                when=change.when,
                action=(change.tell, change.by, change.hold_report, change.line),
                change=number,
            )
        )
    return sorted(steps, key=lambda each: each.playbook)


def conditions(code: str) -> list[FeedFacts | None]:
    """Every condition an exception of this code can have: feed facts only for a feed's code."""
    if code not in PRODUCED.values():
        return [None]
    every = product(get_args(SourceFamily), get_args(Custodian), get_args(FeedStatus))
    return [None] + [
        FeedFacts(source_family=family, custodian=custodian, feed_status=status)
        for family, custodian, status in every
    ]


def fitting(steps: Iterable[Placed], code: str, facts: FeedFacts | None) -> list[Placed]:
    return [each for each in steps if each.code == code and matching.covers(each.when, facts)]


def described(code: str, facts: FeedFacts | None) -> str:
    if facts is None:
        return f"{code} with no feed facts"
    return (
        f"{code} on a {facts.feed_status} {facts.source_family} feed of custodian {facts.custodian}"
    )
