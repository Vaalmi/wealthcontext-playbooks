"""The curator's answer as it is stored, and the proposals that are refused."""

import copy
import json

import curation
import knowledge
import pytest
from stand_ins import idle_agent


@pytest.fixture
def idle(make_layer):
    return make_layer(agent=lambda *_: idle_agent(), curator=curation.idle)


@pytest.fixture
def ready(idle):
    made = curation.first_cycle(idle)
    return idle, curation.started(idle), made["report"]


def test_the_answer_is_stored_as_a_proposal_of_added_changed_and_removed_steps(ready):
    layer, run_id, report = ready
    sent = curation.proposed(report["reference"])

    response = curation.propose(layer, run_id, sent)

    assert response.status_code == 200, response.text
    found = curation.read(layer, run_id)
    assert found["state"] == "completed"
    proposal = found["proposal"]
    assert proposal["changes"] == sent["changes"]
    assert proposal["disagreements"] == sent["disagreements"]
    assert [each["change"] for each in proposal["changes"]] == ["change", "add", "remove"]
    for each in proposal["changes"]:
        assert each["version"] == 1 and each["rests_on"]
    assert (proposal["state"], proposal["id"]) == ("drafted", response.json()["id"])
    assert proposal["sha256"] == response.json()["sha256"]
    line = knowledge.ledger(layer)[-1]
    assert (line["event"], line["who"]) == ("proposal_recorded", "curator")
    assert line["data"]["proposal"]["content"] == sent


def test_a_proposal_changes_no_playbook(ready):
    layer, run_id, report = ready
    before = layer.as_owner("GET", "/api/playbooks").json()

    assert curation.propose(layer, run_id, curation.proposed(report["reference"])).is_success

    assert layer.as_owner("GET", "/api/playbooks").json() == before
    assert sorted(path.name for path in (layer.settings.playbooks / "feed-outage").iterdir()) == [
        "1.yaml"
    ]


def test_a_new_playbooks_step_names_no_version(ready):
    layer, run_id, _ = ready
    sent = {
        "changes": [
            curation.ADDED
            | {
                "playbook": "stale-price",
                "version": None,
                "code": "PRICE_STALE",
                "step": "price-is-stale",
                "when": {"source_family": "any", "custodian": "any", "feed_status": "any"},
                "tell": "pricing",
            }
        ],
        "disagreements": [],
    }

    assert curation.propose(layer, run_id, sent).status_code == 200
    assert curation.read(layer, run_id)["proposal"]["changes"][0]["version"] is None


def test_an_out_of_date_version_is_still_recorded(ready):
    # Whether the version is the one in use is asked of a proposal, not when it is written down.
    layer, run_id, _ = ready

    assert curation.propose(layer, run_id, curation.one(version=2)).status_code == 200


def test_the_reports_a_proposal_names_become_cited(ready):
    layer, run_id, report = ready

    made = curation.propose(layer, run_id, curation.proposed(report["reference"])).json()

    (found,) = knowledge.gap_reports(layer)
    assert (found["state"], found["proposal_id"]) == ("cited", made["id"])


def test_a_report_named_only_in_a_disagreement_becomes_cited(ready):
    layer, run_id, report = ready
    sent = curation.proposed()
    sent["disagreements"][0]["between"].append(report["reference"])

    assert curation.propose(layer, run_id, sent).status_code == 200

    assert knowledge.gap_reports(layer)[0]["state"] == "cited"


def test_a_report_a_proposal_does_not_name_stays_open(ready):
    layer, run_id, _ = ready

    assert curation.propose(layer, run_id, curation.proposed()).status_code == 200

    (found,) = knowledge.gap_reports(layer)
    assert (found["state"], found["proposal_id"]) == ("open", None)


def test_every_report_under_a_reference_becomes_cited_and_none_filed_later(idle):
    report = curation.first_cycle(idle)["report"]

    def another() -> None:
        made = knowledge.corrected(idle, knowledge.settled_run(idle))
        assert knowledge.review(idle, made["id"]).status_code == 200

    another()
    run_id = curation.started(idle)
    another()

    assert curation.propose(idle, run_id, curation.proposed(report["reference"])).is_success

    states = [each["state"] for each in knowledge.gap_reports(idle)]
    assert states == ["cited", "cited", "open"]
    assert {each["reference"] for each in knowledge.gap_reports(idle)} == {report["reference"]}


def test_a_cited_report_is_not_dismissed(ready):
    layer, run_id, report = ready
    assert curation.propose(layer, run_id, curation.proposed(report["reference"])).is_success

    path = f"/api/gap-reports/{report['id']}/dismiss"
    response = layer.as_owner("POST", path, json={"reason": "duplicate"})

    assert response.status_code == 409
    assert response.json() == {"detail": "The gap report is cited."}


def test_filing_a_cited_report_again_returns_it(ready):
    layer, run_id, report = ready
    assert curation.propose(layer, run_id, curation.proposed(report["reference"])).is_success

    again = knowledge.review(layer, report["correction_id"], "Said again.")

    assert again.status_code == 200 and again.json()["gap_report"] == report["reference"]
    assert len(knowledge.gap_reports(layer)) == 1


def test_a_report_dismissed_since_the_bundle_was_built_refuses_the_proposal(ready):
    layer, run_id, report = ready
    path = f"/api/gap-reports/{report['id']}/dismiss"
    assert layer.as_owner("POST", path, json={"reason": "duplicate"}).status_code == 200

    response = curation.propose(layer, run_id, curation.proposed(report["reference"]))

    assert response.status_code == 409
    line = (
        f"The gap report {report['reference']} is dismissed and no longer open, "
        "so a proposal cannot name it."
    )
    assert response.json() == {"detail": line}
    assert curation.read(layer, run_id)["proposal"] is None
    # It was checked, so it is the run's first refusal, and the second answer is the next turn.
    assert curation.work(layer, run_id)["refusals"] == [line]
    assert curation.propose(layer, run_id, curation.proposed(), turn=2).status_code == 200


NOT_IN_BUNDLE = (
    "The proposal names an item that is not in the bundle as a firm source or a gap report."
)


@pytest.mark.parametrize(
    "named",
    [
        "memo-custody-meeting-notice",  # committed, and never taken in
        "memo-agent-instructions",
        "gap-report-abcdefghabcdefgh",
        "playbooks-in-use",
        "triage-outcome-summary",
        "feed-outage",
    ],
)
@pytest.mark.parametrize("where", ["rests_on", "between"])
def test_an_item_not_in_the_bundle_is_refused_with_one_line(ready, named, where):
    layer, run_id, _ = ready
    sent = curation.proposed()
    if where == "rests_on":
        sent["changes"][0]["rests_on"].append(named)
    else:
        sent["disagreements"][0]["between"].append(named)

    response = curation.propose(layer, run_id, sent)

    assert response.status_code == 422
    assert response.json() == {"detail": NOT_IN_BUNDLE}
    found = curation.read(layer, run_id)
    assert (found["state"], found["proposal"]) == ("working", None)
    (step,) = found["steps"]
    assert (step["tool"], step["outcome"], step["line"]) == (
        "submit_proposal",
        "refused",
        NOT_IN_BUNDLE,
    )


def test_a_source_taken_in_after_the_bundle_was_built_is_not_in_it(ready):
    layer, run_id, _ = ready
    assert knowledge.intake(layer, knowledge.committed_memo("custody-meeting-notice")).is_success

    response = curation.propose(
        layer, run_id, curation.one(rests_on=["memo-custody-meeting-notice"])
    )

    assert response.json() == {"detail": NOT_IN_BUNDLE}


MALFORMED = {
    "no change at all": ({"changes": [], "disagreements": []}, "changes no step"),
    "nothing it rests on": (curation.one(rests_on=[]), "nothing that it rests on"),
    "a step that is no name": (curation.one(step="Feed Failed"), "is not lower-case words"),
    "a playbook that is no name": (curation.one(playbook="../desks"), "is not lower-case words"),
    "a line with a figure": (curation.one(line="Wait for ten dollars."), "holds a number"),
    "a line with a digit": (curation.one(line="Ring in 5 minutes."), "holds a number"),
    "a line too long": (curation.one(line="Ask again. " * 18), "over 160 characters"),
    "a deadline of no form": (curation.one(by="as soon as possible"), "forms of a deadline"),
    "a desk not on the list": (curation.one(tell="compliance"), "not on the list of desks"),
    "a step twice": (
        {"changes": [curation.NARROWED, curation.NARROWED], "disagreements": []},
        "names the same step",
    ),
    "a change of no step listed": (curation.one(step="no-such-step"), "not in the bundle"),
    "a change of no playbook listed": (curation.one(playbook="stale-price"), "not in the bundle"),
    "a removal of no step listed": (
        {"changes": [curation.REMOVED | {"step": "no-such-step"}], "disagreements": []},
        "not in the bundle",
    ),
    "an added step listed already": (
        {"changes": [curation.ADDED | {"step": "portfolio-feed-failed"}], "disagreements": []},
        "lists already",
    ),
    "an added step with no version": (
        {"changes": [curation.ADDED | {"version": None}], "disagreements": []},
        "names no version",
    ),
    "a disagreement of one": (
        curation.one() | {"disagreements": [{"between": [curation.MEMO], "about": "A hold."}]},
        "fewer than two",
    ),
    "a disagreement with itself": (
        curation.one() | {"disagreements": [{"between": [curation.MEMO] * 2, "about": "A hold."}]},
        "fewer than two",
    ),
}


@pytest.mark.parametrize("case", MALFORMED)
def test_a_malformed_proposal_is_refused_with_one_line_and_is_a_step(ready, case):
    layer, run_id, _ = ready
    sent, said = MALFORMED[case]

    response = curation.propose(layer, run_id, copy.deepcopy(sent))

    assert response.status_code == 422
    line = response.json()["detail"]
    assert said in line and "\n" not in line
    found = curation.read(layer, run_id)
    assert found["proposal"] is None
    assert [each["line"] for each in found["steps"]] == [line]


@pytest.mark.parametrize("breach", knowledge.BREAKS)
@pytest.mark.parametrize("field", ["line", "about"])
def test_a_proposals_text_that_breaks_a_rule_is_kept_nowhere(ready, breach, field):
    layer, run_id, _ = ready
    text, rule = knowledge.BREAKS[breach]
    sent = curation.proposed()
    if field == "line":
        sent["changes"][0]["line"] = text
    else:
        sent["disagreements"][0]["about"] = text

    response = curation.propose(layer, run_id, sent)

    assert response.status_code == 422
    assert f"rule {rule}" in response.json()["detail"]
    assert text not in response.text
    assert text.encode() not in knowledge.stored(layer)
    assert text not in json.dumps(curation.read(layer, run_id))


def test_a_version_of_six_digits_is_no_version(ready):
    layer, run_id, _ = ready

    response = curation.propose(layer, run_id, curation.one(version=654321))

    assert response.status_code == 422
    assert b"654321" not in (layer.settings.playbooks / "ledger.jsonl").read_bytes()


def test_the_proposal_and_the_run_hold_no_client_identifier_or_trace(idle):
    made = curation.first_cycle(idle)
    run_id = curation.started(idle)
    assert curation.propose(idle, run_id, curation.proposed(made["report"]["reference"])).is_success

    lines = [
        each
        for each in knowledge.ledger(idle)
        if each["event"].startswith(("curator_", "proposal_"))
    ]
    assert len(lines) == 2
    shown = json.dumps([each["data"] for each in lines]) + json.dumps(curation.read(idle, run_id))
    forbidden = {made["report"]["trace_id"], "WC-", "CRM-"}
    for run in made["runs"]:
        forbidden.add(run["id"])
        forbidden |= {subject for note in run["notes"] for subject in note["subjects"]}
    assert not [each for each in forbidden if each in shown]
