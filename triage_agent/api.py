"""One function for each of the backend's operations the triage agent may call."""

from typing import Literal

import httpx2
from pydantic import BaseModel, ConfigDict

from triage_agent.connection import Connection

FailureReason = Literal[
    "model_failed", "cost_cap", "recording_mismatch", "backend_refused", "unexpected_error"
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


class When(_Answer):
    source_family: str
    custodian: str
    feed_status: str


class Feed(When):
    pass


class Offered(_Answer):
    playbook: str
    step: str
    when: When
    line: str


class PendingNote(_Answer):
    id: str
    code: str
    feed: Feed | None
    warning_count: int
    options: list[Offered]
    refusals: list[str]


class UncoveredNote(_Answer):
    id: str
    code: str
    feed: Feed | None
    warning_count: int
    refusals: list[str]


class Work(_Answer):
    state: str
    spent_usd: str | None
    notes: list[PendingNote]
    uncovered: list[UncoveredNote]


class ReviewWork(_Answer):
    state: str
    spent_usd: str | None
    code: str
    feed: Feed | None
    warning_count: int
    step: Offered
    version: int
    field: str
    should_be: bool | str
    reason: str
    refusals: list[str]


class Usage(_Answer):
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: str | None


def _path(run_id: str, attempt: int, rest: str = "") -> str:
    return f"/api/agent/triage-runs/{run_id}/attempts/{attempt}{rest}"


def _done(response: httpx2.Response) -> dict:
    if response.status_code < 400:
        return response.json()
    try:
        line = str(response.json()["detail"])
    except (ValueError, KeyError, TypeError):
        line = "The backend refused."
    raise BackendRefused(response.status_code, line)


def read_triage_work(connection: Connection, run_id: str, attempt: int) -> Work:
    return Work.model_validate(_done(connection.send("GET", _path(run_id, attempt))))


def record_choice(
    connection: Connection,
    run_id: str,
    attempt: int,
    note_id: str,
    turn: int,
    choice: dict,
    usage: Usage,
) -> None:
    body = {"turn": turn, "choice": choice, "usage": usage.model_dump()}
    _done(connection.send("POST", _path(run_id, attempt, f"/notes/{note_id}/choice"), body))


def record_unusable_reply(
    connection: Connection,
    run_id: str,
    attempt: int,
    note_id: str,
    turn: int,
    reason: UnusableReason,
    calls: list[str],
    usage: Usage,
) -> str:
    body = {"turn": turn, "reason": reason, "calls": calls, "usage": usage.model_dump()}
    path = _path(run_id, attempt, f"/notes/{note_id}/unusable-reply")
    return str(_done(connection.send("POST", path, body))["refusal"])


def record_fallback(connection: Connection, run_id: str, attempt: int, note_id: str) -> None:
    _done(connection.send("POST", _path(run_id, attempt, f"/notes/{note_id}/fallback")))


def complete_triage(connection: Connection, run_id: str, attempt: int) -> None:
    _done(connection.send("POST", _path(run_id, attempt, "/complete")))


def fail_triage(connection: Connection, run_id: str, attempt: int, reason: FailureReason) -> None:
    _done(connection.send("POST", _path(run_id, attempt, "/fail"), {"reason": reason}))


def file_gap_report(
    connection: Connection,
    run_id: str,
    attempt: int,
    note_id: str,
    turn: int,
    summary: str,
    usage: Usage,
) -> None:
    body = {"turn": turn, "summary": summary, "usage": usage.model_dump()}
    _done(connection.send("POST", _path(run_id, attempt, f"/notes/{note_id}/gap-report"), body))


def record_no_gap_report(
    connection: Connection, run_id: str, attempt: int, note_id: str, turn: int, usage: Usage
) -> None:
    body = {"turn": turn, "usage": usage.model_dump()}
    _done(connection.send("POST", _path(run_id, attempt, f"/notes/{note_id}/no-gap-report"), body))


def _review(correction_id: str, attempt: int, rest: str = "") -> str:
    return f"/api/agent/corrections/{correction_id}/attempts/{attempt}{rest}"


def read_review_work(connection: Connection, correction_id: str, attempt: int) -> ReviewWork:
    return ReviewWork.model_validate(_done(connection.send("GET", _review(correction_id, attempt))))


def record_review(
    connection: Connection,
    correction_id: str,
    attempt: int,
    turn: int,
    review: dict,
    usage: Usage,
) -> None:
    body = {"turn": turn, "review": review, "usage": usage.model_dump()}
    _done(connection.send("POST", _review(correction_id, attempt, "/review"), body))


def record_unusable_review(
    connection: Connection,
    correction_id: str,
    attempt: int,
    turn: int,
    reason: UnusableReason,
    calls: list[str],
    usage: Usage,
) -> str:
    body = {"turn": turn, "reason": reason, "calls": calls, "usage": usage.model_dump()}
    path = _review(correction_id, attempt, "/unusable-reply")
    return str(_done(connection.send("POST", path, body))["refusal"])


def record_review_fallback(connection: Connection, correction_id: str, attempt: int) -> None:
    _done(connection.send("POST", _review(correction_id, attempt, "/fallback")))


def stop_review(
    connection: Connection, correction_id: str, attempt: int, reason: FailureReason
) -> None:
    _done(connection.send("POST", _review(correction_id, attempt, "/stop"), {"reason": reason}))
