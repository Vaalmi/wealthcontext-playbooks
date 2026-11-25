"""The tools, and the run context they read their ids from.

A choice of step takes no free text. One argument is text: a gap report's summary, which
the backend checks before any of it is kept.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from triage_agent import api
from triage_agent.connection import Connection

CHOOSE_STEP = "choose_step"
NO_STEP_APPLIES = "no_step_applies"
FILE_GAP_REPORT = "file_gap_report"
NO_GAP_REPORT = "no_gap_report"
NAMES = (CHOOSE_STEP, NO_STEP_APPLIES, FILE_GAP_REPORT, NO_GAP_REPORT)
# A tool call the backend refuses is the model's to read, for its choice or for the run's
# state or attempt. It goes back once: the backend holds the line whatever comes next.
FOR_THE_MODEL = (409, 422)


@dataclass
class NoteContext:
    """Whose choice this is. No tool takes any of it from the model."""

    connection: Connection
    run_id: str
    attempt: int
    note: api.PendingNote
    turn: int = 1
    usage: api.Usage | None = None

    def unusable(self, reason: api.UnusableReason, calls: list[str]) -> str:
        assert self.usage is not None  # set by the model's node
        of_note = (self.connection, self.run_id, self.attempt, self.note.id)
        return api.record_unusable_reply(*of_note, self.turn, reason, calls, self.usage)

    def give_up(self) -> None:
        api.record_fallback(self.connection, self.run_id, self.attempt, self.note.id)


@dataclass
class GapContext:
    """Whose exception this is, when no playbook covers it."""

    connection: Connection
    run_id: str
    attempt: int
    note: api.UncoveredNote
    turn: int = 1
    usage: api.Usage | None = None

    def unusable(self, reason: api.UnusableReason, calls: list[str]) -> str:
        assert self.usage is not None
        of_note = (self.connection, self.run_id, self.attempt, self.note.id)
        return api.record_unusable_reply(*of_note, self.turn, reason, calls, self.usage)

    def give_up(self) -> None:
        """After a second refusal no report is filed: the note was settled before it was asked.

        The work is read again, so that an attempt that is no longer the run's stops here and
        pays for no question more.
        """
        api.read_triage_work(self.connection, self.run_id, self.attempt)


@dataclass
class ReviewContext:
    """Whose review this is."""

    connection: Connection
    correction_id: str
    attempt: int
    turn: int = 1
    usage: api.Usage | None = None

    def unusable(self, reason: api.UnusableReason, calls: list[str]) -> str:
        assert self.usage is not None
        of_review = (self.connection, self.correction_id, self.attempt)
        return api.record_unusable_review(*of_review, self.turn, reason, calls, self.usage)

    def give_up(self) -> None:
        api.record_review_fallback(self.connection, self.correction_id, self.attempt)


Context = NoteContext | GapContext | ReviewContext


def closed(properties: Mapping[str, Sequence[str] | None]) -> dict[str, Any]:
    """A schema in which every argument is one of a list, or is text, and nothing else is taken.

    Written out rather than made from a model class: LangChain drops `enum` and
    `additionalProperties` from a class's schema on the way to the provider.
    """
    return {
        "type": "object",
        "properties": {
            name: {"type": "string"} if of is None else {"type": "string", "enum": list(of)}
            for name, of in properties.items()
        },
        "required": list(properties),
        "additionalProperties": False,
    }


def _is_text(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 100_000:
        return False
    try:
        value.encode()
    except UnicodeEncodeError:
        return False  # it could not be sent
    return True


def fits(tool: BaseTool, arguments: dict[str, Any]) -> bool:
    """Whether arguments fit a tool's closed schema. A model's are never trusted to."""
    schema = tool.args_schema
    assert isinstance(schema, dict)  # every tool here has a schema from `closed`
    properties = schema["properties"]
    return arguments.keys() == properties.keys() and all(
        arguments[name] in rules["enum"] if "enum" in rules else _is_text(arguments[name])
        for name, rules in properties.items()
    )


def _answered(move: Callable[[], None]) -> tuple[str, bool]:
    try:
        move()
    except api.BackendRefused as refused:
        if refused.status not in FOR_THE_MODEL:
            raise
        return refused.line, True
    return "Recorded.", False


def build(context: NoteContext) -> list[BaseTool]:
    """The two tools for one exception. The options are a closed list made for this call."""
    offered = {f"{each.playbook}/{each.step}": each for each in context.note.options}

    def record(choice: dict) -> tuple[str, bool]:
        usage = context.usage
        assert usage is not None  # set from the reply that made the call
        of_note = (context.connection, context.run_id, context.attempt, context.note.id)
        return _answered(lambda: api.record_choice(*of_note, context.turn, choice, usage))

    def choose_step(option: str) -> tuple[str, bool]:
        step = offered[option]
        return record({"kind": "step", "playbook": step.playbook, "step": step.step})

    def no_step_applies() -> tuple[str, bool]:
        return record({"kind": "no_step"})

    return [
        StructuredTool.from_function(
            choose_step,
            name=CHOOSE_STEP,
            description="Choose the approved playbook step that applies to this exception.",
            args_schema=closed({"option": list(offered)}),
            response_format="content_and_artifact",
        ),
        StructuredTool.from_function(
            no_step_applies,
            name=NO_STEP_APPLIES,
            description="Say that none of the listed steps applies to this exception.",
            args_schema=closed({}),
            response_format="content_and_artifact",
        ),
    ]


def build_gap(context: GapContext | ReviewContext) -> list[BaseTool]:
    """The two tools that answer whether to file a gap report, for a run or for a review."""

    def file_gap_report(summary: str) -> tuple[str, bool]:
        usage = context.usage
        assert usage is not None
        if isinstance(context, ReviewContext):
            of_review = (context.connection, context.correction_id, context.attempt)
            review = {"kind": "gap_report", "summary": summary}
            return _answered(lambda: api.record_review(*of_review, context.turn, review, usage))
        of_note = (context.connection, context.run_id, context.attempt, context.note.id)
        return _answered(lambda: api.file_gap_report(*of_note, context.turn, summary, usage))

    def no_gap_report() -> tuple[str, bool]:
        usage = context.usage
        assert usage is not None
        if isinstance(context, ReviewContext):
            of_review = (context.connection, context.correction_id, context.attempt)
            review = {"kind": "no_gap_report"}
            return _answered(lambda: api.record_review(*of_review, context.turn, review, usage))
        of_note = (context.connection, context.run_id, context.attempt, context.note.id)
        return _answered(lambda: api.record_no_gap_report(*of_note, context.turn, usage))

    return [
        StructuredTool.from_function(
            file_gap_report,
            name=FILE_GAP_REPORT,
            description=(
                "File a gap report. The summary is one plain line of at most 300 characters "
                "on what the firm's playbooks lack or get wrong."
            ),
            args_schema=closed({"summary": None}),
            response_format="content_and_artifact",
        ),
        StructuredTool.from_function(
            no_gap_report,
            name=NO_GAP_REPORT,
            description="Say that no gap report is to be filed.",
            args_schema=closed({}),
            response_format="content_and_artifact",
        ),
    ]
