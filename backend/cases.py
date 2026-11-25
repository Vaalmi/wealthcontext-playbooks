"""The held-out cases: their format, the case set's hash, and grading notes by fixed rules.

A case is two parts kept in two places: its inputs (`<kind>/<case>.yaml`), the WealthContext
envelopes of one day and the exceptions the layer builds from them, and its expected notes,
which the case set's writer gave for every case of that kind at once
(`<kind>/expected-notes.json`), filed as it gave them. `case-set.yaml` names the writer.

A note is graded on what it tells a person to do: whether a step applies, the playbook it
is in, who is told, by when, and whether the report is held. A step's ID is not graded: the
writer names no step a proposal has yet to add. No model grades anything here.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    ValidationError,
    model_validator,
)

from backend import exceptions, matching
from backend.exceptions import ExceptionFound, Notice
from backend.playbook_format import By, Slug, When, content_hash
from backend.vocabulary import FeedFacts, WarningCode
from backend.wealthcontext import WealthContextFailed, read_envelopes

Kind = Literal["regression", "new_rule"]
FOLDERS: dict[Kind, str] = {"regression": "regression", "new_rule": "new-rule"}
CASE_SET = "case-set.yaml"
EXPECTED = "expected-notes.json"
CASES_OF_A_KIND = 4
MOST_EXCEPTIONS = 3
ExceptionId = Annotated[str, StringConstraints(pattern=r"^e[1-9]$")]
GRADED = ("outcome", "playbook", "tell", "by", "hold_report")


class CasesInvalid(Exception):
    """The case set cannot be read, or does not hold together. The line names no content."""


def _day(value: str) -> str:
    date.fromisoformat(value)
    return value


def _instant(value: str) -> str:
    if datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("a cutoff is an instant, with its offset")
    return value


class _Closed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ListedException(_Closed):
    id: ExceptionId
    code: WarningCode
    subjects: list[str]
    feed_facts: FeedFacts | None


class CaseInput(_Closed):
    case: Slug
    kind: Kind
    date: Annotated[str, AfterValidator(_day)]
    cutoff: Annotated[str, AfterValidator(_instant)]
    about: str
    exceptions: Annotated[list[ListedException], Field(min_length=1, max_length=MOST_EXCEPTIONS)]
    notices: list[Notice]
    envelopes: dict[str, Any]

    @model_validator(mode="after")
    def named_once(self) -> Self:
        if len({e.id for e in self.exceptions}) != len(self.exceptions):
            raise ValueError("each exception has an ID of its own")
        return self


class ExpectedNote(_Closed):
    exception: ExceptionId
    outcome: Literal["step", "no_step"]
    playbook: Slug | None
    tell: Slug | None
    by: By | None
    hold_report: StrictBool | None

    @model_validator(mode="after")
    def whole(self) -> Self:
        given = [self.playbook, self.tell, self.by, self.hold_report]
        if self.outcome == "step" and None in given:
            raise ValueError("a step's note names its playbook, who is told, by when and the hold")
        if self.outcome == "no_step" and any(each is not None for each in given):
            raise ValueError("a note with no step names nothing else")
        return self


class ExpectedCase(_Closed):
    case: Slug
    notes: list[ExpectedNote]


class ExpectedNotes(_Closed):
    cases: list[ExpectedCase]


class Writer(_Closed):
    name: str
    # Matched against a model's setting, whatever its provider or effort: `gemini-4-neon`.
    model: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.-]{2,63}$")]


class CaseSetFile(_Closed):
    writer: Writer


@dataclass(frozen=True)
class Graded:
    """What a note tells a person to do, as it is graded."""

    outcome: Literal["step", "no_step", "unverified"]
    playbook: str | None = None
    tell: str | None = None
    by: str | None = None
    hold_report: bool | None = None

    def differs(self, expected: ExpectedNote) -> list[str]:
        """The graded fields on which this note is not the expected one. Never their values."""
        return [name for name in GRADED if getattr(self, name) != getattr(expected, name)]


@dataclass(frozen=True)
class Case:
    id: str
    kind: Kind
    exceptions: list[ExceptionFound]
    # The ID each exception has in the case file, in the order they are built.
    ids: list[str]
    expected: dict[str, ExpectedNote]


@dataclass(frozen=True)
class CaseSet:
    sha256: str
    writer: Writer
    cases: list[Case]


def _yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_bytes())
    except (OSError, yaml.YAMLError):
        raise CasesInvalid(f"{path.name} cannot be read") from None


def _inputs(folder: Path, kind: Kind) -> list[CaseInput]:
    made = []
    for path in sorted((folder / FOLDERS[kind]).glob("*.yaml")):
        try:
            case = CaseInput.model_validate(_yaml(path))
        except ValidationError:
            raise CasesInvalid(f"{FOLDERS[kind]}/{path.name} is not a case") from None
        if case.kind != kind or f"{case.case}.yaml" != path.name:
            raise CasesInvalid(f"{FOLDERS[kind]}/{path.name} is not a {kind} case of that name")
        made.append(case)
    if len(made) != CASES_OF_A_KIND:
        raise CasesInvalid(f"{FOLDERS[kind]} holds {len(made)} cases, not {CASES_OF_A_KIND}")
    return made


def built(case: CaseInput) -> list[ExceptionFound]:
    """The exceptions the layer builds from the case's envelopes, which the case must list."""
    try:
        reading = read_envelopes(case.date, case.cutoff, case.envelopes)
    except WealthContextFailed:
        raise CasesInvalid(f"case {case.case}: its envelopes cannot be read") from None
    found, notices = exceptions.build(reading)
    listed = [(e.code, e.subjects, e.feed_facts) for e in case.exceptions]
    if [(e.code, e.subjects, e.facts) for e in found] != listed or notices != case.notices:
        raise CasesInvalid(f"case {case.case}: its envelopes do not build what it lists")
    return found


def answer_json(text: str) -> Any:
    """The one JSON object in a writer's answer, with or without a fence around it."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("the answer holds no JSON object")
    return json.loads(text[start : end + 1])


def shape_errors(
    given: Any, inputs: Sequence[CaseInput], desks: Iterable[str], kind: Kind
) -> list[str]:
    """What is wrong with the shape of a kind's expected notes, in lines that quote none of it.

    Shape only: every case and exception once, each note whole, a desk on the list and a
    deadline in one of its forms. Whether a note is right is not judged here.
    """
    try:
        notes = ExpectedNotes.model_validate(given)
    except ValidationError as invalid:
        where = sorted({".".join(str(p) for p in error["loc"]) for error in invalid.errors()})
        return [f"not in the asked shape at: {', '.join(where[:8])}"]
    errors = []
    wanted = {case.case: case for case in inputs if case.kind == kind}
    named = [each.case for each in notes.cases]
    if sorted(named) != sorted(wanted):
        errors.append(f"the cases named are not exactly these, each once: {', '.join(wanted)}")
    known = set(desks)
    for each in notes.cases:
        if each.case not in wanted:
            continue
        ids = sorted(e.id for e in wanted[each.case].exceptions)
        if sorted(note.exception for note in each.notes) != ids:
            errors.append(f"case {each.case}: not one note for each of {', '.join(ids)}")
        if any(note.tell is not None and note.tell not in known for note in each.notes):
            errors.append(f"case {each.case}: a desk that is not on the list")
    return errors


def _files(folder: Path) -> list[Path]:
    """The case set's files. A kind's folder holds its cases and its notes, and nothing else,
    so that the hash is of exactly the files read."""
    files = [folder / CASE_SET]
    for kind in FOLDERS.values():
        found = sorted((folder / kind).rglob("*"))
        for path in found:
            if path.parent != folder / kind or not path.is_file() or path.is_symlink():
                raise CasesInvalid(f"{kind} holds something that is not a case file")
            if path.suffix != ".yaml" and path.name != EXPECTED:
                raise CasesInvalid(f"{kind} holds a file that is no case: {path.name}")
        files += found
    return files


def _hash(files: list[Path], folder: Path) -> str:
    """One hash over the case set's files: each path and the hash of its bytes."""
    try:
        listed = [
            f"{path.relative_to(folder).as_posix()} {content_hash(path.read_bytes())}"
            for path in files
        ]
    except OSError:
        raise CasesInvalid("a case file cannot be read") from None
    return content_hash("\n".join(listed).encode())


def writer_of(folder: Path) -> Writer | None:
    """The cases' writer, from the case set's file alone, or None when there is none to read."""
    try:
        return CaseSetFile.model_validate(_yaml(folder / CASE_SET)).writer
    except (CasesInvalid, OSError, ValidationError):
        return None


def set_sha256(folder: Path) -> str:
    """The case set's hash, as `read_set` takes it, without reading a case."""
    return _hash(_files(folder), folder)


def read_set(folder: Path, desks: Iterable[str]) -> CaseSet:
    """The whole case set, checked: four cases of each kind, each built as it lists, and the
    writer's notes for every exception. Its hash is of exactly the files read."""
    if not (folder / CASE_SET).is_file():
        raise CasesInvalid("there is no case set")
    try:
        named = CaseSetFile.model_validate(_yaml(folder / CASE_SET))
    except ValidationError:
        raise CasesInvalid(f"{CASE_SET} does not name the writer") from None
    desks = list(desks)
    files = _files(folder)
    cases = []
    for kind, folder_name in FOLDERS.items():
        inputs = _inputs(folder, kind)
        try:
            given = answer_json((folder / folder_name / EXPECTED).read_text())
        except (OSError, ValueError):
            raise CasesInvalid(f"{folder_name}/{EXPECTED} cannot be read") from None
        if shape_errors(given, inputs, desks, kind):
            raise CasesInvalid(f"{folder_name}/{EXPECTED} is not in its shape")
        notes = {each.case: each for each in ExpectedNotes.model_validate(given).cases}
        for case in inputs:
            cases.append(
                Case(
                    id=case.case,
                    kind=kind,
                    exceptions=built(case),
                    ids=[e.id for e in case.exceptions],
                    expected={note.exception: note for note in notes[case.case].notes},
                )
            )
    return CaseSet(sha256=_hash(files, folder), writer=named.writer, cases=cases)


def _plain(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def written_by(setting: str, writer: Writer) -> bool:
    """Whether a model's setting names the writer's model, whatever its provider or effort."""
    return bool(setting) and _plain(writer.model) in _plain(setting)


# Grading by fixed rules


@dataclass(frozen=True)
class Rule:
    """A step as it would stand, for grading: where it is, when it applies and its action."""

    playbook: str
    code: str
    when: When
    tell: str
    by: str
    hold_report: bool


def by_fixed_rules(case: Case, rules: Sequence[Rule]) -> dict[str, Graded]:
    """Each exception's note, worked out from the steps alone: the one that fits, or none.

    Two steps that fit one exception are no note anyone could give, so it is graded as a
    note with no verified choice.
    """
    notes = {}
    for exception_id, exception in zip(case.ids, case.exceptions, strict=True):
        fits = [
            rule
            for rule in rules
            if rule.code == exception.code and matching.covers(rule.when, exception.facts)
        ]
        if len(fits) == 1:
            (rule,) = fits
            notes[exception_id] = Graded(
                "step", rule.playbook, rule.tell, rule.by, rule.hold_report
            )
        else:
            notes[exception_id] = Graded("no_step" if not fits else "unverified")
    return notes


def misses(case: Case, given: Mapping[str, Graded]) -> list[tuple[str, list[str]]]:
    """Each exception whose note is not the expected one, with the fields that differ."""
    found = []
    for exception_id in case.ids:
        note = given.get(exception_id, Graded("unverified"))
        if wrong := note.differs(case.expected[exception_id]):
            found.append((exception_id, wrong))
    return found
