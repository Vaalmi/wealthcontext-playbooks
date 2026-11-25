"""Proposals brought to the gate and decided, as the tests do it through the API."""

import copy
from collections.abc import Callable
from typing import Any

import curation
import gating
import held_out_cases
import knowledge
from layer import CROSS_CHECK_DAY, CUTOFF, DAY
from stand_ins import fitting
from stub_model import call

CORRECTION = curation.CORRECTION
FOUNDING = curation.CROSS_CHECK_FOUNDING
NARROWED = "feed-outage/portfolio-feed-failed"
OTHER_SUMMARY = "A partial custody file held a report again, and the step may be wrong to hold it."
GAP_REPORT = next(
    each for each in gating.HONEST["changes"][2]["rests_on"] if each.startswith("gap-report-")
)
NEW_PLAYBOOK = "custodian-b-feed"


def scripted(make_layer, *proposals: dict | Callable[[], dict], **options):
    """A layer whose curator proposes these, one for each of its runs, on the gate's agent."""
    waiting = list(proposals)

    def answer():
        given = waiting.pop(0)
        return call("submit_proposal", **(given() if callable(given) else given))

    options.setdefault("agent", gating.trial_agent)
    return make_layer(curates=lambda: [answer], **options)


def drafted(layer) -> dict[str, Any]:
    run = curation.curated(layer)
    assert run["state"] == "completed", run
    return run["proposal"]


def passed(layer) -> tuple[dict[str, Any], dict[str, Any]]:
    """The next proposal, through the gate."""
    proposal = drafted(layer)
    run = gating.gated(layer, proposal["id"])
    assert run["outcome"] == "passed", run["checks"]
    return proposal, run


def first_cycle_passed(make_layer, **options) -> tuple[Any, dict, dict]:
    layer = scripted(make_layer, gating.honest(), **options)
    gating.first_cycle(layer)
    return (layer, *passed(layer))


def rulings(layer, proposal: dict, preferred: str | None = None) -> list[dict]:
    """A ruling on each disagreement the view lists: for the source preferred where it is in
    one, else as the first cycle's owner rules, for a correction or gap report over a memo.
    """
    response = layer.as_owner("GET", f"/api/proposals/{proposal['id']}")
    assert response.status_code == 200, response.text
    made = []
    for each in response.json()["to_rule_on"]:
        others = [source for source in each["between"] if not source.startswith("memo-")]
        winner = preferred if preferred in each["between"] else (others or each["between"])[0]
        made.append(each | {"ruled_for": winner})
    return made


def approve(layer, proposal: dict, run: dict, ruled=None, send=None, **named):
    ruled = rulings(layer, proposal) if ruled is None else ruled
    body = {"sha256": proposal["sha256"], "gate_run": run["id"], "rulings": ruled} | named
    send = send or layer.as_owner
    return send("POST", f"/api/proposals/{proposal['id']}/approve", json=body)


def approved(layer, proposal: dict, run: dict, ruled=None) -> dict[str, Any]:
    response = approve(layer, proposal, run, ruled)
    assert response.status_code == 200, response.text
    return response.json()


def refuse(layer, proposal_id: str, reason: str = "wrong_change", send=None):
    send = send or layer.as_owner
    return send("POST", f"/api/proposals/{proposal_id}/refuse", json={"reason": reason})


def listed(layer, proposal_id: str) -> dict[str, Any]:
    (found,) = [p for p in gating.record(layer)["proposals"] if p["id"] == proposal_id]
    return found


def state(layer, proposal_id: str) -> str:
    return listed(layer, proposal_id)["state"]


def reports(layer) -> dict[str, tuple[str, str | None]]:
    """Each gap report's state and the proposal it names."""
    found = knowledge.gap_reports(layer)
    return {each["id"]: (each["state"], each["proposal_id"]) for each in found}


def in_use(layer) -> dict[str, int]:
    found = layer.as_analyst("GET", "/api/playbooks").json()
    return {each["id"]: each["version"] for each in found["in_use"]}


def another_report(layer) -> dict[str, Any]:
    """A second gap report of other content, from another run's correction and its review."""
    run = triaged(layer, CROSS_CHECK_DAY)
    made = knowledge.corrected(layer, run)
    assert knowledge.review(layer, made["id"], OTHER_SUMMARY).status_code == 200
    (found,) = [r for r in knowledge.gap_reports(layer) if r["summary"] == OTHER_SUMMARY]
    return found


def resting_on(report: str) -> dict[str, Any]:
    """The honest proposal, with this gap report in place of the first cycle's."""
    proposal = gating.honest()
    swapped = {GAP_REPORT: report}
    for named in (
        *(change["rests_on"] for change in proposal["changes"]),
        *(told["between"] for told in proposal["disagreements"]),
    ):
        named[:] = [swapped.get(each, each) for each in named]
    return proposal


def triaged(layer, day: str = DAY) -> dict[str, Any]:
    """A run completed with the step that fits each note, the moves made with the agent's key."""
    run_id = layer.start(day, CUTOFF)["id"]
    base = f"/api/agent/triage-runs/{run_id}/attempts/1"
    for note in layer.as_agent("GET", base).json()["notes"]:
        body = {"turn": 1, "choice": fitting(note), "usage": knowledge.USAGE}
        made = layer.as_agent("POST", f"{base}/notes/{note['id']}/choice", json=body)
        assert made.status_code == 200, made.text
    assert layer.as_agent("POST", f"{base}/complete").status_code == 200
    return layer.run(run_id)


def feed_change() -> dict[str, Any]:
    """The custodian B memo's change alone, which no source disputes."""
    changes = [copy.deepcopy(curation.NARROWED), copy.deepcopy(curation.ADDED)]
    return {"changes": changes, "disagreements": []}


def feed_only(layer) -> None:
    held_out_cases.write(layer.settings.cases, held_out_cases.FEED_ONLY)
    for name in curation.FIRST_CYCLE:
        assert knowledge.intake(layer, knowledge.committed_memo(name)).status_code == 200


def new_playbook(make_layer, **options) -> tuple[Any, dict, dict]:
    """A proposal of a playbook that is new, with a memo whose scope names it, through the gate."""
    memo = knowledge.memo(
        "The service desk answers for custodian B's failed feed.",
        id="memo-a-new-playbook",
        desk="portfolio-accounting",
        scope={
            "playbook": NEW_PLAYBOOK,
            "code": "SOURCE_UNAVAILABLE",
            "when": curation.FEED | {"custodian": "B"},
        },
    )
    proposal = feed_change()
    placed = {"playbook": NEW_PLAYBOOK, "version": None, "rests_on": ["memo-a-new-playbook"]}
    proposal["changes"][1] |= placed
    layer = scripted(make_layer, proposal, **options)
    feed_only(layer)
    to_desk = held_out_cases.FEED_B
    moved = copy.deepcopy(held_out_cases.FEED_ONLY)
    for case in moved:
        notes: Any = case[5]
        for note in notes:
            if note == to_desk:
                note["playbook"] = NEW_PLAYBOOK
    held_out_cases.write(layer.settings.cases, moved)
    assert knowledge.intake(layer, memo).status_code == 200
    return (layer, *passed(layer))
