"""One function for each of the backend's operations the curator may call."""

from typing import Any, Literal

import httpx2
from pydantic import BaseModel, ConfigDict

from curator.connection import Connection

FailureReason = Literal[
    "refused_twice",
    "model_failed",
    "cost_cap",
    "recording_mismatch",
    "backend_refused",
    "unexpected_error",
]
UnusableReason = Literal[
    "no_tool_call", "several_tool_calls", "unknown_tool", "invalid_arguments", "no_answer"
]


class BackendRefused(Exception):
    def __init__(self, status: int, line: str):
        super().__init__(line)
        self.status = status
        self.line = line


class _Answer(BaseModel):
    model_config = ConfigDict(frozen=True)


class Item(_Answer):
    kind: str
    reference: str
    content: dict[str, Any]


class Work(_Answer):
    state: str
    spent_usd: str | None
    items: list[Item]
    refusals: list[str]


class Usage(_Answer):
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: str | None


def _path(run_id: str, attempt: int, rest: str = "") -> str:
    return f"/api/agent/curator-runs/{run_id}/attempts/{attempt}{rest}"


def _done(response: httpx2.Response) -> dict:
    if response.status_code < 400:
        return response.json()
    try:
        line = str(response.json()["detail"])
    except (ValueError, KeyError, TypeError):
        line = "The backend refused."
    raise BackendRefused(response.status_code, line)


def read_curator_work(connection: Connection, run_id: str, attempt: int) -> Work:
    return Work.model_validate(_done(connection.send("GET", _path(run_id, attempt))))


def record_proposal(
    connection: Connection, run_id: str, attempt: int, turn: int, proposal: dict, usage: Usage
) -> None:
    body = {"turn": turn, "proposal": proposal, "usage": usage.model_dump()}
    _done(connection.send("POST", _path(run_id, attempt, "/proposal"), body))


def record_no_change(
    connection: Connection, run_id: str, attempt: int, turn: int, usage: Usage
) -> None:
    body = {"turn": turn, "usage": usage.model_dump()}
    _done(connection.send("POST", _path(run_id, attempt, "/no-change"), body))


def record_unusable_curator_reply(
    connection: Connection,
    run_id: str,
    attempt: int,
    turn: int,
    reason: UnusableReason,
    calls: list[str],
    usage: Usage,
) -> str:
    body = {"turn": turn, "reason": reason, "calls": calls, "usage": usage.model_dump()}
    path = _path(run_id, attempt, "/unusable-reply")
    return str(_done(connection.send("POST", path, body))["refusal"])


def fail_curator_run(
    connection: Connection, run_id: str, attempt: int, reason: FailureReason
) -> None:
    _done(connection.send("POST", _path(run_id, attempt, "/fail"), {"reason": reason}))
