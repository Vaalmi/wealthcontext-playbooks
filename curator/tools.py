"""The two tools, and the run context they read their ids from.

The model names a step by its playbook and step, and what a change rests on by the
bundle's own references. The backend checks both, whatever a schema offered.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from curator import api
from curator.connection import Connection

SUBMIT_PROPOSAL = "submit_proposal"
NO_CHANGE = "no_change"
NAMES = (SUBMIT_PROPOSAL, NO_CHANGE)
# A call the backend refuses is the model's to read. It goes back once.
FOR_THE_MODEL = (409, 422)
CITABLE = ("firm_source", "gap_report")

# The closed values the backend takes. A test holds them to the backend's own.
CODES = (
    "AMBIGUOUS",
    "AMBIGUOUS_ENTITY",
    "PRICE_STALE",
    "SOURCE_UNAVAILABLE",
    "FX_UNAVAILABLE",
    "HOUSEHOLD_MEMBERSHIP_CONFLICT",
    "REPORTING_HOUSEHOLD_CONFLICT",
    "OWNERSHIP_CONFLICT",
    "OWNERSHIP_UNAVAILABLE",
    "PENDING_SETTLEMENT",
    "QUANTITY_BREAK",
    "CASH_BREAK",
    "RECONCILIATION_UNAVAILABLE",
    "PRICE_SOURCE_MISMATCH",
    "CLOSED_ACCOUNT_WITH_BALANCE",
    "HELD_AWAY_EXCLUDED",
)
WHEN = {
    "source_family": ("PAS", "CUSTODY", "any"),
    "custodian": ("A", "B", "any"),
    "feed_status": ("FAILED", "PARTIAL", "TIMEOUT", "any"),
}
MOST_ITEMS = 4096
MOST_TEXT = 100_000
MOST_VERSION = 99_999


def _closed(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _one_of(values: Sequence[str]) -> dict[str, Any]:
    # An empty list is no schema. Then it is text, and the backend refuses what is not there.
    return {"type": "string", "enum": list(values)} if values else {"type": "string"}


def proposal_schema(desks: Sequence[str], references: Sequence[str]) -> dict[str, Any]:
    """A proposal's schema, written out so that each list and each closed object stays one."""
    text = {"type": "string"}
    named = {"type": "array", "items": _one_of(references)}
    written = {
        "step": text,
        "code": _one_of(CODES),
        "when": _closed({name: _one_of(values) for name, values in WHEN.items()}),
        "tell": _one_of(desks),
        "by": text,
        "hold_report": {"type": "boolean"},
        "line": text,
    }
    version = {"type": "integer"}
    added = {
        "change": _one_of(("add",)),
        "playbook": text,
        "version": {"anyOf": [version, {"type": "null"}]},
        **written,
        "rests_on": named,
    }
    changed = added | {"change": _one_of(("change",)), "version": version}
    removed = {
        "change": _one_of(("remove",)),
        "playbook": text,
        "version": version,
        "step": text,
        "rests_on": named,
    }
    changes = {"anyOf": [_closed(added), _closed(changed), _closed(removed)]}
    disagreement = _closed({"between": named, "about": text})
    return _closed(
        {
            "changes": {"type": "array", "items": changes},
            "disagreements": {"type": "array", "items": disagreement},
        }
    )


# What arguments must fit to be sent on. A desk or a reference that is not on its list is
# sent on, so that the backend refuses it by name of the rule, and records that it did.
SHAPE = {SUBMIT_PROPOSAL: proposal_schema((), ()), NO_CHANGE: _closed({})}


def _is_text(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > MOST_TEXT:
        return False
    try:
        value.encode()
    except UnicodeEncodeError:
        return False  # it could not be sent
    return True


def _fits(value: Any, schema: dict[str, Any]) -> bool:
    if "anyOf" in schema:
        return any(_fits(value, each) for each in schema["anyOf"])
    match schema["type"]:
        case "object":
            properties = schema["properties"]
            return (
                isinstance(value, dict)
                and value.keys() == properties.keys()
                and all(_fits(value[name], rules) for name, rules in properties.items())
            )
        case "array":
            return (
                isinstance(value, list)
                and len(value) <= MOST_ITEMS
                and all(_fits(each, schema["items"]) for each in value)
            )
        case "string":
            return _is_text(value) and ("enum" not in schema or value in schema["enum"])
        case "integer":  # the one number here is a version
            return type(value) is int and 1 <= value <= MOST_VERSION
        case "boolean":
            return isinstance(value, bool)
        case _:  # null
            return value is None


def fits(name: str, arguments: dict[str, Any]) -> bool:
    """Whether arguments can be sent to the backend. A model's are never trusted to."""
    return _fits(arguments, SHAPE[name])


@dataclass
class RunContext:
    """Whose answer this is. No tool takes any of it from the model."""

    connection: Connection
    run_id: str
    attempt: int
    turn: int = 1
    usage: api.Usage | None = None

    def unusable(self, reason: api.UnusableReason, calls: list[str]) -> str:
        assert self.usage is not None  # set by the model's node
        of_run = (self.connection, self.run_id, self.attempt)
        return api.record_unusable_curator_reply(*of_run, self.turn, reason, calls, self.usage)

    def give_up(self) -> None:
        api.fail_curator_run(self.connection, self.run_id, self.attempt, "refused_twice")


def _answered(move: Callable[[], None]) -> tuple[str, bool]:
    try:
        move()
    except api.BackendRefused as refused:
        if refused.status not in FOR_THE_MODEL:
            raise
        return refused.line, True
    return "Recorded.", False


def build(context: RunContext, items: Sequence[api.Item]) -> list[BaseTool]:
    """The two tools for one bundle. Their lists are made from it, for this call."""
    references = sorted(item.reference for item in items if item.kind in CITABLE)
    desks = [desk for item in items for desk in item.content.get("desks", ())]
    of_run = (context.connection, context.run_id, context.attempt)

    def submit_proposal(changes: list, disagreements: list) -> tuple[str, bool]:
        usage = context.usage
        assert usage is not None  # set from the reply that made the call
        proposal = {"changes": changes, "disagreements": disagreements}
        return _answered(lambda: api.record_proposal(*of_run, context.turn, proposal, usage))

    def no_change() -> tuple[str, bool]:
        usage = context.usage
        assert usage is not None
        return _answered(lambda: api.record_no_change(*of_run, context.turn, usage))

    return [
        StructuredTool.from_function(
            submit_proposal,
            name=SUBMIT_PROPOSAL,
            description=(
                "Submit the one proposal: each step to add, change or remove, what each rests "
                "on, and each disagreement among the sources."
            ),
            args_schema=proposal_schema(desks, references),
            response_format="content_and_artifact",
        ),
        StructuredTool.from_function(
            no_change,
            name=NO_CHANGE,
            description="Say that the bundle calls for no change to any playbook.",
            args_schema=_closed({}),
            response_format="content_and_artifact",
        ),
    ]
