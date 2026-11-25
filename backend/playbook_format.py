"""Parses and validates a playbook file: the step's fixed fields, and the content hash."""

import hashlib
import unicodedata
from typing import Annotated, Self

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

from backend.text_rules import figure_in
from backend.vocabulary import ANY, Any_, Custodian, FeedStatus, SourceFamily, WarningCode

LINE_LENGTH = 160
CLOCK = "([01][0-9]|2[0-3]):[0-5][0-9]"

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z]+(-[a-z]+)*$", max_length=64)]
# The three forms a deadline takes.
By = Annotated[
    str,
    StringConstraints(
        pattern=(
            rf"^(same_day {CLOCK} ET"
            r"|within ([1-9]|[1-6][0-9]|7[0-2]) hours"
            rf"|next_business_day {CLOCK} ET)$"
        )
    ),
]


def _line(value: str) -> str:
    if not value.strip() or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("a line is visible text on one line")
    if figure_in(value):
        raise ValueError("a line holds no number, currency or percentage")
    return value


Line = Annotated[str, StringConstraints(max_length=LINE_LENGTH), AfterValidator(_line)]


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class Fixed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class When(Fixed):
    source_family: SourceFamily | Any_
    custodian: Custodian | Any_
    feed_status: FeedStatus | Any_

    def fixed(self) -> list[tuple[str, str]]:
        """The conditions that are not `any`, in the order they are written."""
        return [(name, value) for name, value in self.model_dump().items() if value != ANY]


class Scope(Fixed):
    """What a firm source speaks for, in the same closed values a step uses."""

    playbook: Slug
    code: WarningCode
    when: When


class Step(Fixed):
    id: Slug
    code: WarningCode
    when: When
    tell: Slug
    by: By
    hold_report: StrictBool
    line: Line


class Playbook(Fixed):
    id: Slug
    version: StrictInt = Field(ge=1)
    replaces: StrictInt | None
    # None when an approved change removed the last: no approved step applies then.
    steps: list[Step]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if len({step.id for step in self.steps}) != len(self.steps):
            raise ValueError("step ids are unique within a playbook")
        if self.replaces is None and not self.steps:
            raise ValueError("a version that replaces none has a step")
        if self.replaces is not None and not 1 <= self.replaces < self.version:
            raise ValueError("a version replaces an earlier one")
        return self


def parse(content: bytes) -> Playbook:
    return Playbook.model_validate(yaml.safe_load(content))
