"""Curator runs and proposals, as the tests make them through the API."""

import copy
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import knowledge
from layer import CROSS_CHECK_DAY, DAY
from stand_ins import idle_agent

from backend import triage

MEMO = "memo-custodian-b-feed-escalation"
FOUNDING = "memo-feed-outage-founding"
CROSS_CHECK_FOUNDING = "memo-missing-cross-check-founding"
CORRECTION = knowledge.committed_correction()["id"]
FIRST_CYCLE = (
    "feed-outage-founding",
    "missing-cross-check-founding",
    "custodian-b-feed-escalation",
)
USAGE = {"model": "stub:curation", "input_tokens": 9, "output_tokens": 2, "cost_usd": "0.000100"}
FEED = {"source_family": "PAS", "feed_status": "FAILED"}
NARROWED = {
    "change": "change",
    "playbook": "feed-outage",
    "version": 1,
    "step": "portfolio-feed-failed",
    "code": "SOURCE_UNAVAILABLE",
    "when": FEED | {"custodian": "A"},
    "tell": "portfolio-accounting",
    "by": "same_day 20:00 ET",
    "hold_report": True,
    "line": "Ask for the failed portfolio accounting fetch to be run again.",
    "rests_on": [MEMO, FOUNDING],
}
ADDED = {
    "change": "add",
    "playbook": "feed-outage",
    "version": 1,
    "step": "custodian-b-feed-failed",
    "code": "SOURCE_UNAVAILABLE",
    "when": FEED | {"custodian": "B"},
    "tell": "custodian-b-service",
    "by": "within 2 hours",
    "hold_report": True,
    "line": "Ask the service desk for the failed fetch to be run again.",
    "rests_on": [MEMO],
}
REMOVED = {
    "change": "remove",
    "playbook": "missing-cross-check",
    "version": 1,
    "step": "custody-cross-check-missing",
    "rests_on": [CORRECTION],
}


def idle(*_: Any):
    return idle_agent()


def proposed(report: str | None = None, **changed: Any) -> dict:
    """A proposal that can be recorded on the first cycle's bundle, with every kind of change."""
    removed = copy.deepcopy(REMOVED)
    if report:
        removed["rests_on"].append(report)
    proposal = {
        "changes": [copy.deepcopy(NARROWED), copy.deepcopy(ADDED), removed],
        "disagreements": [
            {
                "between": [CROSS_CHECK_FOUNDING, CORRECTION],
                "about": "Whether a missing cross-check alone holds the report.",
            }
        ],
    }
    return proposal | changed


def one(**changes: Any) -> dict:
    """A proposal of one change."""
    return {"changes": [copy.deepcopy(NARROWED) | changes], "disagreements": []}


def first_cycle(layer, summary: str = knowledge.SUMMARY, memos=FIRST_CYCLE) -> dict[str, Any]:
    """The first cycle's sources and events. The triage agent's moves are made with its key,
    so the layer's agent is one that does nothing.
    """
    for name in memos:
        assert knowledge.intake(layer, knowledge.committed_memo(name)).status_code == 200
    runs = [knowledge.settled_run(layer, day) for day in (DAY, CROSS_CHECK_DAY)]
    made = knowledge.corrected(layer, runs[1])
    assert knowledge.review(layer, made["id"], summary).status_code == 200
    report = knowledge.gap_reports(layer)[-1]
    return {"runs": runs, "correction": made, "report": report}


def curate(layer):
    return layer.as_owner("POST", "/api/curator-runs")


def started(layer) -> str:
    response = curate(layer)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def read(layer, run_id: str) -> dict:
    response = layer.as_owner("GET", f"/api/curator-runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def ended(layer, run_id: str) -> dict:
    until = time.monotonic() + 15
    while (found := read(layer, run_id))["state"] == "working":
        assert time.monotonic() < until, "the curator run did not end"
        time.sleep(0.02)
    return found


def curated(layer) -> dict:
    return ended(layer, started(layer))


def work_path(run_id: str, rest: str = "", attempt: int = 1) -> str:
    return f"/api/agent/curator-runs/{run_id}/attempts/{attempt}{rest}"


def work(layer, run_id: str, attempt: int = 1) -> dict:
    response = layer.as_curator("GET", work_path(run_id, attempt=attempt))
    assert response.status_code == 200, response.text
    return response.json()


def items(layer, kind: str, run_id: str) -> list[dict]:
    return [each for each in work(layer, run_id)["items"] if each["kind"] == kind]


def propose(layer, run_id: str, proposal: dict, turn: int = 1, attempt: int = 1, usage=None):
    body = {"turn": turn, "proposal": proposal, "usage": usage or USAGE}
    return layer.as_curator("POST", work_path(run_id, "/proposal", attempt), json=body)


def no_change(layer, run_id: str, turn: int = 1, attempt: int = 1):
    body = {"turn": turn, "usage": USAGE}
    return layer.as_curator("POST", work_path(run_id, "/no-change", attempt), json=body)


def unusable(layer, run_id: str, turn: int = 1, attempt: int = 1):
    body = {"turn": turn, "reason": "no_tool_call", "calls": [], "usage": USAGE}
    return layer.as_curator("POST", work_path(run_id, "/unusable-reply", attempt), json=body)


def fail(layer, run_id: str, reason: str = "unexpected_error", attempt: int = 1):
    path = work_path(run_id, "/fail", attempt)
    return layer.as_curator("POST", path, json={"reason": reason})


def later(monkeypatch):
    """Move the backend's clock on. Returns the function that does it."""
    ahead = [timedelta(0)]
    monkeypatch.setattr(triage, "_clock", lambda: datetime.now(UTC) + ahead[0])

    def move(**by: float) -> None:
        ahead[0] += timedelta(**by)

    return move
