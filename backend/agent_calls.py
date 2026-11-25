"""What every question to either agent shares: how its calls are counted and recorded."""

import hashlib
import json
from collections.abc import Sequence
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import ColumnElement, Connection, RowMapping, Table, and_, func, select

from backend import models

REFUSALS_BEFORE_FALLBACK = 2
# The status of a move that comes from the wrong state, turn or attempt. A call refused
# with it was never checked, so it is not one of the two refusals.
OUT_OF_PLACE = 409
# A call that brought no answer back: kept as a step so that what it may have cost is counted,
# and never a refused answer, so it takes no turn of the model's and leads to no fallback.
NO_ANSWER = 202


def refusal_status(reason: models.UnusableReason) -> int:
    """The status kept for an unusable reply: a refused answer's, or a call's with no answer."""
    return NO_ANSWER if reason == "no_answer" else 200


GAP_UNUSABLE: dict[models.UnusableReason, str] = {
    "no_tool_call": "Answer with exactly one tool call: file_gap_report or no_gap_report.",
    "several_tool_calls": "Make exactly one tool call; none of those was recorded.",
    "unknown_tool": "There is no such tool: use file_gap_report or no_gap_report.",
    "invalid_arguments": "The arguments do not fit the tool: file_gap_report takes a summary.",
    "no_answer": "No answer came back from the model; the most the call could have cost is "
    "counted as spent.",
}


def digest(operation: str, body: BaseModel) -> str:
    """The hash of a move as sent, so that the same move sent again is known."""
    sent = json.dumps([operation, body.model_dump(mode="json")], sort_keys=True)
    return hashlib.sha256(sent.encode()).hexdigest()


def written(
    connection: Connection,
    table: Table,
    where: Sequence[ColumnElement[bool]],
    usage: models.Usage,
    tools: Sequence[str | None],
    *,
    option: str | None = None,
    refusal: tuple[int, str] | None = None,
) -> list[dict]:
    """One reply's calls as a line holds them."""
    # A turn holds more than one move when a call out of place was refused in it.
    last = connection.execute(
        select(func.coalesce(func.max(table.c.call), 0)).where(*where)
    ).scalar_one()
    # A reply's usage is counted once, however many calls it held.
    uncounted = {"input_tokens": None, "output_tokens": None, "cost_usd": None}
    return [
        {
            "call": last + number,
            "tool": tool,
            "option": option,
            "outcome": "refused" if refusal else "accepted",
            "refused_status": refusal[0] if refusal else None,
            "line": refusal[1] if refusal else None,
            **usage.model_dump(),
            **({} if number == 1 else uncounted),
        }
        for number, tool in enumerate(tools, start=1)
    ]


def refused_turns(
    connection: Connection,
    table: Table,
    where: Sequence[ColumnElement[bool]],
    checked: ColumnElement[bool] | None = None,
) -> list[str]:
    """The line of each refused turn, in order, whichever attempt it was in.

    `checked` says which refused calls were checked, where the status alone does not.
    """
    if checked is None:
        checked = table.c.refused_status != OUT_OF_PLACE
    checked = and_(checked, table.c.refused_status != NO_ANSWER)
    found = connection.execute(
        select(table.c.turn, table.c.line)
        .where(*where, table.c.outcome == "refused", checked)
        .distinct()
        .order_by(table.c.turn)
    )
    return [row.line for row in found]


def total(steps: Sequence[RowMapping]) -> models.UsageTotal:
    taken = [step["input_tokens"] for step in steps if step["input_tokens"] is not None]
    given = [step["output_tokens"] for step in steps if step["output_tokens"] is not None]
    costs = [Decimal(step["cost_usd"]) for step in steps if step["cost_usd"] is not None]
    return models.UsageTotal(
        models=sorted({step["model"] for step in steps}),
        input_tokens=sum(taken) if taken else None,
        output_tokens=sum(given) if given else None,
        cost_usd=f"{sum(costs, Decimal(0)):.6f}" if costs else None,
    )
