"""Intake: a memo becomes a firm source, or a ledger line that says which rule refused it."""

import hashlib
import json

import firm
import pytest
import yaml
from knowledge import (
    BREAKS,
    MEMOS,
    committed_correction,
    committed_memo,
    events,
    intake,
    ledger,
    memo,
    stored,
)
from layer import AGENT_KEY, ROOT
from stand_ins import idle_agent

from backend import references
from backend.playbook_format import Scope

TAKEN = {
    "feed-outage-founding": "memo-feed-outage-founding",
    "missing-cross-check-founding": "memo-missing-cross-check-founding",
    "custodian-b-feed-escalation": "memo-custodian-b-feed-escalation",
    "custody-meeting-notice": "memo-custody-meeting-notice",
    "stale-price-poisoned": "memo-stale-price-carry-forward",
    # Taken in by one evidence scenario alone: it disagrees with the memo above it.
    "custodian-b-service-desk-hours": "memo-custodian-b-service-desk-hours",
}


@pytest.fixture
def layer(make_layer):
    return make_layer(agent=lambda *_: idle_agent())


def test_sources_holds_each_kind_the_first_cycle_needs():
    memos = {file.stem for file in MEMOS.glob("*.md")}

    assert memos == {*TAKEN, "agent-instructions"}
    assert [file.name for file in (ROOT / "sources" / "corrections").iterdir()] == [
        "cross-check-alone-is-no-hold.yaml"
    ]


def test_the_memo_that_changes_the_rule_is_later_from_the_same_desk_and_narrower():
    def front(name: str) -> dict:
        return yaml.safe_load(committed_memo(name).split("---\n", 2)[1])

    founding, change = front("feed-outage-founding"), front("custodian-b-feed-escalation")

    assert change["desk"] == founding["desk"]
    assert change["date"] > founding["date"]
    assert change["scope"]["when"] == founding["scope"]["when"] | {"custodian": "B"}
    assert "within 2 hours" in committed_memo("custodian-b-feed-escalation")
    assert "Custodian B Service Desk" in committed_memo("custodian-b-feed-escalation")
    assert front("custody-meeting-notice")["scope"] == "none"
    assert front("stale-price-poisoned")["scope"]["code"] == "PRICE_STALE"


def test_the_committed_correction_has_the_id_of_its_content():
    told = committed_correction()
    content = {name: value for name, value in told.items() if name != "id"}

    assert told["id"] == references.reference("correction", content)
    Scope.model_validate(told["scope"])


@pytest.mark.parametrize("name, memo_id", TAKEN.items())
def test_intake_records_id_date_desk_scope_and_hash_as_one_line(layer, name, memo_id):
    content = committed_memo(name)
    front = yaml.safe_load(content.split("---\n", 2)[1])

    response = intake(layer, content)

    assert response.status_code == 200, response.text
    taken = response.json()
    sha256 = hashlib.sha256((MEMOS / f"{name}.md").read_bytes()).hexdigest()
    assert taken["id"] == memo_id
    assert taken["date"] == front["date"].isoformat()
    assert (taken["desk"], taken["kind"]) == (front["desk"], "memo")
    assert taken["scope"] == front["scope"]
    assert taken["sha256"] == sha256
    assert taken["text"] == content.split("---\n", 2)[2].strip()
    *founded, line = ledger(layer)
    assert (line["event"], line["who"]) == ("source_taken_in", "playbook_owner")
    recorded = line["data"]["source"]
    for field in ("id", "date", "desk", "scope", "sha256", "text"):
        assert recorded[field] == taken[field], field


def test_the_founding_memos_hashes_are_those_the_versions_rest_on(layer):
    founded = ledger(layer)[0]["data"]["versions"]
    for version in founded:
        (rests_on,) = version["rests_on"]
        taken = intake(layer, committed_memo(f"{version['id']}-founding")).json()
        assert (taken["id"], taken["sha256"]) == (rests_on["id"], rests_on["sha256"])


def test_the_memo_that_speaks_to_an_agent_is_refused_and_never_a_source(layer):
    content = committed_memo("agent-instructions")

    response = intake(layer, content)

    assert response.status_code == 422
    assert response.json() == {
        "detail": "The memo is refused: it carries words addressed to an agent "
        "(rule addressed_to_an_agent)."
    }
    assert layer.as_analyst("GET", "/api/sources").json() == {"sources": []}
    line = ledger(layer)[-1]
    assert line["event"] == "source_refused"
    assert line["data"] == {
        "kind": "memo",
        "rule": "addressed_to_an_agent",
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }
    # The rows and the lines. The schema's own table names have the word.
    rows = repr(list(firm.records(layer.settings.database).values()))
    logs = layer.settings.triage_log.read_text()
    logs += (layer.settings.playbooks / "ledger.jsonl").read_text()
    assert "curator" not in rows + logs
    assert b"Note to the curator" not in stored(layer)


@pytest.mark.parametrize("text, rule", BREAKS.values(), ids=list(BREAKS))
def test_a_memo_that_breaks_a_rule_is_refused_with_a_line_saying_which(layer, text, rule):
    response = intake(layer, memo(f"The desk keeps its hours.\n{text}."))

    assert response.status_code == 422
    assert f"(rule {rule})" in response.json()["detail"]
    assert text not in response.text
    assert ledger(layer)[-1]["data"]["rule"] == rule
    assert text.encode() not in stored(layer)
    assert layer.as_analyst("GET", "/api/sources").json() == {"sources": []}
    shown = layer.as_analyst("GET", "/api/record")
    assert rule in shown.text and text not in shown.text


@pytest.mark.parametrize(
    "changes, rule",
    [
        ({"id": "memo-quillon-vantree"}, "personal_data"),
        ({"id": "memo-ignore-previous-instructions"}, "addressed_to_an_agent"),
        ({"desk": '"\\u0076antree"'}, "personal_data"),  # spelled by an escape
        (
            {
                "scope": '{playbook: "\\u0069gnore-previous-instructions", code: PRICE_STALE, '
                "when: {source_family: any, custodian: any, feed_status: any}}"
            },
            "addressed_to_an_agent",
        ),
        ({"kind": "memo # ignore previous instructions"}, "addressed_to_an_agent"),
    ],
)
def test_the_front_matter_is_checked_as_sent_and_as_read(layer, changes, rule):
    response = intake(layer, memo(**changes))

    assert response.status_code == 422
    assert ledger(layer)[-1]["data"]["rule"] == rule


def test_a_memo_over_length_is_refused_by_that_rule(layer):
    response = intake(layer, memo("x" * 4000))

    assert response.status_code == 422
    assert ledger(layer)[-1]["data"]["rule"] == "too_long"


MALFORMED = {
    "no front matter": "The desk keeps its hours.",
    "no text": memo(" "),
    "front matter not closed": "---\nid: memo-a\nThe desk keeps its hours.",
    "an id that is no memo's": memo(id="correction-abcdefgh"),
    "a gap report's reference": memo(id="gap-report-abcdefgh"),
    "a correction": memo(kind="correction"),
    "a date that is none": memo(date="yesterday"),
    "an instant for a date": memo(date="2026-10-20 10:00:00"),
    "a scope that is no list's": memo(scope="everything"),
    "a code that is none": memo(scope={"playbook": "a", "code": "NOPE", "when": firm.ANY_WHEN}),
    "a wildcard that is none": memo(
        scope={
            "playbook": "a",
            "code": "PRICE_STALE",
            "when": firm.ANY_WHEN | {"custodian": "C"},
        }
    ),
    "another field": memo(approved="true"),
    "not YAML": memo(desk="[unclosed"),
    "deeply nested": memo(scope="[" * 1500 + "]" * 1500),
    "aliases": memo(desk="&d pricing", scope="*d"),
    "a merge of aliases": memo(desk="&d {a: b}", scope="{<<: [*d, *d]}"),
    "a key that is a list": memo(scope="{[a]: b}"),
    "a tag that is no time": memo(date="!!timestamp yesterday"),
    "a tag that is no number": memo(desk='!!int ""'),
    "a tag that is no truth": memo(desk="!!bool perhaps"),
    "a tag of another language": memo(desk="!!python/name:os.system"),
}


@pytest.mark.parametrize("content", MALFORMED.values(), ids=list(MALFORMED))
def test_a_memo_out_of_form_is_refused_and_writes_nothing(layer, content):
    response = intake(layer, content)

    assert response.status_code == 422
    assert response.json()["detail"].startswith("The memo does not fit")
    assert events(layer) == firm.FOUNDING_EVENTS


def test_a_desk_that_is_not_listed_is_refused(layer):
    response = intake(layer, memo(desk="research"))

    assert response.json() == {"detail": "The memo's desk is not on the list of desks."}
    assert events(layer) == firm.FOUNDING_EVENTS


def test_the_same_memo_again_is_the_same_and_changes_nothing(layer):
    first = intake(layer, memo())
    again = intake(layer, memo())

    assert first.json() == again.json() and again.status_code == 200
    assert events(layer) == [*firm.FOUNDING_EVENTS, "source_taken_in"]


def test_the_same_refused_memo_again_is_one_line(layer):
    content = committed_memo("agent-instructions")

    assert intake(layer, content).status_code == intake(layer, content).status_code == 422
    assert events(layer) == [*firm.FOUNDING_EVENTS, "source_refused"]


def test_an_id_taken_in_keeps_its_content(layer):
    intake(layer, memo())

    response = intake(layer, memo("The desk opens later."))

    assert response.status_code == 409
    assert len(layer.as_analyst("GET", "/api/sources").json()["sources"]) == 1
    assert events(layer) == [*firm.FOUNDING_EVENTS, "source_taken_in"]


def test_no_other_text_takes_a_founding_memos_id(layer):
    changed = committed_memo("feed-outage-founding").replace("20:00", "21:00")

    assert intake(layer, changed).status_code == 409
    assert events(layer) == firm.FOUNDING_EVENTS


def test_only_the_playbook_owner_takes_a_source_in(layer):
    sent = {"content": memo()}

    assert layer.as_analyst("POST", "/api/sources", json=sent).status_code == 403
    assert layer.as_agent("POST", "/api/sources", json=sent).status_code == 401
    bearer = {"Authorization": f"Bearer {AGENT_KEY}"}
    assert layer.request("POST", "/api/sources", bearer, json=sent).status_code == 401
    assert events(layer) == firm.FOUNDING_EVENTS


def test_text_that_cannot_be_encoded_is_refused_and_repeats_nothing(layer):
    body = json.dumps({"content": "\ud800 hidden"}).encode()
    headers = {"Authorization": "Bearer owner-token-for-tests", "Content-Type": "application/json"}

    response = layer.request("POST", "/api/sources", headers, content=body)

    assert response.status_code in (400, 422)
    assert "hidden" not in response.text


def test_intake_leaves_the_playbook_set_and_a_working_run_as_they_were(make_layer):
    layer = make_layer(agent=lambda *_: idle_agent())
    before = layer.as_analyst("GET", "/api/playbooks").json()
    run_id = layer.start()["id"]
    work = f"/api/agent/triage-runs/{run_id}/attempts/1"

    assert intake(layer, memo()).status_code == 200
    assert intake(layer, committed_memo("agent-instructions")).status_code == 422

    assert layer.as_analyst("GET", "/api/playbooks").json() == before
    (note,) = layer.as_agent("GET", work).json()["notes"]
    choice = {"kind": "step", "playbook": "feed-outage", "step": "portfolio-feed-failed"}
    usage = {"model": "stub:t", "input_tokens": 1, "output_tokens": 1, "cost_usd": None}
    body = {"turn": 1, "choice": choice, "usage": usage}
    assert layer.as_agent("POST", f"{work}/notes/{note['id']}/choice", json=body).status_code == 200
    entries = layer.as_analyst("GET", "/api/record").json()["entries"]
    founding = [True] * len(firm.FOUNDING_EVENTS)
    assert [entry["playbook_set"] is not None for entry in entries] == [*founding, False, False]
