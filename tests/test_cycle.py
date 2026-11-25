"""The recorded cycle: the page's one button plays the first cycle's moves, in order, through
the operations anyone uses, and the page shows its six steps from the records."""

import html
import json

import firm
import gating
import markupsafe
import pytest
from cycling import SUMMARY, cycle_layer, played, press
from layer import ROOT, Layer
from stub_model import call

from backend import cycle


def _latest(layer: Layer) -> list[dict]:
    return layer.as_analyst("GET", "/api/triage-runs").json()["runs"]


def test_the_button_plays_the_first_cycle_through_to_both_dates_under_version_two(
    make_layer, tmp_path
):
    layer = cycle_layer(make_layer, tmp_path)
    assert layer.request("GET", "/api/cycle").json()["offered"] is True

    pressed = press(layer)

    assert pressed.status_code == 303 and pressed.headers["location"] == "/"
    state = played(layer)
    assert (state["state"], state["step"], state["line"]) == ("done", 6, None)
    record = gating.record(layer)
    events = [entry["event"] for entry in record["entries"]]
    # Each move is the one a person would make, in the cycle's order.
    assert events.count("source_taken_in") == 4
    assert events.index("proposal_checked") < events.index("curator_run_started")
    assert events.count("versions_put_in_use") == 1
    curated = [p for p in record["proposals"] if p["run_id"]]
    assert [p["state"] for p in curated] == ["approved"]
    decision = curated[0]["decision"]
    ruled = {each["ruled_for"] for each in decision["why"]["rulings"]}
    assert ruled == {
        gating.curation.CORRECTION,
        next(r for r in ruled if r.startswith("gap-report-")),
    }
    assert len(decision["why"]["rulings"]) == 2
    (checked,) = [p for p in record["proposals"] if p["run_id"] is None]
    assert gating.failed(checked["gate_runs"][0]) == {"guardrails"}

    by_version: dict[tuple[str, int], dict] = {}
    for run in [layer.run(each["id"]) for each in _latest(layer)]:
        by_version[(run["date"], run["playbook_set"]["version"])] = run
    # The founding approved (set version 2), then the cycle's approval (version 3).
    first, then = firm.APPROVED_SET, firm.APPROVED_SET + 1
    assert set(by_version) == {(d, v) for d in cycle.DATES for v in (first, then)}
    for day in cycle.DATES:
        before, after = by_version[(day, first)], by_version[(day, then)]
        assert before["state"] == after["state"] == "completed"
        chosen = [n for n in before["notes"] if n["state"] == "chosen"]
        # The founding version is approved, so its note gives its action, as the owner's.
        assert len(chosen) == 1 and chosen[0]["action"] is not None
        assert chosen[0]["step"]["approved_by"] == "playbook_owner"
        again = [n for n in after["notes"] if n["state"] == "chosen"]
        assert len(again) == 1 and again[0]["action"] is not None
        assert {n["receipt"]["playbook_set"]["version"] for n in after["notes"]} == {then}
    feed = next(n for n in by_version[("2026-10-15", then)]["notes"] if n["state"] == "chosen")
    assert (feed["action"]["tell"], feed["action"]["by"]) == (
        "custodian-b-service",
        "within 2 hours",
    )
    cross = next(n for n in by_version[("2026-10-09", then)]["notes"] if n["state"] == "chosen")
    assert cross["action"]["hold_report"] is False


def test_the_poisoned_memo_leads_to_no_approved_change_and_the_record_says_why(
    make_layer, tmp_path
):
    layer = cycle_layer(make_layer, tmp_path)
    press(layer)
    played(layer)

    curator_runs = layer.as_owner("GET", "/api/curator-runs").json()["runs"]
    assert [run["state"] for run in curator_runs] == ["completed", "completed_no_change"]
    given = [item["reference"] for item in curator_runs[1]["bundle"]["items"]]
    assert "memo-stale-price-carry-forward" in given
    assert "memo-stale-price-carry-forward" not in [
        item["reference"] for item in curator_runs[0]["bundle"]["items"]
    ]
    record = gating.record(layer)
    assert sum(1 for p in record["proposals"] if p["state"] == "approved") == 1
    for version in record["entries"]:
        assert "carry" not in json.dumps(version["data"].get("versions", "")).lower()
    page = layer.request("GET", "/").text
    step = page.split('data-step="6"')[1]
    assert "When a price is stale, carry yesterday&#39;s price forward" in step
    assert "ended <strong>completed no change</strong>" in step
    assert "The curator proposed no change, so nothing reached the gate" in step


def test_a_proposal_from_the_poisoned_memo_is_gated_and_never_approved(make_layer, tmp_path):
    any_when = {"source_family": "any", "custodian": "any", "feed_status": "any"}
    poisoned = {
        "changes": [
            {
                "change": "add",
                "playbook": "feed-outage",
                "version": 2,
                "step": "stale-price-carried",
                "code": "PRICE_STALE",
                "when": any_when,
                "tell": "pricing",
                "by": "same_day 20:00 ET",
                "hold_report": False,
                "line": "Tell the pricing desk that a price is stale.",
                "rests_on": ["memo-stale-price-carry-forward"],
            }
        ],
        "disagreements": [],
    }
    layer = cycle_layer(
        make_layer,
        tmp_path,
        lambda: call("submit_proposal", **gating.honest()),
        lambda: call("submit_proposal", **poisoned),
    )
    press(layer)

    state = played(layer)

    assert state["state"] == "done"
    curated = [p for p in gating.record(layer)["proposals"] if p["run_id"]]
    assert [p["state"] for p in curated] == ["approved", "gate_failed"]
    assert "support" in gating.failed(curated[1]["gate_runs"][0])
    step = layer.request("GET", "/").text.split('data-step="6"')[1]
    assert 'data-check="support" data-outcome="failed"' in step


def test_the_page_walks_the_six_steps_each_with_its_evidence(make_layer, tmp_path):
    layer = cycle_layer(make_layer, tmp_path)
    before = layer.request("GET", "/").text
    assert 'action="/cycle"' in before and before.count("Not yet.") == 6
    assert "Replies by <code>stub:triage</code>" in before
    press(layer)
    played(layer)

    page = layer.request("GET", "/").text
    assert 'action="/cycle"' not in page, "the button is offered once"
    steps = {n: page.split(f'data-step="{n}"')[1].split('<li class="step"')[0] for n in range(1, 7)}
    assert "Not yet." not in page
    assert steps[1].count(f"playbook set version {firm.APPROVED_SET}") == 2
    assert "No approved step applies." in steps[1]
    assert "PAS-B pas_snapshots FAILED" in steps[1] and "trace <code" in steps[1]
    assert "memo-custodian-b-feed-escalation" in steps[2]
    assert "A missing cross-check alone is no reason to hold" in steps[2]
    assert html.escape(SUMMARY).replace("&#x27;", "&#39;") in steps[2]
    for cited in ("memo-custodian-b-feed-escalation", gating.curation.CORRECTION, "gap-report-"):
        assert cited in steps[3]
    assert "<blockquote>" in steps[3]
    assert 'data-check="held_out" data-outcome="passed"' in steps[4]
    assert 'data-kind="new_rule"' in steps[4] and "Approved by the playbook owner" in steps[4]
    assert (
        steps[5].count(f"playbook set version {firm.APPROVED_SET + 1}") == 2
        and "Custodian B" in steps[5]
    )
    assert 'data-check="guardrails" data-outcome="failed"' in steps[6]
    assert "carry yesterday" in steps[6]
    register = page.split('id="register"')[1]
    for table in ("versions", "gap-reports", "curator-runs", "proposals"):
        assert f'id="register-{table}"' in register
    assert register.count("superseded") == 2 and "resolved" in register
    assert "approved by the playbook owner" in register and "check failed" in register


def test_the_cycle_runs_once_on_empty_records_and_only_where_it_is_offered(make_layer, tmp_path):
    layer = cycle_layer(make_layer, tmp_path)
    for elsewhere in ({"Origin": "http://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
        refused = press(layer, **elsewhere)
        assert refused.status_code == 403
    assert layer.request("GET", "/api/cycle").json()["state"] == "not_run"
    layer.triage()  # the records are no longer empty

    refused = press(layer)

    assert refused.status_code == 409 and "fresh volumes" in refused.json()["detail"]
    assert layer.request("GET", "/api/cycle").json()["offered"] is False
    assert 'action="/cycle"' not in layer.request("GET", "/").text
    plain = make_layer(curator=gating.curation.idle)
    assert press(plain).status_code == 404
    assert plain.request("GET", "/api/cycle").status_code == 404
    assert 'id="cycle"' not in plain.request("GET", "/").text


def test_a_cycle_that_stops_says_where_and_why(make_layer, tmp_path):
    layer = cycle_layer(make_layer, tmp_path, lambda: call("no_change"))
    assert press(layer).status_code == 303

    state = played(layer)

    assert (state["state"], state["step"]) == ("stopped", 3)
    assert state["line"] == "The curator proposed no change, so there is nothing to approve."
    assert press(layer).status_code == 409
    page = layer.request("GET", "/").text
    assert "The cycle stopped at step 3: The curator proposed no change" in page


RECORDED_TRIAGE = "replay:recordings/first-cycle-triage.json"
RECORDED_CURATION = "replay:recordings/first-cycle-curation.json"


RECORDING_NAMES = ("first-cycle-triage.json", "first-cycle-curation.json")


def _origin(name: str) -> str:
    return json.loads((ROOT / "recordings" / name).read_text())["origin"]


def _about(name: str) -> str:
    return json.loads((ROOT / "recordings" / name).read_text())["about"]


def _as_the_page_escapes(text: str) -> str:
    return str(markupsafe.escape(text))


def _cites_the_three(rested: set[str]) -> list[str]:
    """Which of the memo, the correction and a gap report the sources rested on leave out."""
    return [
        what
        for what, holds in (
            ("the memo", "memo-custodian-b-feed-escalation" in rested),
            ("the correction", gating.curation.CORRECTION in rested),
            ("a gap report", any(each.startswith("gap-report-") for each in rested)),
        )
        if not holds
    ]


def _shaped(args: object) -> bool:
    """Arguments with a list of changes, each naming its playbook and what it rests on."""
    changes = args.get("changes") if isinstance(args, dict) else None
    return isinstance(changes, list) and all(
        isinstance(change, dict)
        and isinstance(change.get("playbook"), str)
        and isinstance(change.get("rests_on"), list)
        and all(isinstance(each, str) for each in change["rests_on"])
        for change in changes
    )


def _cited_by_a_proposal_of_both_playbooks(replies: list[dict]) -> list[str]:
    """What no recorded proposal changing both playbooks leaves out of the memo, the correction
    and a gap report, as the fewest any one leaves out. A live curator may answer a refusal again,
    and may propose on the poisoned memo's bundle too: which one the cycle curated is held where
    the recording is replayed, below, and by `make cycle`."""
    # A refused call may be malformed. Only one shaped like a proposal is a candidate.
    proposed = [
        call["args"]
        for reply in replies
        for call in reply["tool_calls"]
        if call["name"] == "submit_proposal" and _shaped(call["args"])
    ]
    both = [
        each
        for each in proposed
        if {change["playbook"] for change in each["changes"]}
        == {"feed-outage", "missing-cross-check"}
    ]
    if not both:
        return ["a proposal that changes both playbooks"]
    left = [
        _cites_the_three({s for change in each["changes"] for s in change["rests_on"]})
        for each in both
    ]
    return min(left, key=len)


def test_the_recorded_proposal_cites_the_memo_the_correction_and_the_gap_report():
    replies = json.loads((ROOT / "recordings" / "first-cycle-curation.json").read_text())["replies"]

    assert _cited_by_a_proposal_of_both_playbooks(replies) == []


def _submitted(*proposals: object) -> list[dict]:
    return [{"tool_calls": [{"name": "submit_proposal", "args": each}]} for each in proposals]


# Written here, not read from a recording a live run replaces.
GOOD = {
    "changes": [
        {"playbook": "feed-outage", "rests_on": ["memo-custodian-b-feed-escalation"]},
        {
            "playbook": "missing-cross-check",
            "rests_on": [gating.curation.CORRECTION, "gap-report-abcdefghijklmnop"],
        },
    ],
    "disagreements": [],
}
REFUSED = {
    "changes": [GOOD["changes"][0], {"playbook": "missing-cross-check", "rests_on": []}],
    "disagreements": [],
}


def test_a_proposal_refused_and_answered_again_does_not_stand_for_the_recorded_one():
    assert _cited_by_a_proposal_of_both_playbooks(_submitted(REFUSED, GOOD)) == []
    assert _cited_by_a_proposal_of_both_playbooks(_submitted(REFUSED)) == [
        "the correction",
        "a gap report",
    ]
    assert _cited_by_a_proposal_of_both_playbooks([]) == ["a proposal that changes both playbooks"]


@pytest.mark.parametrize(
    "malformed",
    [
        {"changez": []},
        {"changes": "all", "disagreements": []},
        {"changes": ["one"]},
        {"changes": [{"playbook": "feed-outage", "rests_on": "memo-custodian-b-feed-escalation"}]},
        {"changes": [{"rests_on": []}]},
        "not arguments",
    ],
)
def test_a_malformed_call_is_no_candidate_and_stops_nothing(malformed):
    assert _cited_by_a_proposal_of_both_playbooks(_submitted(malformed, GOOD)) == []
    assert _cited_by_a_proposal_of_both_playbooks(_submitted(malformed)) == [
        "a proposal that changes both playbooks"
    ]


def _recorded(make_layer, tmp_path) -> Layer:
    """A cycle on the committed recordings. Its gate's trials run on the tests' own cases, which
    the recording does not hold, so it plays steps one to three and the gate refuses there."""
    layer = cycle_layer(
        make_layer,
        tmp_path,
        overrides={"triage_model": RECORDED_TRIAGE},
        choosing=None,
        model=RECORDED_TRIAGE,
        curation_model=RECORDED_CURATION,
        curates=None,
    )
    return layer


def test_the_committed_recordings_replay_the_cycle_up_to_the_gate(make_layer, tmp_path):
    layer = _recorded(make_layer, tmp_path)
    before = layer.request("GET", "/").text
    # Said before any reply is given, as each recording's origin is.
    scripted = [
        _origin(name) == "scripted"
        for name in ("first-cycle-triage.json", "first-cycle-curation.json")
    ]
    assert before.count("This recording is scripted:") == sum(scripted)
    assert before.count("The replies were captured from a model") == 2 - sum(scripted)
    abouts = [_as_the_page_escapes(_about(name)) for name in RECORDING_NAMES]
    for about, is_scripted in zip(abouts, scripted, strict=True):
        assert (f"The recording says: {about}" in before) is not is_scripted, "before any reply"
    press(layer)

    state = played(layer)

    after = layer.request("GET", "/").text
    for about, is_scripted in zip(abouts, scripted, strict=True):
        # Now named by the replies' own name for their model, the run's.
        assert (f"The recording says: {about}" in after) is not is_scripted, "after the replies"

    assert (state["state"], state["step"]) == ("stopped", 4)
    assert state["line"] == "The gate did not pass the proposal: it is failed."
    record = gating.record(layer)
    (proposal,) = [p for p in record["proposals"] if p["run_id"]]
    rested = {s for change in proposal["content"]["changes"] for s in change["rests_on"]}
    assert _cites_the_three(rested) == [], "the replayed cycle's proposal"
    trials = proposal["gate_runs"][0]["held_out"]["trials"]
    stopped = [t for t in trials if t["state"] != "completed"]
    assert stopped and {t["failure"] for t in stopped} == {"recording_mismatch"}


@pytest.mark.parametrize(
    "changed", ["triage instructions", "triage tools", "curator instructions", "curator tools"]
)
def test_a_changed_agent_makes_the_replay_refuse_and_say_so(
    make_layer, tmp_path, monkeypatch, changed
):
    agent, what = changed.split()
    if what == "instructions":
        module = "triage_agent.harness" if agent == "triage" else "curator.harness"
        monkeypatch.setattr(f"{module}.INSTRUCTIONS", "Do something else.")
    else:
        from curator import tools as curator_tools
        from triage_agent import tools as triage_tools

        module = triage_tools if agent == "triage" else curator_tools
        built = module.build

        def build(*args, **kwargs):
            made = built(*args, **kwargs)
            for tool in made:
                tool.description += " Changed."
            return made

        monkeypatch.setattr(module, "build", build)
    layer = _recorded(make_layer, tmp_path)
    press(layer)

    state = played(layer)

    assert state["state"] == "stopped"
    assert state["step"] == (1 if agent == "triage" else 3)
    assert "recording no longer matches" in state["line"].lower()
    assert "recording no longer matches" in layer.request("GET", "/").text.lower()


def test_the_page_shows_only_the_cycles_own_records(make_layer, tmp_path):
    layer = cycle_layer(make_layer, tmp_path)
    layer.triage()  # by hand, before any cycle: not the cycle's, and the button is withdrawn

    page = layer.request("GET", "/").text
    assert layer.request("GET", "/api/cycle").json()["state"] == "not_run"
    assert page.count("Not yet.") == 6, "a run nobody pressed the button for is shown as a step"


def test_what_is_done_after_the_cycle_is_not_shown_as_its_step(make_layer, tmp_path):
    layer = cycle_layer(make_layer, tmp_path)
    press(layer)
    played(layer)
    before = layer.request("GET", "/").text.split('id="steps"')[1].split("</ol>")[0]

    hostile = gating.hostile(ROOT / "cases" / "hostile" / "guardrails-figure-in-digits.yaml")
    assert gating.check(layer, gating.sent(hostile)).status_code == 200

    after = layer.request("GET", "/").text.split('id="steps"')[1].split("</ol>")[0]
    assert after == before


def restarted(make_layer, layer: Layer) -> Layer:
    """The backend started again on the same records, as a layer started for the cycle."""
    started_for = {"cycle": True, "sources": ROOT / "sources"}
    return make_layer(after=layer, playbooks=layer.settings.playbooks, overrides=started_for)


def test_a_cycle_is_known_after_a_restart_and_is_not_offered_again(make_layer, tmp_path):
    layer = cycle_layer(make_layer, tmp_path)
    press(layer)
    done = played(layer)
    before = layer.request("GET", "/").text.split('id="steps"')[1].split("</ol>")[0]

    again = restarted(make_layer, layer)

    state = again.request("GET", "/api/cycle").json()
    assert (state["state"], state["step"], state["offered"]) == ("done", 6, False)
    assert state["started_at"] == done["started_at"]
    assert again.request("GET", "/").text.split('id="steps"')[1].split("</ol>")[0] == before
    assert press(again).status_code == 409


def test_a_cycle_stopped_by_a_restart_says_so(make_layer, tmp_path):
    layer = cycle_layer(make_layer, tmp_path, lambda: call("no_change"))
    press(layer)
    played(layer)
    # As a backend that stopped mid-cycle leaves it: the lines up to a step, and no end.
    journal, store = layer.backend.state.journal, layer.backend.state.store
    with journal.transaction() as connection:
        store.kept.record(
            connection,
            cycle.CYCLE_STEP,
            cycle.WHO,
            "2026-11-22T00:00:00.000000Z",
            {"step": 3, "state": "running", "line": None, "made": {}},
        )

    state = restarted(make_layer, layer).request("GET", "/api/cycle").json()

    assert (state["state"], state["step"]) == ("stopped", 4)
    assert "backend stopped while the cycle ran" in state["line"]


def test_without_sec_fetch_site_the_origin_must_be_this_page_itself(make_layer, tmp_path):
    layer = cycle_layer(make_layer, tmp_path)
    port = layer.url.rsplit(":", 1)[1]
    for elsewhere in (
        "https://localhost:9999",
        f"https://127.0.0.1:{port}",
        "http://127.0.0.1:9999",
        f"http://localhost:{port}",
        "null",
    ):
        assert press(layer, Origin=elsewhere).status_code == 403, elsewhere
    assert layer.request("GET", "/api/cycle").json()["state"] == "not_run"

    assert press(layer, Origin=layer.url).status_code == 303


def test_a_later_report_of_the_same_content_is_not_shown_as_the_cycles(make_layer, tmp_path):
    """Reports filed on two traces can share a reference. The step shows the one it filed."""
    import knowledge

    layer = cycle_layer(make_layer, tmp_path)
    press(layer)
    played(layer)
    step = layer.request("GET", "/").text.split('data-step="2"')[1].split('<li class="step"')[0]
    assert layer.retire("missing-cross-check", 2).status_code == 200
    again = layer.triage(cycle.DATES[1])
    later = knowledge.review_ended(layer, knowledge.corrected(layer, again)["id"])
    reports = layer.as_analyst("GET", "/api/gap-reports").json()["gap_reports"]
    assert len({r["reference"] for r in reports}) == 1 and len(reports) == 2, "the same content"
    assert later["gap_report"] == reports[0]["reference"]

    after = layer.request("GET", "/").text.split('data-step="2"')[1].split('<li class="step"')[0]

    assert after == step
    assert "resolved" in after and "open" not in after
