"""Approval and refusal: one exact proposal, its gate run and the set that run was made against."""

import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import approving
import curation
import firm
import gating
import held_out_cases
import knowledge
import pytest
import stand_ins
from layer import CROSS_CHECK_DAY, DAY
from sqlalchemy import event

from backend.chainlog import Line
from backend.gate import run as gate_run
from backend.playbook_format import content_hash, parse


def kept(layer) -> tuple:
    """All that a refused move must leave as it was."""
    return (
        firm.files(layer.settings.playbooks),
        firm.records(layer.settings.database),
        approving.in_use(layer),
    )


@pytest.fixture
def passed(make_layer):
    return approving.first_cycle_passed(make_layer)


# Approving


def test_approving_writes_the_versions_and_one_line_and_resolves_the_gap_reports(passed):
    layer, proposal, run = passed
    folder = layer.settings.playbooks
    files = gating.approved_files(folder)
    lines = knowledge.ledger(layer)
    (report,) = knowledge.gap_reports(layer)

    done = approving.approved(layer, proposal, run)

    (line,) = knowledge.ledger(layer)[len(lines) :]
    assert (line["event"], line["who"]) == ("versions_put_in_use", "playbook_owner")
    after = gating.approved_files(folder)
    assert {name: after[name] for name in files} == files, "an approved file was edited"
    written = {"feed-outage/2.yaml", "missing-cross-check/2.yaml"}
    assert after.keys() - files.keys() == written
    named = {f"{v['id']}/{v['version']}.yaml": v["sha256"] for v in line["data"]["versions"]}
    assert named == {name: content_hash(after[name]) for name in written}
    assert approving.in_use(layer) == {"feed-outage": 2, "missing-cross-check": 2}
    assert line["data"]["approval"] == {
        "proposal_id": proposal["id"],
        "sha256": proposal["sha256"],
        "gate_run_id": run["id"],
        "rulings": done["rulings"],
        "resolved": [report["id"]],
    }
    assert approving.reports(layer) == {report["id"]: ("resolved", proposal["id"])}
    assert approving.state(layer, proposal["id"]) == "approved"
    assert (done["line"], done["resolved"]) == (line["line"], [report["id"]])
    in_use = layer.as_analyst("GET", "/api/playbooks").json()
    assert done["playbook_set"] == {
        "id": in_use["id"],
        "version": firm.APPROVED_SET + 1,
        "hash": in_use["hash"],
    }


def test_a_new_version_holds_what_the_proposal_says_and_what_it_rests_on(passed):
    layer, proposal, run = passed
    approving.approved(layer, proposal, run)
    folder = layer.settings.playbooks

    feed = parse((folder / "feed-outage" / "2.yaml").read_bytes())
    cross = parse((folder / "missing-cross-check" / "2.yaml").read_bytes())

    assert (feed.version, feed.replaces, cross.version, cross.replaces) == (2, 1, 2, 1)
    held = [("feed-outage", step) for step in feed.steps]
    held += [("missing-cross-check", step) for step in cross.steps]
    for (playbook, step), change in zip(held, gating.HONEST["changes"], strict=True):
        assert (playbook, step.id) == (change["playbook"], change["step"])
        fields = ("code", "when", "tell", "by", "hold_report", "line")
        assert step.model_dump(include=set(fields)) == {name: change[name] for name in fields}
    curated = curation.read(layer, approving.listed(layer, proposal["id"])["run_id"])
    manifest = {each["reference"]: each["sha256"] for each in curated["bundle"]["items"]}
    rests_on = {v["id"]: v["rests_on"] for v in knowledge.ledger(layer)[-1]["data"]["versions"]}
    wanted = {
        "feed-outage": {curation.MEMO, curation.FOUNDING},
        "missing-cross-check": {approving.CORRECTION, approving.GAP_REPORT, approving.FOUNDING},
    }
    for playbook, cited in wanted.items():
        assert rests_on[playbook] == [{"id": r, "sha256": manifest[r]} for r in sorted(cited)]


def test_the_next_triage_receipt_names_the_new_set_and_shows_the_approved_action(
    make_layer, tmp_path
):
    # Nobody approved version 1, so it gives no action.
    unapproved = firm.unapproved(firm.copy(tmp_path / "unapproved"))
    layer, proposal, run = approving.first_cycle_passed(make_layer, playbooks=unapproved)
    before = approving.triaged(layer)
    assert all(note["action"] is None for note in before["notes"])

    done = approving.approved(layer, proposal, run)

    feed, cross = approving.triaged(layer), approving.triaged(layer, CROSS_CHECK_DAY)
    for triage in (feed, cross):
        for note in triage["notes"]:
            assert note["receipt"]["playbook_set"] == done["playbook_set"]
    assert before["notes"][0]["receipt"]["playbook_set"]["version"] == 1
    (told,) = [note for note in feed["notes"] if note["state"] == "chosen"]
    assert (told["step"]["version"], told["step"]["approved_by"]) == (2, "playbook_owner")
    assert (told["action"]["tell"], told["action"]["by"]) == (
        "custodian-b-service",
        "within 2 hours",
    )
    (held,) = [note for note in cross["notes"] if note["state"] == "chosen"]
    assert held["action"]["hold_report"] is False


def test_the_set_in_use_after_approval_is_the_one_the_gate_tested(passed):
    layer, proposal, run = passed
    approving.approved(layer, proposal, run)

    work = layer.as_agent(
        "GET", f"/api/agent/triage-runs/{layer.start(DAY)['id']}/attempts/1"
    ).json()

    # The options, in the order the trials were given them.
    assert [option["step"] for option in work["notes"][0]["options"]] == [
        "portfolio-feed-failed",
        "custodian-b-portfolio-feed-failed",
    ]


def test_the_same_approval_again_returns_the_first_and_changes_nothing(passed):
    layer, proposal, run = passed
    first = approving.approved(layer, proposal, run)
    before = kept(layer)

    again = approving.approve(layer, proposal, run)
    other = approving.approve(
        layer, proposal, run, approving.rulings(layer, proposal, approving.FOUNDING)
    )
    mistyped = approving.approve(layer, proposal, run, gate_run="0" * 32)

    assert again.status_code == 200 and again.json() == first
    assert other.status_code == mistyped.status_code == 409
    assert other.json() == {"detail": "The proposal is approved already, so nothing changes."}
    assert kept(layer) == before


def test_the_same_approval_twice_at_once_makes_one_approval(passed):
    layer, proposal, run = passed
    lines = len(knowledge.ledger(layer))

    with ThreadPoolExecutor(2) as pool:
        answers = list(pool.map(lambda _: approving.approve(layer, proposal, run), range(2)))

    assert [answer.status_code for answer in answers] == [200, 200]
    assert answers[0].json() == answers[1].json()
    assert len(knowledge.ledger(layer)) == lines + 1


def test_an_approval_and_a_refusal_at_once_make_one_decision(passed):
    layer, proposal, run = passed
    lines = len(knowledge.ledger(layer))

    with ThreadPoolExecutor(2) as pool:
        approval = pool.submit(approving.approve, layer, proposal, run)
        refusal = pool.submit(approving.refuse, layer, proposal["id"])
        statuses = [approval.result().status_code, refusal.result().status_code]

    assert sorted(statuses) == [200, 409]
    assert len(knowledge.ledger(layer)) == lines + 1


def test_an_approval_whose_commit_failed_is_complete_before_anything_else(passed):
    layer, proposal, run = passed
    engine = layer.backend.state.journal._engine
    failed = []

    def fail_once(_connection) -> None:
        if not failed:
            failed.append(True)
            raise RuntimeError("the commit failed")

    ruled = approving.rulings(layer, proposal)
    event.listen(engine, "commit", fail_once)
    response = approving.approve(layer, proposal, run, ruled)
    event.remove(engine, "commit", fail_once)

    assert response.status_code == 500
    assert approving.state(layer, proposal["id"]) == "approved"
    assert {state for state, _ in approving.reports(layer).values()} == {"resolved"}
    # Sent again, as after a lost response, it is the same approval.
    assert approving.approve(layer, proposal, run).status_code == 200
    reordered = [
        each | {"between": each["between"][::-1]}
        for each in reversed(approving.rulings(layer, proposal))
    ]
    assert approving.approve(layer, proposal, run, reordered).status_code == 200
    assert knowledge.events(layer).count("versions_put_in_use") == 1


def test_a_proposal_of_a_new_playbook_puts_its_first_version_in_use(make_layer):
    layer, proposal, run = approving.new_playbook(make_layer)

    approving.approved(layer, proposal, run, ruled=[])

    assert approving.in_use(layer) == {
        approving.NEW_PLAYBOOK: 1,
        "feed-outage": 2,
        "missing-cross-check": 1,
    }
    made = parse((layer.settings.playbooks / approving.NEW_PLAYBOOK / "1.yaml").read_bytes())
    assert made.replaces is None
    assert [step.id for step in made.steps] == ["custodian-b-feed-failed"]


def removing() -> dict:
    proposal = curation.proposed(approving.GAP_REPORT)
    proposal["disagreements"][0]["between"].append(approving.GAP_REPORT)
    return proposal


def removed_passed(make_layer) -> tuple:
    layer = approving.scripted(make_layer, removing())
    cases = held_out_cases.FIRST_CYCLE
    for name in ("made-custody-b-partial", "made-custody-a-failed"):
        cases = held_out_cases.changed(cases, name, [held_out_cases.NO_STEP])
    held_out_cases.write(layer.settings.cases, cases)
    gating.first_cycle(layer)
    return (layer, *approving.passed(layer))


def test_removing_a_playbooks_last_step_leaves_a_version_by_which_no_step_applies(make_layer):
    layer, proposal, run = removed_passed(make_layer)

    approving.approved(layer, proposal, run)

    emptied = parse((layer.settings.playbooks / "missing-cross-check" / "2.yaml").read_bytes())
    assert (emptied.replaces, emptied.steps) == (1, [])
    notes = approving.triaged(layer, CROSS_CHECK_DAY)["notes"]
    assert {note["statement"] for note in notes} == {"No approved step applies."}
    # And rolling it back puts the step in use again.
    assert layer.retire("missing-cross-check", 2).status_code == 200
    again = approving.triaged(layer, CROSS_CHECK_DAY)["notes"]
    assert [note["state"] for note in again].count("chosen") == 1


def test_an_approved_version_with_no_step_is_shown_approved_in_the_register(make_layer):
    layer, proposal, run = removed_passed(make_layer)
    approving.approved(layer, proposal, run)
    row = "<tr><td>missing-cross-check</td><td>2</td><td>in use</td><td>by the playbook owner</td>"

    assert row in layer.request("GET", "/").text
    # Made again from the logs, the register says the same.
    assert layer.as_owner("POST", "/api/rebuild").status_code == 200
    assert row in layer.request("GET", "/").text


def test_a_version_number_is_one_above_every_one_the_playbook_had(make_layer):
    layer = approving.scripted(make_layer, approving.feed_change())
    layer.put_in_use({"feed-outage": firm.steps("feed-outage")})
    assert layer.retire("feed-outage", 2).status_code == 200
    approving.feed_only(layer)
    proposal, run = approving.passed(layer)

    done = approving.approved(layer, proposal, run, ruled=[])

    assert [(v["id"], v["version"]) for v in done["versions"]] == [("feed-outage", 3)]
    assert parse((layer.settings.playbooks / "feed-outage" / "3.yaml").read_bytes()).replaces == 1


# The ruling


def test_a_disagreement_needs_a_ruling_and_the_ledger_line_keeps_it(passed):
    layer, proposal, run = passed
    before = kept(layer)
    both = approving.rulings(layer, proposal)

    none = approving.approve(layer, proposal, run, [])
    left_out = layer.as_owner(
        "POST",
        f"/api/proposals/{proposal['id']}/approve",
        json={"sha256": proposal["sha256"], "gate_run": run["id"]},
    )
    one_of_two = approving.approve(layer, proposal, run, both[:1])

    for refused in (none, left_out, one_of_two):
        assert refused.status_code == 422
        assert "has no ruling: name the source you rule for." in refused.json()["detail"]
    assert kept(layer) == before
    assert approving.state(layer, proposal["id"]) == "gate_passed"

    approving.approved(layer, proposal, run, both)

    assert knowledge.ledger(layer)[-1]["data"]["approval"]["rulings"] == [
        {
            "change": 3,
            "between": sorted([approving.CORRECTION, approving.FOUNDING]),
            "ruled_for": approving.CORRECTION,
        },
        {
            "change": 3,
            "between": sorted([approving.GAP_REPORT, approving.FOUNDING]),
            "ruled_for": approving.GAP_REPORT,
        },
    ]


def for_the_memo(both: list[dict]) -> list[dict]:
    return [each | {"ruled_for": approving.FOUNDING} for each in both]


def for_the_memo_on_one(both: list[dict]) -> list[dict]:
    return [both[0], *for_the_memo(both[1:])]


NOT_SETTLED = {
    "a source in none": (
        lambda both: [both[0] | {"ruled_for": "memo-never-taken-in"}, both[1]],
        "names no disagreement",
    ),
    "a source of the other disagreement": (
        lambda both: [both[0] | {"ruled_for": both[1]["ruled_for"]}, both[1]],
        "names no disagreement",
    ),
    "a change that is not disputed": (
        lambda both: [both[0] | {"change": 1}, both[1]],
        "names no disagreement",
    ),
    "no change": (lambda both: [both[0] | {"change": None}, both[1]], "names no disagreement"),
    "the group the curator listed": (
        lambda both: [
            both[0] | {"between": [approving.CORRECTION, approving.GAP_REPORT, approving.FOUNDING]}
        ],
        "names no disagreement",
    ),
    "both sides": (lambda both: [*both, *for_the_memo(both)], "more than one ruling"),
    "for the memo": (for_the_memo, "which you rule against for a memo: refuse the proposal"),
    "for the memo on one of two": (
        for_the_memo_on_one,
        "which you rule against for a memo: refuse the proposal",
    ),
}


@pytest.mark.parametrize("named", NOT_SETTLED)
def test_a_ruling_that_settles_nothing_is_refused_and_changes_nothing(passed, named):
    layer, proposal, run = passed
    made, said = NOT_SETTLED[named]
    sent = made(approving.rulings(layer, proposal))
    before = kept(layer)

    refused = approving.approve(layer, proposal, run, sent)

    assert refused.status_code == 422 and said in refused.json()["detail"]
    assert "memo-never-taken-in" not in refused.text
    assert kept(layer) == before


def test_a_ruling_for_the_memo_is_no_approval_of_what_overrides_it(make_layer):
    layer, proposal, run = removed_passed(make_layer)
    before = kept(layer)

    # The removal rests on the correction and the gap report, and not on the founding memo.
    ruled = approving.rulings(layer, proposal, approving.FOUNDING)
    refused = approving.approve(layer, proposal, run, ruled)

    assert refused.status_code == 422
    assert "which you rule against for a memo" in refused.json()["detail"]
    assert kept(layer) == before


def test_a_proposal_with_no_disagreement_needs_no_ruling_and_takes_none(make_layer):
    layer = approving.scripted(make_layer, approving.feed_change())
    approving.feed_only(layer)
    proposal, run = approving.passed(layer)

    named = {"change": 1, "between": [curation.MEMO, curation.FOUNDING]}
    ruled = approving.approve(layer, proposal, run, [named | {"ruled_for": curation.MEMO}])

    assert ruled.status_code == 422
    assert approving.approved(layer, proposal, run, ruled=[])["rulings"] == []


def test_a_disagreement_only_the_proposal_lists_needs_a_ruling_too(make_layer):
    proposed = approving.feed_change()
    between = [curation.MEMO, curation.FOUNDING]
    proposed["disagreements"] = [{"between": between, "about": "Who is told for custodian B."}]
    layer = approving.scripted(make_layer, proposed)
    approving.feed_only(layer)
    proposal, run = approving.passed(layer)
    assert run["disagreements"] == []

    assert approving.approve(layer, proposal, run, []).status_code == 422
    # Both changes rest on some of it, so each has a ruling of its own.
    sent = approving.rulings(layer, proposal, curation.MEMO)
    assert [each["change"] for each in sent] == [1, 2]
    ruled = approving.approved(layer, proposal, run, sent)["rulings"]
    assert ruled == [
        {"change": change, "between": sorted(between), "ruled_for": curation.MEMO}
        for change in (1, 2)
    ]


# Exactly what was checked


@pytest.mark.parametrize(
    "named",
    [
        {"sha256": "0" * 64},
        {"gate_run": "0" * 32},
        {"sha256": "f" * 64, "gate_run": "f" * 32},
        {"sha256": "0" * 64, "rulings": []},
    ],
)
@pytest.mark.parametrize("set_changed", [False, True], ids=["set as gated", "set changed"])
def test_a_mistyped_hash_or_gate_run_changes_nothing(passed, named, set_changed):
    layer, proposal, run = passed
    if set_changed:
        assert layer.retire("feed-outage", 1).status_code == 200
    before = kept(layer)

    refused = approving.approve(layer, proposal, run, **named)

    assert refused.status_code == 409
    assert "nothing changes. It is still gate_passed." in refused.json()["detail"]
    assert kept(layer) == before
    assert approving.state(layer, proposal["id"]) == "gate_passed"
    assert {state for state, _ in approving.reports(layer).values()} == {"cited"}


def test_one_character_off_the_hash_is_another_proposals(passed):
    layer, proposal, run = passed
    last = "0" if proposal["sha256"][-1] != "0" else "1"

    refused = approving.approve(layer, proposal, run, sha256=proposal["sha256"][:-1] + last)

    assert refused.status_code == 409
    assert approving.state(layer, proposal["id"]) == "gate_passed"


@pytest.fixture
def two_passed(make_layer):
    """Two proposals through the gate against one set, each citing a gap report of its own."""
    second: dict = {}
    layer = approving.scripted(make_layer, gating.honest(), lambda: second)
    gating.first_cycle(layer)
    first = approving.passed(layer)
    report = approving.another_report(layer)
    second.update(approving.resting_on(report["reference"]))
    return layer, first, approving.passed(layer), report["id"]


def test_the_second_of_two_passed_proposals_goes_stale(two_passed):
    layer, (first, first_run), (second, second_run), report = two_passed
    assert first_run["playbook_set"] == second_run["playbook_set"]
    assert approving.reports(layer)[report] == ("cited", second["id"])
    approving.approved(layer, first, first_run)
    files = gating.approved_files(layer.settings.playbooks)
    lines = len(knowledge.ledger(layer))

    refused = approving.approve(layer, second, second_run)

    assert refused.status_code == 409
    assert "The gate must run again, on a new proposal." in refused.json()["detail"]
    assert approving.state(layer, second["id"]) == "stale"
    assert approving.reports(layer)[report] == ("open", None)
    (line,) = knowledge.ledger(layer)[lines:]
    assert (line["event"], line["data"]["reopened"]) == ("proposal_went_stale", [report])
    assert gating.approved_files(layer.settings.playbooks) == files
    # The first one's report stays resolved.
    assert [s for s, _ in approving.reports(layer).values()].count("resolved") == 1

    # Sent again, and refused again: the same answer, and no second line.
    again = approving.approve(layer, second, second_run)
    assert (again.status_code, again.json()) == (409, refused.json())
    assert approving.refuse(layer, second["id"]).status_code == 409
    assert gating.gate(layer, second["id"]).status_code == 409
    assert len(knowledge.ledger(layer)) == lines + 1


def test_two_passed_proposals_approved_at_once_leave_one_stale(two_passed):
    layer, first, second, _ = two_passed

    with ThreadPoolExecutor(2) as pool:
        answers = list(pool.map(lambda each: approving.approve(layer, *each), (first, second)))

    assert sorted(answer.status_code for answer in answers) == [200, 409]
    states = {approving.state(layer, proposal["id"]) for proposal, _ in (first, second)}
    assert states == {"approved", "stale"}
    assert knowledge.events(layer).count("versions_put_in_use") == 1


def changed_and_back(layer) -> None:
    """The same versions in use as at the gate, and a later set."""
    layer.put_in_use({"missing-cross-check": firm.steps("missing-cross-check")})
    assert layer.retire("missing-cross-check", 2).status_code == 200


CHANGES = {
    "a retirement": lambda layer: layer.retire("feed-outage", 1),
    "another playbook's new version": lambda layer: layer.put_in_use(
        {"missing-cross-check": firm.steps("missing-cross-check", by="within 8 hours")}
    ),
    "a change undone": changed_and_back,
}


@pytest.mark.parametrize("change", CHANGES)
@pytest.mark.parametrize("ruled", [None, []], ids=["ruled", "no ruling"])
def test_any_change_to_the_set_after_the_gate_makes_the_approval_stale(passed, change, ruled):
    layer, proposal, run = passed
    CHANGES[change](layer)
    files = gating.approved_files(layer.settings.playbooks)

    refused = approving.approve(layer, proposal, run, ruled)

    assert refused.status_code == 409 and "stale" in refused.json()["detail"]
    assert approving.state(layer, proposal["id"]) == "stale"
    assert {state for state, _ in approving.reports(layer).values()} == {"open"}
    assert gating.approved_files(layer.settings.playbooks) == files


# Refusing


@pytest.mark.parametrize("when", ["drafted", "gate_failed", "gate_passed"])
def test_refusing_writes_one_line_with_the_reason_and_opens_what_it_still_cited(make_layer, when):
    proposed = gating.honest()
    if when == "gate_failed":
        proposed["disagreements"] = []
    layer = approving.scripted(make_layer, proposed)
    gating.first_cycle(layer)
    proposal = approving.drafted(layer)
    if when != "drafted":
        gating.gated(layer, proposal["id"])
    assert approving.state(layer, proposal["id"]) == when
    (report,) = approving.reports(layer)
    files = gating.approved_files(layer.settings.playbooks)
    lines = len(knowledge.ledger(layer))

    done = approving.refuse(layer, proposal["id"], "against_the_sources")

    assert done.status_code == 200, done.text
    (line,) = knowledge.ledger(layer)[lines:]
    assert (line["event"], line["who"]) == ("proposal_refused", "playbook_owner")
    assert line["data"] == {
        "proposal_id": proposal["id"],
        "sha256": proposal["sha256"],
        "reason": "against_the_sources",
        # A failed gate opened it already.
        "reopened": [] if when == "gate_failed" else [report],
    }
    assert done.json()["reason"] == "against_the_sources"
    assert approving.state(layer, proposal["id"]) == "refused"
    assert approving.reports(layer) == {report: ("open", None)}
    assert gating.approved_files(layer.settings.playbooks) == files


def test_refusing_one_that_failed_leaves_anothers_citation_untouched(make_layer):
    layer = approving.scripted(make_layer, gating.honest() | {"disagreements": []}, gating.honest())
    gating.first_cycle(layer)
    failing = approving.drafted(layer)
    assert gating.gated(layer, failing["id"])["outcome"] == "failed"
    second = approving.drafted(layer)
    before = approving.reports(layer)
    assert list(before.values()) == [("cited", second["id"])]

    assert approving.refuse(layer, failing["id"]).status_code == 200

    assert approving.reports(layer) == before
    assert approving.state(layer, second["id"]) == "drafted"


def test_the_same_refusal_again_returns_the_first_and_another_is_refused(passed):
    layer, proposal, run = passed
    first = approving.refuse(layer, proposal["id"], "not_needed")
    before = kept(layer)

    again = approving.refuse(layer, proposal["id"], "not_needed")
    other = approving.refuse(layer, proposal["id"], "wrong_change")
    approving_it = approving.approve(layer, proposal, run)

    assert again.status_code == 200 and again.json() == first.json()
    assert other.status_code == approving_it.status_code == 409
    for refused in (other, approving_it):
        assert "The proposal is refused" in refused.json()["detail"]
    assert kept(layer) == before


def test_an_approved_proposal_cannot_be_refused(passed):
    layer, proposal, run = passed
    approving.approved(layer, proposal, run)
    before = kept(layer)

    refused = approving.refuse(layer, proposal["id"])

    assert refused.status_code == 409 and "is approved" in refused.json()["detail"]
    assert kept(layer) == before


def test_a_checked_proposal_is_neither_refused_nor_read_for_approval(make_layer):
    layer = make_layer(agent=gating.trial_agent, curator=curation.idle)
    gating.first_cycle(layer)
    checked = gating.checked(layer, gating.honest())

    refused = approving.refuse(layer, checked["id"])
    read = layer.as_owner("GET", f"/api/proposals/{checked['id']}")

    assert refused.status_code == read.status_code == 409
    assert "can never be approved" in refused.json()["detail"]


def test_no_proposal_is_404(make_layer):
    layer = make_layer(agent=gating.trial_agent, curator=curation.idle)

    assert approving.refuse(layer, "0" * 32).status_code == 404
    assert layer.as_owner("GET", f"/api/proposals/{'0' * 32}").status_code == 404


def test_a_proposal_is_not_refused_while_its_gate_runs(make_layer):
    asked, go_on = threading.Event(), threading.Event()

    def waits(*_) -> None:
        asked.set()
        assert go_on.wait(20)

    layer = make_layer(
        agent=lambda url, key: stand_ins.trial_agent(url, key, before=waits),
        curator=curation.idle,
    )
    approving.feed_only(layer)
    run_id = curation.started(layer)
    proposal = curation.propose(layer, run_id, approving.feed_change()).json()

    with ThreadPoolExecutor(1) as pool:
        gate = pool.submit(gating.gate, layer, proposal["id"])
        assert asked.wait(20)
        during = approving.refuse(layer, proposal["id"])
        go_on.set()
        ended = gate.result(timeout=60)

    assert during.status_code == 409
    assert during.json() == {
        "detail": "The proposal is drafted and the gate is running on it: "
        "refuse it once the gate has ended."
    }
    assert ended.json()["outcome"] == "passed"
    assert approving.refuse(layer, proposal["id"]).status_code == 200


# What no refusal changes


def failed_gate(layer, proposal, run) -> None:
    assert gating.gated(layer, proposal["id"])["outcome"] == "failed"


def mistyped(layer, proposal, run) -> None:
    assert approving.approve(layer, proposal, run, sha256="0" * 64).status_code == 409


def unruled(layer, proposal, run) -> None:
    assert approving.approve(layer, proposal, run, []).status_code == 422


def by_the_analyst(layer, proposal, run) -> None:
    assert approving.approve(layer, proposal, run, send=layer.as_analyst).status_code == 403


def refused(layer, proposal, run) -> None:
    assert approving.refuse(layer, proposal["id"]).status_code == 200


def stale(layer, proposal, run) -> None:
    assert approving.approve(layer, proposal, run).status_code == 409
    assert approving.state(layer, proposal["id"]) == "stale"


@pytest.mark.parametrize("what", [failed_gate, mistyped, unruled, by_the_analyst, refused, stale])
def test_a_failed_gate_or_a_refused_approval_leaves_every_approved_files_hash(make_layer, what):
    proposed = copy.deepcopy(gating.HONEST)
    if what is failed_gate:
        proposed["disagreements"] = []
    layer = approving.scripted(make_layer, proposed)
    gating.first_cycle(layer)
    proposal, run = approving.drafted(layer), {}
    if what is not failed_gate:
        run = gating.gated(layer, proposal["id"])
    if what is stale:
        assert layer.retire("feed-outage", 1).status_code == 200
    folder = layer.settings.playbooks
    hashes = {n: content_hash(b) for n, b in gating.approved_files(folder).items()}
    ledger = (folder / "ledger.jsonl").read_bytes()
    in_use = approving.in_use(layer)

    what(layer, proposal, run)

    assert {n: content_hash(b) for n, b in gating.approved_files(folder).items()} == hashes
    assert approving.in_use(layer) == in_use
    grown = (folder / "ledger.jsonl").read_bytes()
    assert grown.startswith(ledger), "the ledger is only added to"
    for line in grown[len(ledger) :].splitlines():
        assert Line.model_validate_json(line).event != "versions_put_in_use"


# What a version's file can hold


LINES = (
    "yes",
    "null",
    "- Ask the desk: now # and wait",
    "'Ask' the desk for \"the file\"",
    "Ask the desk again.",
    "Ask the café’s desk – again.",
    "  Ask the desk again.  ",
)


@pytest.mark.parametrize("name", ["null", "true", "no", "off"])
@pytest.mark.parametrize("line", LINES)
def test_a_version_reads_back_as_the_step_it_was_given(tmp_path, name, line):
    folder = firm.copy(tmp_path / "playbooks")
    given = firm.steps("feed-outage", id=name, line=line, code="PRICE_STALE")

    with firm.opened(folder, tmp_path) as (journal, store):
        firm.put_in_use(journal, store, {name: given})

    made = parse((folder / name / "1.yaml").read_bytes())
    assert (made.id, made.steps) == (name, given)


def test_across_playbooks_triage_is_asked_in_the_order_the_trials_were(make_layer):
    asked: set[tuple] = set()

    def keep(_http, _path, note) -> None:
        if note["code"] == "SOURCE_UNAVAILABLE":
            asked.add(tuple(f"{each['playbook']}/{each['step']}" for each in note["options"]))

    class Layers:
        def __call__(self, **options):
            agent = options | {
                "agent": lambda url, key: stand_ins.trial_agent(url, key, before=keep)
            }
            return make_layer(**agent)

    layer, proposal, run = approving.new_playbook(Layers())
    approving.approved(layer, proposal, run, ruled=[])

    work = layer.as_agent(
        "GET", f"/api/agent/triage-runs/{layer.start(DAY)['id']}/attempts/1"
    ).json()

    in_use = tuple(f"{each['playbook']}/{each['step']}" for each in work["notes"][0]["options"])
    assert asked == {in_use}
    assert in_use == (
        "custodian-b-feed/custodian-b-feed-failed",
        "feed-outage/portfolio-feed-failed",
    )


def test_a_version_that_replaces_none_has_a_step():
    with pytest.raises(ValueError, match="replaces none has a step"):
        parse(b"id: feed-outage\nversion: 1\nreplaces: null\nsteps: []\n")


@pytest.fixture
def split(make_layer):
    """Three desks' memos that all disagree, and a change for each of two, through the gate."""
    when = curation.FEED | {"custodian": "any"}
    scope = {"playbook": "feed-outage", "code": "SOURCE_UNAVAILABLE", "when": when}
    second, third = "memo-a-pricing-view", "memo-a-service-desk-view"
    sources = [curation.FOUNDING, second, third]
    proposed = approving.feed_change()
    proposed["changes"][0]["rests_on"] = [curation.FOUNDING]
    proposed["changes"][1]["rests_on"] = [third]
    proposed["disagreements"] = [{"between": sources, "about": "Who is told of a failed feed."}]
    layer = approving.scripted(make_layer, proposed)
    held_out_cases.write(layer.settings.cases, held_out_cases.FEED_ONLY)
    of_b = scope | {"when": when | {"custodian": "B"}}
    for memo in (
        knowledge.committed_memo("feed-outage-founding"),
        knowledge.memo("Pricing sees a failed feed too.", id=second, scope=scope),
        knowledge.memo(
            "The service desk has custodian B's feed.",
            id=third,
            desk="custodian-b-service",
            scope=of_b,
        ),
    ):
        assert knowledge.intake(layer, memo).status_code == 200
    proposal, run = approving.passed(layer)
    assert len(run["disagreements"]) == 3
    return layer, proposal, run, third


def test_a_split_between_three_desks_takes_a_ruling_for_each_change(split):
    layer, proposal, run, third = split
    listed = approving.rulings(layer, proposal)
    winners = {1: curation.FOUNDING, 2: third}
    sent = []
    for each in listed:
        wanted = winners.get(each["change"])
        ruled_for = wanted if wanted in each["between"] else each["between"][0]
        sent.append(each | {"ruled_for": ruled_for})

    done = approving.approved(layer, proposal, run, sent)

    assert {each["change"] for each in done["rulings"]} <= {1, 2}
    assert third in {each["ruled_for"] for each in done["rulings"]}


def test_one_changes_winner_approves_no_change_that_rests_on_what_lost(split):
    layer, proposal, run, _ = split
    before = kept(layer)
    # For the founding memo wherever it is: so against all that the second change rests on.
    sent = approving.rulings(layer, proposal, curation.FOUNDING)

    refused = approving.approve(layer, proposal, run, sent)

    assert refused.status_code == 422
    assert "Change 2 rests on no source you rule for" in refused.json()["detail"]
    assert kept(layer) == before


@pytest.fixture
def gate_that_kept_nothing(monkeypatch):
    """Gate runs whose lines hold nothing to judge a ruling by."""
    written = gate_run._gate_run

    def without(*given, **named) -> dict:
        made = written(*given, **named)
        del made["disputed"]
        return made

    monkeypatch.setattr(gate_run, "_gate_run", without)
    return monkeypatch


@pytest.mark.parametrize("case", ["a disagreement", "two playbooks"])
def test_no_approval_rests_on_a_gate_run_that_kept_nothing(
    make_layer, gate_that_kept_nothing, case
):
    if case == "a disagreement":
        layer, proposal, run = approving.first_cycle_passed(make_layer)
    else:
        layer, proposal, run = approving.new_playbook(make_layer)
    gate_that_kept_nothing.undo()
    layer.settings.database.unlink()
    assert layer.as_owner("POST", "/api/rebuild").status_code == 200
    before = kept(layer)
    pair = {"change": 3, "between": [approving.CORRECTION, approving.FOUNDING]}

    for sent in ([], [pair | {"ruled_for": approving.CORRECTION}], None):
        refused = approving.approve(layer, proposal, run, sent)
        assert refused.status_code == 409
        assert "whose record holds nothing to judge a ruling by" in refused.json()["detail"]
        assert "the gate must run on a new proposal" in refused.json()["detail"]
    assert kept(layer) == before
    # It is still in the record, and the way out is the one the line names.
    assert approving.listed(layer, proposal["id"])["gate_runs"][0]["outcome"] == "passed"
    assert approving.refuse(layer, proposal["id"]).status_code == 200


def test_a_fresh_gate_after_one_that_kept_nothing_can_be_approved(
    make_layer, gate_that_kept_nothing
):
    layer = approving.scripted(make_layer, gating.honest(), gating.honest())
    gating.first_cycle(layer)
    old, _ = approving.passed(layer)
    gate_that_kept_nothing.undo()
    assert approving.refuse(layer, old["id"]).status_code == 200

    proposal, run = approving.passed(layer)

    assert approving.approve(layer, proposal, run).status_code == 200


def test_a_gate_run_that_kept_nothing_whose_set_changed_goes_stale(
    make_layer, gate_that_kept_nothing
):
    layer, proposal, run = approving.first_cycle_passed(make_layer)
    gate_that_kept_nothing.undo()
    assert layer.retire("feed-outage", 1).status_code == 200

    assert approving.approve(layer, proposal, run).status_code == 409
    assert approving.state(layer, proposal["id"]) == "stale"


def test_a_listed_disagreement_beats_the_only_support(make_layer):
    other = "memo-for-another-playbook"
    proposed = approving.feed_change()
    for change in proposed["changes"]:
        change["rests_on"].append(other)
    between = [curation.MEMO, other]
    proposed["disagreements"] = [{"between": between, "about": "Who is told for custodian B."}]
    layer = approving.scripted(make_layer, proposed)
    approving.feed_only(layer)
    memo = knowledge.memo(
        "The accounting desk keeps custodian B's failed feed.",
        id=other,
        desk="portfolio-accounting",
        date="2026-10-19",
        scope={
            "playbook": "other-feed",
            "code": "SOURCE_UNAVAILABLE",
            "when": curation.FEED | {"custodian": "B"},
        },
    )
    assert knowledge.intake(layer, memo).status_code == 200
    proposal, run = approving.passed(layer)
    assert run["disagreements"] == []
    before = kept(layer)

    # It speaks at custodian B's outcome and covers nothing in the playbook that is changed.
    refused = approving.approve(layer, proposal, run, approving.rulings(layer, proposal, other))

    assert refused.status_code == 422
    assert f"where you rule against {curation.MEMO}" in refused.json()["detail"]
    assert kept(layer) == before
