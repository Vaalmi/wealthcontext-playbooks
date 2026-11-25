"""The API's rules, who may call what, and refusals that repeat nothing."""

import re
from collections.abc import Callable
from typing import Any

import approving
import curation
import firm
import gating
import knowledge
import pytest
from layer import AGENT_KEY, ANALYST, CROSS_CHECK_DAY, CURATOR_KEY, CUTOFF, DAY, OPTION, OWNER, ROOT
from stand_ins import idle_agent, sign_in
from stub_model import call

from api_rules import references, rule_breaks
from backend.main import create_app
from backend.settings import Settings

PLANTED = "WC-AC-0002"


@pytest.fixture
def schema(tmp_path):
    settings = Settings(
        database=tmp_path / "schema.sqlite",
        playbooks=firm.copy(tmp_path / "schema-playbooks"),
        triage_log=tmp_path / "schema-log.jsonl",
        wealthcontext_url="http://127.0.0.1:9",
        agent_url="http://127.0.0.1:9",
        wealthcontext_token=sign_in(),
        analyst_token=ANALYST,
        playbook_owner_token=OWNER,
        triage_agent_key=AGENT_KEY,
        curator_key=CURATOR_KEY,
    )
    app = create_app(settings)
    yield app.openapi()
    app.state.journal.close()


def operations(schema):
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            yield method.upper(), path, operation


def test_the_api_keeps_its_rules(schema):
    assert rule_breaks(schema) == []


def test_each_agents_operations_are_the_ones_it_needs(schema):
    tagged = {
        op["operationId"]: key_of(path)
        for _, path, op in operations(schema)
        if "agent" in op["tags"]
    }
    triage_agents = {name for name, key in tagged.items() if key == "X-Triage-Agent-Key"}
    assert tagged.keys() - triage_agents == {
        "read_curator_work",
        "record_proposal",
        "record_no_change",
        "record_unusable_curator_reply",
        "fail_curator_run",
    }
    assert triage_agents == {
        "read_triage_work",
        "record_choice",
        "record_unusable_reply",
        "record_fallback",
        "complete_triage",
        "fail_triage",
        "file_gap_report",
        "record_no_gap_report",
        "read_review_work",
        "record_review",
        "record_unusable_review",
        "record_review_fallback",
        "stop_review",
    }


@pytest.fixture
def layer(make_layer):
    # An agent that leaves every run to the test, and plays only a gate's held-out trials.
    return make_layer(agent=gating.trial_agent, curator=curation.idle)


OWNERS = {
    "retire",
    "rebuild",
    "intake",
    "dismiss_gap_report",
    "start_curator_run",
    "approve",
    "refuse",
    "approve_founding",
    "read_founding",
}


def key_of(path: str) -> str:
    of_curator = path.startswith("/api/agent/curator-runs/")
    return "X-Curator-Key" if of_curator else "X-Triage-Agent-Key"


def headers_for(path: str, operation) -> dict:
    if "agent" in operation["tags"]:
        header = key_of(path)
        return {header: CURATOR_KEY if header == "X-Curator-Key" else AGENT_KEY}
    token = OWNER if operation["operationId"] in OWNERS else ANALYST
    return {"Authorization": f"Bearer {token}"}


def filled(path: str, value: str, attempt: str = "1") -> str:
    return re.sub(r"\{[^}]+\}", value, path.replace("{attempt}", attempt))


BODIES = [
    {"json": {PLANTED: PLANTED}},
    {"json": {"date": PLANTED, "cutoff": PLANTED}},
    {"json": {"date": DAY, "cutoff": f"{CUTOFF}{PLANTED}"}},
    {"json": {"turn": PLANTED, "choice": {"kind": PLANTED}, "usage": PLANTED}},
    {"json": {"turn": 1, "choice": {"kind": "step", "playbook": PLANTED, "step": PLANTED}}},
    {"json": {"reason": PLANTED}},
    {"json": {"content": [PLANTED]}},
    {"json": {"change": {"field": PLANTED, "should_be": PLANTED}, "reason": [PLANTED]}},
    {"json": {"turn": 1, "summary": [PLANTED], "usage": PLANTED}},
    {"json": [PLANTED]},
    {"json": PLANTED},
    {"content": b'{"broken": "' + PLANTED.encode(), "headers_too": True},
    {"content": b"\xff\xfe" + PLANTED.encode(), "headers_too": True},
]


@pytest.mark.parametrize("sent", BODIES)
def test_a_malformed_request_to_any_operation_repeats_nothing(layer, schema, sent):
    run_id = layer.start()["id"]
    note_id = layer.run(run_id)["notes"][0]["id"]
    for method, path, operation in operations(schema):
        where = path.replace("{run_id}", run_id).replace("{note_id}", note_id)
        headers = headers_for(path, operation)
        options = {key: value for key, value in sent.items() if key != "headers_too"}
        if "content" in options:
            headers["Content-Type"] = "application/json"
        response = layer.request(method, filled(where, PLANTED), headers, **options)

        takes_body = "requestBody" in operation
        if takes_body:
            assert response.status_code in (400, 422), operation["operationId"]
            assert list(response.json()) == ["detail"]
            assert "\n" not in response.json()["detail"]
            assert PLANTED not in response.text
            assert PLANTED not in str(response.headers)
        elif response.status_code >= 400:
            assert PLANTED not in response.text


def test_an_identifier_in_a_path_is_not_repeated(layer, schema):
    for method, path, operation in operations(schema):
        if "{" not in path:
            continue
        for attempt in ("1", PLANTED, "999999999999999999999999"):
            where = filled(path, PLANTED, attempt)
            for wanted in (where, f"{where}/", f"{where}?q={PLANTED}"):
                response = layer.request(method, wanted, headers_for(path, operation))
                assert 400 <= response.status_code < 500, (operation["operationId"], wanted)
                assert PLANTED not in response.text
                assert PLANTED not in str(response.headers)
                assert list(response.json()) == ["detail"]


@pytest.mark.parametrize(
    "cutoff",
    ["2026-10-31T16:00:00", "1793462400", "9999-12-31T23:59:59-23:59", "2026-02-30T00:00:00Z", 7],
)
def test_a_cutoff_that_is_no_instant_is_refused(layer, cutoff):
    response = layer.as_analyst("POST", "/api/triage-runs", json={"date": DAY, "cutoff": cutoff})

    assert response.status_code == 422
    assert str(cutoff) not in response.json()["detail"]
    assert layer.wealthcontext.asked == []


@pytest.mark.parametrize("day", ["2026-02-30", "20261015", "0", 20261015, "2026-10-15T00:00:00"])
def test_a_date_that_is_no_day_is_refused(layer, day):
    response = layer.as_analyst("POST", "/api/triage-runs", json={"date": day, "cutoff": CUTOFF})

    assert response.status_code == 422
    assert layer.wealthcontext.asked == []


def test_only_the_analyst_starts_triage(layer):
    asked = {"date": DAY, "cutoff": CUTOFF}

    assert layer.as_owner("POST", "/api/triage-runs", json=asked).status_code == 403
    assert layer.as_agent("POST", "/api/triage-runs", json=asked).status_code == 401
    assert layer.request("POST", "/api/triage-runs", json=asked).status_code == 401
    bearer = {"Authorization": f"Bearer {AGENT_KEY}"}
    assert layer.request("POST", "/api/triage-runs", bearer, json=asked).status_code == 401
    assert layer.wealthcontext.asked == []


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": f"Bearer {ANALYST}"},
        {"Authorization": f"Bearer {OWNER}"},
        {"Authorization": f"Bearer {AGENT_KEY}"},
        {"X-Triage-Agent-Key": ANALYST},
        {"X-Triage-Agent-Key": ""},
        {"X-Curator-Key": ""},
        {"X-Curator-Key": OWNER},
        {"X-Curator-Key": AGENT_KEY},
        {"X-Triage-Agent-Key": CURATOR_KEY},
        {"Authorization": f"Bearer {CURATOR_KEY}"},
    ],
)
def test_only_that_agents_key_opens_its_operations(layer, schema, headers):
    for method, path, operation in operations(schema):
        if "agent" in operation["tags"]:
            response = layer.request(method, filled(path, "0" * 32), dict(headers), json={})
            assert response.status_code == 401, operation["operationId"]


def test_neither_agents_key_opens_a_persons_operation(layer, schema):
    for method, path, operation in operations(schema):
        if "people" in operation["tags"]:
            for send in (layer.as_agent, layer.as_curator):
                response = send(method, filled(path, "0" * 32), json={})
                assert response.status_code == 401, operation["operationId"]


def test_each_key_opens_none_of_the_other_agents_operations(layer, schema):
    for method, path, operation in operations(schema):
        if "agent" in operation["tags"]:
            of_triage = key_of(path) == "X-Triage-Agent-Key"
            send = layer.as_curator if of_triage else layer.as_agent
            response = send(method, filled(path, "0" * 32), json={})
            assert response.status_code == 401, operation["operationId"]


def test_every_operation_is_public_or_behind_one_gate(schema):
    public = {"health", "show_page", "show_triage_run", "press_cycle_button", "read_cycle"}
    for _, path, operation in operations(schema):
        gates = [name for each in operation.get("security", []) for name in each]
        if operation["operationId"] in public:
            assert not gates
        elif "agent" in operation["tags"]:
            of_curator = key_of(path) == "X-Curator-Key"
            assert gates == (["CuratorKey"] if of_curator else ["APIKeyHeader"])
        else:
            assert gates == ["HTTPBearer"]


def test_with_nothing_configured_the_gates_refuse(make_layer):
    bare = make_layer(
        agent=lambda *_: idle_agent(),
        overrides={
            "analyst_token": "",
            "playbook_owner_token": "",
            "triage_agent_key": "",
            "curator_key": "",
        },
    )
    agent_path = f"/api/agent/triage-runs/{'0' * 32}/attempts/1"
    for sent in ({}, {"Authorization": "Bearer unknown"}, {"Authorization": f"Bearer {ANALYST}"}):
        assert bare.request("GET", "/api/playbooks", sent).status_code == 401
    for sent in ({}, {"X-Triage-Agent-Key": ""}, {"X-Triage-Agent-Key": AGENT_KEY}):
        assert bare.request("GET", agent_path, sent).status_code == 401
    curator_path = f"/api/agent/curator-runs/{'0' * 32}/attempts/1"
    for sent in ({}, {"X-Curator-Key": ""}, {"X-Curator-Key": CURATOR_KEY}):
        assert bare.request("GET", curator_path, sent).status_code == 401


def test_the_store_reports_both_playbooks_in_use(layer):
    found = layer.as_analyst("GET", "/api/playbooks").json()

    assert [(each["id"], each["version"]) for each in found["in_use"]] == [
        ("feed-outage", 1),
        ("missing-cross-check", 1),
    ]
    assert layer.as_owner("GET", "/api/playbooks").status_code == 200


def test_another_host_name_is_not_served(layer):
    response = layer.request("GET", "/", {"Host": "elsewhere.example"})

    assert response.status_code == 400
    assert response.json() == {"detail": "That host is not served here."}


@pytest.mark.parametrize("app", ["backend", "agent", "curator"])
def test_a_near_miss_of_any_path_repeats_nothing(tmp_path, app):
    from decimal import Decimal

    from fastapi.testclient import TestClient

    from curator import entrypoint as curators
    from triage_agent import entrypoint

    if app == "agent":
        given = entrypoint.Settings("http://127.0.0.1:9", "", Decimal(2), AGENT_KEY, {})
        served = entrypoint.create_app(given)
    elif app == "curator":
        for_it = curators.Settings("http://127.0.0.1:9", "", Decimal(2), CURATOR_KEY, {})
        served = curators.create_app(for_it)
    else:
        settings = Settings(
            database=tmp_path / "near.sqlite",
            playbooks=firm.copy(tmp_path / "near-playbooks"),
            triage_log=tmp_path / "near-log.jsonl",
            wealthcontext_url="http://127.0.0.1:9",
            agent_url="http://127.0.0.1:9",
            wealthcontext_token=sign_in(),
            analyst_token=ANALYST,
            playbook_owner_token=OWNER,
            triage_agent_key=AGENT_KEY,
        )
        served = create_app(settings)
    client = TestClient(served, base_url="http://127.0.0.1", follow_redirects=False)
    paths = [path.split("{")[0].rstrip("/") for path in served.openapi()["paths"]]
    for path in {path for path in paths if path}:
        for method in ("GET", "POST", "DELETE"):
            response = client.request(method, f"{path}/?q={PLANTED}", json={PLANTED: PLANTED})
            assert 400 <= response.status_code < 500, (method, path)
            assert list(response.json()) == ["detail"]
            assert PLANTED not in response.text
            assert PLANTED not in str(response.headers)
    if app == "backend":
        served.state.journal.close()


def test_the_agents_app_takes_only_the_agents_key(tmp_path):
    from decimal import Decimal

    from fastapi.testclient import TestClient

    from triage_agent import entrypoint

    given = entrypoint.Settings("http://127.0.0.1:9", "", Decimal(2), AGENT_KEY, {})
    agent = TestClient(entrypoint.create_app(given))
    asked = {"kind": "triage", "run_id": "0" * 32, "attempt": 1}

    assert agent.post("/invocations", json=asked).status_code == 401
    malformed = agent.post("/invocations", json={**asked, "run_id": PLANTED})
    assert malformed.status_code == 422 and PLANTED not in malformed.text
    assert agent.get("/ping").json() == {"status": "Healthy"}


def test_no_operation_gives_out_an_action_nobody_approved(make_layer, tmp_path):
    unapproved = firm.unapproved(firm.copy(tmp_path / "unapproved"))
    layer = make_layer(
        playbooks=unapproved,
        replies=lambda: [call("choose_step", option=OPTION)],
        curator=curation.idle,
        # The page then shows the recorded cycle's steps too, read from these records.
        overrides={"cycle": True, "sources": ROOT / "sources"},
    )
    run = layer.triage()
    run_id = run["id"]
    # A correction reads the chosen step. What it read is given out by no operation either.
    path = f"/api/triage-runs/{run_id}/notes/{run['notes'][0]['id']}/corrections"
    told = {"change": {"field": "by", "should_be": "within 2 hours"}, "reason": "That is too late."}
    correction_id = layer.as_analyst("POST", path, json=told).json()["id"]
    memo = knowledge.memo(id="memo-for-a-proposal")
    assert knowledge.intake(layer, memo).status_code == 200
    # The curator's bundle lists the steps in use. It gives out none of their actions either.
    curator_run = curation.started(layer)
    # A proposal is shown beside the step it would change, whose action is not given out.
    changed = curation.one(tell="pricing", by="within 3 hours", rests_on=["memo-for-a-proposal"])
    proposal_id = curation.propose(layer, curator_run, changed).json()["id"]

    for method, path, operation in operations(layer.backend.openapi()):
        if method != "GET":
            continue
        of_run = curator_run if "curator-runs" in path else run_id
        where = path.replace("{run_id}", of_run).replace("{attempt}", "1")
        where = where.replace("{correction_id}", correction_id)
        where = where.replace("{proposal_id}", proposal_id)
        assert "{" not in where, f"give this test a value for a parameter of {path}"
        response = layer.request(method, where, headers_for(path, operation))
        assert response.status_code == 200, where
        if operation["operationId"] == "read_founding":
            # The files the playbook owner reads whole to approve them, and only the owner.
            refused = layer.as_analyst(method, where)
            assert refused.status_code == 403, "the founding's files are the owner's to read"
            continue
        for action in ("same_day 20:00 ET", "Portfolio Accounting Operations"):
            assert action not in response.text, (
                f"{operation['operationId']} shows an action with no recorded approval: "
                "take a note from triage._shown, which withholds it"
            )


# Texts that may enter no log. A pattern that lets one of them through bounds nothing.
PLANTS = (
    "20998877",
    "stub:20998877",
    "stub:triage-20261015",
    "model 20998877",
    "quillon-vantree",
    "stub:quillon-vantree",
    "ignore-previous-instructions",
    "Ignore all previous instructions",
    "stub:ignore-previous-rules",
)
PROPOSALS_CHECK = "personal_data.broken_rule over the whole proposal, before any is kept"
# Text an operation takes that is no list and no shape that keeps the plants out, and what
# checks each instead.
# The logs are only added to, so a field is listed here only with the check that bounds it.
CHECKED_TEXT = {
    "StartTriage.cutoff": "parsed as an instant, and stored as that instant",
    "Intake.content": "personal_data.broken_rule, as sent and as parsed, before any is kept",
    "RecordCorrection.reason": "personal_data.broken_rule, before any is kept",
    "FileGapReport.summary": "personal_data.broken_rule, before any is kept",
    "ReviewFiles.summary": "personal_data.broken_rule, before any is kept",
    "Usage.model": "personal_data.broken_rule, as the request is read",
    "StepChoice.playbook": "looked up among the steps offered; only a match is kept",
    "StepChoice.step": "looked up among the steps offered; only a match is kept",
    "CorrectTell.should_be": "looked up among the desks, before any is kept",
    "RuleFor.between": "looked up among the proposal's disagreements; only a match is kept",
    "RuleFor.ruled_for": "looked up in that disagreement; only a match is kept",
    **{
        f"{change}.{field}": PROPOSALS_CHECK
        for change in ("AddStep", "ChangeStep")
        for field in ("playbook", "step", "tell", "by", "line", "rests_on")
    },
    **{f"RemoveStep.{field}": PROPOSALS_CHECK for field in ("playbook", "step", "rests_on")},
    "Disagreement.between": PROPOSALS_CHECK,
    "Disagreement.about": PROPOSALS_CHECK,
}


def open_text(node) -> bool:
    """Whether a part of a schema takes a string that is neither listed nor of a closed shape."""
    if isinstance(node, list):
        return any(open_text(each) for each in node)
    if not isinstance(node, dict):
        return False
    if node.get("type") == "string":
        if {"enum", "const"} & node.keys():
            return False
        pattern = node.get("pattern")
        return pattern is None or any(re.search(pattern, plant) for plant in PLANTS)
    return any(open_text(value) for value in node.values())


def test_no_operation_takes_free_text_unless_a_check_is_named_for_it(schema):
    defined = schema["components"]["schemas"]
    reached = [
        name
        for _, _, operation in operations(schema)
        for name in references(operation.get("requestBody", {}))
    ]
    found = set()
    seen: set[str] = set()
    while reached:
        if (name := reached.pop()) in seen:
            continue
        seen.add(name)
        reached.extend(references(defined[name]))
        for field, rules in defined[name].get("properties", {}).items():
            if open_text(rules):
                found.add(f"{name}.{field}")

    assert found == CHECKED_TEXT.keys(), (
        "a free-text input can put personal data in a log that is only added to: "
        "make it a closed list, or name here the check that bounds it"
    )


def sent_as(layer, run: dict) -> dict[str, Callable[[str], Any]]:
    """A request for each input in CHECKED_TEXT, with a given text in that input alone."""
    working = layer.run(layer.start(CROSS_CHECK_DAY)["id"])
    note = knowledge.uncovered(working)[0]
    made = knowledge.corrected(layer, run)["id"]
    work = f"/api/agent/triage-runs/{working['id']}/attempts/1/notes"
    pending = working["notes"][0]["id"]

    def choose(**named):
        choice = {"kind": "step", "playbook": "missing-cross-check", "step": "other"} | named
        body = {"turn": 1, "choice": choice, "usage": knowledge.USAGE}
        return layer.as_agent("POST", f"{work}/{pending}/choice", json=body)

    def named_model(text: str):
        body = {"turn": 1, "usage": knowledge.USAGE | {"model": text}}
        return layer.as_agent("POST", f"{work}/{note}/no-gap-report", json=body)

    def tell(text: str):
        change = {"field": "tell", "should_be": text}
        sent = {"change": change, "reason": "The wrong desk is told."}
        return knowledge.correct(layer, run, sent)

    curator_run = curation.started(layer)

    def ruling(field: str) -> Callable[[str], Any]:
        def send(text: str):
            # On a proposal that passed the gate, so that the ruling's own check is what answers.
            assert curation.fail(layer, curator_run).status_code == 200
            approving.feed_only(layer)
            second = curation.started(layer)
            between = [curation.MEMO, curation.FOUNDING]
            proposed = approving.feed_change()
            proposed["disagreements"] = [{"between": between, "about": "Who is told."}]
            proposal = curation.propose(layer, second, proposed).json()
            gate_run = gating.gated(layer, proposal["id"])
            assert gate_run["outcome"] == "passed"
            ruled = approving.rulings(layer, proposal)
            value = [text, curation.MEMO] if field == "between" else text
            ruled[0] = ruled[0] | {field: value}
            return approving.approve(layer, proposal, gate_run, ruled)

        return send

    def proposing(variant: str, field: str) -> Callable[[str], Any]:
        def send(text: str):
            changes = {
                "AddStep": curation.ADDED,
                "ChangeStep": curation.NARROWED,
                "RemoveStep": curation.REMOVED,
            }
            value = [text] if field in ("rests_on", "between") else text
            if variant == "Disagreement":
                told = {"between": [curation.MEMO], "about": "A hold."} | {field: value}
                sent = curation.one() | {"disagreements": [told]}
            else:
                sent = {"changes": [changes[variant] | {field: value}], "disagreements": []}
            return curation.propose(layer, curator_run, sent)

        return send

    return {
        **{
            f"{variant}.{field}": proposing(variant, field)
            for variant, fields in (
                ("AddStep", ("playbook", "step", "tell", "by", "line", "rests_on")),
                ("ChangeStep", ("playbook", "step", "tell", "by", "line", "rests_on")),
                ("RemoveStep", ("playbook", "step", "rests_on")),
                ("Disagreement", ("between", "about")),
            )
            for field in fields
        },
        "StartTriage.cutoff": lambda text: layer.as_analyst(
            "POST", "/api/triage-runs", json={"date": DAY, "cutoff": text}
        ),
        "Intake.content": lambda text: knowledge.intake(layer, knowledge.memo(text)),
        "RecordCorrection.reason": lambda text: knowledge.correct(
            layer, run, knowledge.HOLD_NOT | {"reason": text}
        ),
        "FileGapReport.summary": lambda text: knowledge.file_report(
            layer, working["id"], note, text
        ),
        "ReviewFiles.summary": lambda text: knowledge.review(layer, made, text),
        "Usage.model": named_model,
        "StepChoice.playbook": lambda text: choose(playbook=text),
        "StepChoice.step": lambda text: choose(step=text),
        "CorrectTell.should_be": tell,
        "RuleFor.between": ruling("between"),
        "RuleFor.ruled_for": ruling("ruled_for"),
    }


@pytest.mark.parametrize("field", sorted(CHECKED_TEXT))
@pytest.mark.parametrize("plant", PLANTS)
def test_the_check_named_for_each_text_input_keeps_the_plants_out(layer, field, plant):
    requests = sent_as(layer, knowledge.settled_run(layer))
    assert requests.keys() == CHECKED_TEXT.keys(), "give this test a request for the new input"

    response = requests[field](plant)

    assert response.status_code in (400, 409, 422), f"{field} took {plant!r}"
    if field.split(".")[0] in ("AddStep", "ChangeStep", "RemoveStep", "Disagreement"):
        # By the check named, not by what happened to be wrong with that proposal besides.
        assert "(rule " in response.json()["detail"], f"{field} was not checked for {plant!r}"
    kept = knowledge.stored(layer)
    assert plant.encode() not in kept, f"{field} kept {plant!r}: give it a check with no exception"
