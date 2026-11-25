"""The held-out check: the case set, its hash and writer, grading by fixed rules, and the
trials the gate runs on the triage agent."""

import copy
import json
import shutil
import threading
import time
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import curation
import firm
import gating
import held_out_cases
import knowledge
import pytest
import stand_ins
import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from layer import OWNER, ROOT
from stub_model import call, choosing

from backend import bundle, cases
from backend.gate import held_out, trials
from modelkit.prices import Price
from triage_agent import harness

WRITER_MODEL = "gemini-4-neon"
FEED_CHANGE = {
    "changes": [copy.deepcopy(curation.NARROWED), copy.deepcopy(curation.ADDED)],
    "disagreements": [],
}
NEON = {"name": "Gemini 4 Neon", "model": WRITER_MODEL}
DESKS = list(yaml.safe_load((ROOT / "playbooks" / "desks.yaml").read_text()))


def scripted(make_layer, proposal: dict, **options):
    return make_layer(
        agent=gating.trial_agent,
        curates=lambda: [call("submit_proposal", **proposal)],
        **options,
    )


def drafted(layer) -> dict:
    run = curation.curated(layer)
    assert run["state"] == "completed", run
    return run["proposal"]


def first_cycle(make_layer, proposal: dict | None = None, **options) -> tuple:
    layer = scripted(make_layer, proposal or gating.honest(), **options)
    gating.first_cycle(layer)
    return layer, drafted(layer)


def feed_only(make_layer, **options) -> tuple:
    """The custodian B memo's change alone, on the real triage agent: no triage is needed."""
    layer = make_layer(curates=lambda: [call("submit_proposal", **FEED_CHANGE)], **options)
    held_out_cases.write(layer.settings.cases, held_out_cases.FEED_ONLY)
    for name in curation.FIRST_CYCLE:
        assert knowledge.intake(layer, knowledge.committed_memo(name)).status_code == 200
    return layer, drafted(layer)


def state_of(layer, proposal_id: str) -> dict:
    (found,) = [p for p in gating.record(layer)["proposals"] if p["id"] == proposal_id]
    return found


def failures(run: dict) -> list[str]:
    (found,) = [each for each in run["checks"] if each["check"] == "held_out"]
    return [failure["line"] for failure in found["failures"]]


# The case set


def made_set(tmp_path, cases_made=held_out_cases.FIRST_CYCLE) -> Path:
    return held_out_cases.write(tmp_path / "cases", cases_made)


def test_a_case_set_reads_whole_with_its_writer(tmp_path):
    found = cases.read_set(made_set(tmp_path), DESKS)

    assert [case.kind for case in found.cases] == ["regression"] * 4 + ["new_rule"] * 4
    assert found.writer.name == "A writer of these tests"
    (pas_b,) = [case for case in found.cases if case.id == "made-pas-b-failed"]
    assert pas_b.ids == ["e1"]
    assert pas_b.expected["e1"].tell == "custodian-b-service"
    assert len(found.sha256) == 64


@pytest.mark.parametrize(
    "file", ["case-set.yaml", "regression/made-price-stale.yaml", "new-rule/expected-notes.json"]
)
def test_changing_any_file_of_the_case_set_changes_its_hash(tmp_path, file):
    folder = made_set(tmp_path)
    before = cases.read_set(folder, DESKS).sha256
    path = folder / file
    path.write_bytes(path.read_bytes() + (b"\n" if path.suffix == ".json" else b"# changed\n"))

    assert cases.read_set(folder, DESKS).sha256 != before


def broken_inputs(folder: Path) -> None:
    path = folder / "regression" / "made-pas-a-failed.yaml"
    case = yaml.safe_load(path.read_text())
    case["exceptions"][0]["subjects"] = ["WC-AC-0001"]
    path.write_text(yaml.safe_dump(case))


def a_note_missing(folder: Path) -> None:
    path = folder / "new-rule" / "expected-notes.json"
    given = json.loads(path.read_text())
    given["cases"][3]["notes"].pop()
    path.write_text(json.dumps(given))


def a_fifth_case(folder: Path) -> None:
    shutil.copy(folder / "regression" / "made-price-stale.yaml", folder / "regression" / "x.yaml")


def no_writer(folder: Path) -> None:
    (folder / "case-set.yaml").write_text("writer: {}\n")


def a_desk_not_on_the_list(folder: Path) -> None:
    path = folder / "regression" / "expected-notes.json"
    path.write_text(path.read_text().replace("portfolio-accounting", "the-front-office"))


def a_date_that_is_no_date(folder: Path) -> None:
    path = folder / "regression" / "made-pas-a-failed.yaml"
    case = yaml.safe_load(path.read_text())
    case["date"] = "not-a-date"
    path.write_text(yaml.safe_dump(case))


def an_exception_id_twice(folder: Path) -> None:
    path = folder / "new-rule" / "made-both-pas-feeds.yaml"
    case = yaml.safe_load(path.read_text())
    case["exceptions"][1]["id"] = "e1"
    path.write_text(yaml.safe_dump(case))


def a_file_that_is_no_case(folder: Path) -> None:
    (folder / "regression" / ".DS_Store").write_bytes(b"made")


def a_folder_inside(folder: Path) -> None:
    (folder / "new-rule" / "more").mkdir()


@pytest.mark.parametrize(
    "broken",
    [
        broken_inputs,
        a_note_missing,
        a_fifth_case,
        no_writer,
        a_desk_not_on_the_list,
        a_date_that_is_no_date,
        an_exception_id_twice,
        a_file_that_is_no_case,
        a_folder_inside,
    ],
)
def test_a_case_set_that_does_not_hold_together_is_refused(tmp_path, broken):
    folder = made_set(tmp_path)
    broken(folder)

    with pytest.raises(cases.CasesInvalid):
        cases.read_set(folder, DESKS)


def test_a_shape_error_quotes_nothing_of_the_answer(tmp_path):
    folder = made_set(tmp_path)
    inputs = cases._inputs(folder, "regression")
    planted = "WC-AC-0099 Jane Example ten dollars"
    given = {
        "cases": [
            {"case": "made-pas-a-failed", "notes": [{"exception": "e1", "outcome": planted}]},
            {"case": planted, "notes": []},
        ]
    }

    errors = cases.shape_errors(given, inputs, DESKS, "regression")

    assert errors
    for word in ("WC-AC-0099", "Jane", "dollars"):
        assert word not in json.dumps(errors)


def test_the_committed_case_set_holds_together():
    # Read as the gate reads it. Nothing of an expected note is shown, even on a failure.
    found = cases.read_set(ROOT / "cases", DESKS)

    assert sorted(case.kind for case in found.cases) == ["new_rule"] * 4 + ["regression"] * 4
    assert (found.writer.name, found.writer.model) == ("Gemini 4 Neon", WRITER_MODEL)
    assert all(1 <= len(case.exceptions) <= 3 for case in found.cases)


@pytest.mark.parametrize(
    ("setting", "written"),
    [
        ("google_genai:gemini-4-neon-xhigh", True),
        ("google:Gemini-4-Neon", True),
        ("stub:gemini_4_neon", True),
        ("anthropic:claude-opus-5-5", False),
        ("replay:recordings/first-cycle.json", False),
        ("", False),
    ],
)
def test_the_writer_is_known_whatever_its_provider_or_effort(setting, written):
    writer = cases.Writer(name="Gemini 4 Neon", model=WRITER_MODEL)

    assert cases.written_by(setting, writer) is written


# Grading by fixed rules


def test_the_gate_passes_a_proposal_whose_cases_give_their_notes(make_layer):
    layer, proposal = first_cycle(make_layer)

    run = gating.gated(layer, proposal["id"])

    assert gating.outcomes(run)["held_out"] == "passed"
    held = run["held_out"]
    assert held["case_set_sha256"] == cases.read_set(layer.settings.cases, DESKS).sha256
    graded = {each["case"]: (each["in_use"], each["proposed"]) for each in held["graded"]}
    # Regression cases pass before and after; the new-rule cases only once it is made.
    assert {graded[name] for name in graded if name.startswith("made-pas-b-f")} == {
        ("failed", "passed")
    }
    assert graded["made-price-stale"] == ("passed", "passed")
    assert state_of(layer, proposal["id"])["state"] == "gate_passed"


def test_the_gate_fails_a_proposal_that_breaks_a_regression_case_and_names_it(make_layer):
    broken = gating.honest()
    broken["changes"][0]["by"] = "same_day 18:00 ET"
    layer, proposal = first_cycle(make_layer, broken)

    run = gating.gated(layer, proposal["id"])

    assert gating.failed(run) == {"held_out"}
    assert failures(run) == [
        "Regression case made-pas-a-failed, exception e1: by differs from its expected note "
        "once the proposal is made.",
        "New-rule case made-both-pas-feeds, exception e1: by differs from its expected note "
        "once the proposal is made.",
    ]
    assert run["held_out"]["trials"] == []  # graded by fixed rules first: no trial is needed
    assert state_of(layer, proposal["id"])["state"] == "gate_failed"
    assert {each["state"] for each in knowledge.gap_reports(layer)} == {"open"}


def test_a_proposal_that_does_not_meet_a_new_rule_case_fails_naming_it(make_layer):
    # The feed change alone leaves the cross-check's hold where the new-rule cases move it.
    layer, proposal = first_cycle(
        make_layer, FEED_CHANGE | {"changes": copy.deepcopy(FEED_CHANGE["changes"])}
    )

    run = gating.gated(layer, proposal["id"])

    assert gating.failed(run) == {"held_out"}
    named = {line.split(",")[0] for line in failures(run)}
    assert named == {"New-rule case made-custody-b-partial", "New-rule case made-custody-a-failed"}
    assert all("hold_report differs" in line for line in failures(run))


def test_a_regression_case_the_playbooks_in_use_break_fails_the_gate(make_layer):
    layer = scripted(make_layer, gating.honest())
    made = held_out_cases.changed(
        held_out_cases.FIRST_CYCLE, "made-pas-b-timeout", [held_out_cases.FEED_B]
    )
    held_out_cases.write(layer.settings.cases, made)
    gating.first_cycle(layer)

    run = gating.gated(layer, drafted(layer)["id"])

    assert failures(run)[0] == (
        "Regression case made-pas-b-timeout, exception e1: outcome, playbook, tell, by, "
        "hold_report differ from its expected note with the playbooks in use."
    )


def test_grading_calls_no_model(make_layer):
    layer, proposal = feed_only(make_layer, choosing=lambda: choosing())

    run = gating.gated(layer, proposal["id"])

    assert run["outcome"] == "passed", failures(run)
    asked = [prompt for stub in layer.stubs for prompt in stub.prompts]
    choices = [
        step
        for trial in run["held_out"]["trials"]
        for step in trial["steps"]
        if step["tool"] in ("choose_step", "no_step_applies")
    ]
    # Every model call was a trial's choice of step, and nothing else asked a model.
    assert len(asked) == len(choices) == 27
    assert {prompt[0].content for prompt in asked} == {harness.INSTRUCTIONS}
    for path in [ROOT / "backend" / "cases.py", *(ROOT / "backend" / "gate").glob("*.py")]:
        text = path.read_text()
        assert "langchain" not in text and "modelkit" not in text, path


# The trials


def test_every_case_runs_three_times_on_the_triage_agent(make_layer):
    layer, proposal = feed_only(make_layer, choosing=lambda: choosing())

    run = gating.gated(layer, proposal["id"])

    held = run["held_out"]
    assert held["trial_model"] == "stub:triage" and held["models"] == ["stub:triage"]
    trials_run = [(t["case"], t["trial"]) for t in held["trials"]]
    names = [case.id for case in cases.read_set(layer.settings.cases, DESKS).cases]
    assert trials_run == [(name, number) for name in names for number in (1, 2, 3)]
    assert {t["state"] for t in held["trials"]} == {"completed"}
    (with_conflict,) = [t for t in held["trials"] if t["case"].endswith("conflict")][:1]
    assert with_conflict["notes"] == [
        {"exception": "e1", "state": "chosen", "step": "feed-outage/custodian-b-feed-failed"},
        {"exception": "e2", "state": "no_step", "step": None},
    ]


def test_one_wrong_trial_fails_the_check(make_layer):
    made = iter(range(1000))
    # The fifth model asked, the second case's second trial, answers wrong twice.
    layer, proposal = feed_only(
        make_layer, choosing=lambda: choosing(wrong=2 if next(made) == 4 else 0)
    )

    run = gating.gated(layer, proposal["id"])

    assert run["outcome"] == "failed" and gating.failed(run) == {"held_out"}
    assert failures(run) == [
        "Regression case made-custody-b-partial, trial 2 of 3, exception e1: outcome, "
        "playbook, tell, by, hold_report differ from its expected note."
    ]
    missed = [t for t in run["held_out"]["trials"] if t["misses"]]
    assert [(t["case"], t["trial"]) for t in missed] == [("made-custody-b-partial", 2)]
    (note,) = missed[0]["notes"]
    assert note["state"] == "unverified"
    refused = [s["line"] for s in missed[0]["steps"] if s["outcome"] == "refused"]
    assert refused == ["A step applies to this exception, so 'no step' is refused."] * 2
    assert state_of(layer, proposal["id"])["state"] == "gate_failed"


def test_a_choice_refused_once_and_then_right_passes(make_layer):
    made = iter(range(1000))
    layer, proposal = feed_only(
        make_layer, choosing=lambda: choosing(wrong=1 if next(made) == 0 else 0)
    )

    run = gating.gated(layer, proposal["id"])

    assert run["outcome"] == "passed", failures(run)
    first = run["held_out"]["trials"][0]
    assert [s["outcome"] for s in first["steps"]] == ["refused", "accepted"]


def test_with_no_model_for_the_trials_the_gate_does_not_run(make_layer):
    layer, proposal = first_cycle(make_layer, overrides={"triage_model": ""})
    lines = len(knowledge.ledger(layer))

    response = gating.gate(layer, proposal["id"])

    assert response.status_code == 503
    assert response.json() == {
        "detail": "No model is configured for the held-out trials: set TRIAGE_MODEL. "
        "The gate did not run."
    }
    found = state_of(layer, proposal["id"])
    assert found["state"] == "drafted" and found["gate_runs"] == []
    assert [(r["reason"], r["line"]) for r in found["gate_refusals"]] == [
        ("no_trial_model", response.json()["detail"])
    ]
    assert knowledge.events(layer)[lines:] == ["gate_refused"]
    assert {each["state"] for each in knowledge.gap_reports(layer)} == {"cited"}


@pytest.mark.parametrize(
    ("named", "reason", "whose"),
    [
        (
            {"overrides": {"triage_model": "google_genai:gemini-4-neon-xhigh"}},
            "trial_model",
            "The model for the held-out trials is",
        ),
        (
            {"curation_model": "stub:gemini-4-neon"},
            "curator_model",
            "The proposal's curator ran on",
        ),
    ],
)
def test_the_writer_of_the_cases_never_runs_them(make_layer, named, reason, whose):
    layer, proposal = first_cycle(make_layer, **named)
    held_out_cases.write(layer.settings.cases, writer=NEON)
    lines = len(knowledge.ledger(layer))

    response = gating.gate(layer, proposal["id"])

    assert response.status_code == 409
    assert response.json() == {
        "detail": f"{whose} Gemini 4 Neon, which wrote the cases' expected notes. "
        "A test is never run by the model that wrote its answers."
    }
    found = state_of(layer, proposal["id"])
    assert found["state"] == "drafted" and found["gate_runs"] == []
    assert [r["reason"] for r in found["gate_refusals"]] == [f"{reason}_is_writer"]
    assert knowledge.events(layer)[lines:] == ["gate_refused"]


def counting(model: str, **options):
    """A trial agent configured with `model`, which counts the trials it is asked."""
    asked: list[dict] = []

    def made(url: str, key: str):
        playing = stand_ins.trial_agent(url, key, model=model, **options)
        app = FastAPI()

        @app.post("/invocations")
        def invocations(sent: dict) -> JSONResponse:
            if sent.get("kind") == "trial":
                asked.append(sent)
            with TestClient(playing) as client:
                return JSONResponse(client.post("/invocations", json=sent).json())

        return app

    return made, asked


def test_an_agent_configured_as_the_writer_runs_no_trial(make_layer):
    agent, asked = counting("google_genai:gemini-4-neon")
    layer = make_layer(agent=agent, curates=lambda: [call("submit_proposal", **gating.honest())])
    held_out_cases.write(layer.settings.cases, writer=NEON)
    gating.first_cycle(layer)
    proposal = drafted(layer)
    lines = len(knowledge.ledger(layer))

    response = gating.gate(layer, proposal["id"])

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "The triage agent is configured with another model than TRIAGE_MODEL names, so no "
        "trial ran and the gate records no result."
    )
    # It was asked once, with the model the gate records, and refused before any call.
    assert [(each["model"], each["cap_usd"]) for each in asked] == [("stub:triage", "10.000000")]
    assert knowledge.events(layer)[lines:] == ["gate_refused"]
    (refused,) = state_of(layer, proposal["id"])["gate_refusals"]
    assert refused["reason"] == "agent_model_differs"


def test_a_trial_whose_calls_name_another_model_is_refused(make_layer):
    # The agent's key names the model on each call: one that names another is no result.
    for named in ("stub:another", "google_genai:gemini-4-neon"):
        agent, _ = counting("stub:triage", answers_as=named)
        layer = make_layer(
            agent=agent, curates=lambda: [call("submit_proposal", **gating.honest())]
        )
        held_out_cases.write(layer.settings.cases, writer=NEON)
        gating.first_cycle(layer)
        proposal = drafted(layer)

        response = gating.gate(layer, proposal["id"])

        assert response.status_code == 409
        reason = "other_model_answered" if named == "stub:another" else "trial_model_is_writer"
        (refused,) = state_of(layer, proposal["id"])["gate_refusals"]
        assert refused["reason"] == reason
        line = knowledge.ledger(layer)[-1]["data"]
        assert line["case_set_sha256"] == cases.read_set(layer.settings.cases, DESKS).sha256
        # The trials it ran are each a line of their own, under the refused gate run's ID.
        trials = [e for e in knowledge.ledger(layer) if e["event"] == "held_out_trial"]
        assert len(trials) == 24
        assert {t["data"]["gate_run_id"] for t in trials} == {line["gate_run_id"]}


def test_a_trial_cannot_file_a_gap_report_and_none_is_stored(make_layer):
    answered = []
    usage = {"model": "stub:triage", "input_tokens": 1, "output_tokens": 1, "cost_usd": None}

    def asks_about_a_gap(http, note_path: str, _: dict) -> None:
        for tool, body in (
            ("gap-report", {"turn": 1, "summary": "A rule is missing.", "usage": usage}),
            ("no-gap-report", {"turn": 1, "usage": usage}),
        ):
            sent = http.post(f"{note_path}/{tool}", json=body)
            answered.append((sent.status_code, sent.json()))

    layer = make_layer(
        agent=lambda url, key: stand_ins.trial_agent(url, key, before=asks_about_a_gap),
        curates=lambda: [call("submit_proposal", **gating.honest())],
    )
    gating.first_cycle(layer)
    proposal = drafted(layer)
    reports = knowledge.gap_reports(layer)
    lines = len(knowledge.ledger(layer))

    run = gating.gated(layer, proposal["id"])

    assert run["outcome"] == "passed"
    assert answered and set(map(json.dumps, answered)) == {
        json.dumps([409, {"detail": trials.NO_GAP_REPORT}])
    }
    assert knowledge.gap_reports(layer) == reports
    assert knowledge.events(layer)[lines:] == ["held_out_trial"] * 24 + ["gate_run"]
    steps = [s for t in run["held_out"]["trials"] for s in t["steps"]]
    assert {s["tool"] for s in steps if s["outcome"] == "refused"} == {
        "file_gap_report",
        "no_gap_report",
    }
    assert "A rule is missing." not in json.dumps(knowledge.ledger(layer))


def test_trials_are_never_written_to_the_triage_log(make_layer):
    layer, proposal = first_cycle(make_layer)
    log = layer.settings.triage_log.read_bytes()
    runs = layer.as_analyst("GET", "/").text.count("/triage-runs/")

    gating.gated(layer, proposal["id"])

    assert layer.settings.triage_log.read_bytes() == log
    assert layer.as_analyst("GET", "/").text.count("/triage-runs/") == runs


def test_a_trial_nothing_is_heard_from_fails_the_check(make_layer, monkeypatch):
    monkeypatch.setattr(held_out, "STALLED_SECONDS", 0.3)
    layer, proposal = first_cycle(make_layer)
    # The same layer, started again with an agent that says it started and never works.
    quiet = make_layer(
        after=layer,
        agent=lambda *_: stand_ins.idle_agent(),
        curates=lambda: [],
    )

    run = gating.gated(quiet, proposal["id"])

    assert run["outcome"] == "failed"
    assert failures(run)[0] == (
        "Regression case made-crm-unavailable, trial 1 of 3: it ended failed (nothing was heard "
        "from it), so it gave no notes to grade."
    )
    # A case with no step for its warnings asks no model, and is not waited on.
    ended = {(t["case"], t["state"], t["failure"]) for t in run["held_out"]["trials"]}
    assert ("made-price-stale", "completed", None) in ended
    assert {(state, failure) for _, state, failure in ended} == {
        ("failed", "stalled"),
        ("completed", None),
    }


def test_an_agent_that_cannot_be_reached_stops_the_gate(make_layer):
    layer, proposal = first_cycle(make_layer)
    down = make_layer(after=layer, agent_url="http://127.0.0.1:9", curates=lambda: [])

    response = gating.gate(down, proposal["id"])

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "The triage agent cannot be reached. The gate did not finish, and records no result."
    )
    found = state_of(down, proposal["id"])
    assert found["state"] == "drafted"
    assert [r["reason"] for r in found["gate_refusals"]] == ["agent_did_not_start"]


def test_the_playbooks_changing_while_the_gate_runs_records_no_result(make_layer):
    retired = []

    def retiring(url: str, key: str):
        def choose(note: dict) -> dict:
            if not retired:
                path = "/api/playbooks/missing-cross-check/versions/1/retire"
                with stand_ins.httpx2.Client(base_url=url) as http:
                    sent = http.post(
                        path,
                        json={"reason": "wrong_action"},
                        headers={"Authorization": f"Bearer {OWNER}"},
                    )
                retired.append(sent.status_code)
            return stand_ins.fitting(note)

        return stand_ins.trial_agent(url, key, choose=choose)

    layer = make_layer(agent=retiring, curates=lambda: [call("submit_proposal", **gating.honest())])
    gating.first_cycle(layer)
    proposal = drafted(layer)

    response = gating.gate(layer, proposal["id"])

    assert retired == [200]
    assert response.status_code == 409
    assert response.json()["detail"].startswith("The playbooks in use changed while the gate ran")
    found = state_of(layer, proposal["id"])
    assert found["state"] == "drafted" and found["gate_runs"] == []


def test_each_trial_is_given_what_is_left_of_the_cap(make_layer):
    agent, asked = counting("stub:triage", cost="0.400000")
    layer = make_layer(
        agent=agent,
        curates=lambda: [call("submit_proposal", **gating.honest())],
        overrides={"gate_max_dollars": "1"},
    )
    gating.first_cycle(layer)

    run = gating.gated(layer, drafted(layer)["id"])

    assert [each["cap_usd"] for each in asked] == ["1.000000", "0.600000", "0.200000"]
    # This stand-in spends what it was not given. The gate stops at once, and fails.
    assert run["outcome"] == "failed"
    assert failures(run) == [
        "The trials passed the gate's cap of $1 at case made-crm-unavailable, trial 3, and stopped."
    ]
    assert len(run["held_out"]["trials"]) == 3 and run["held_out"]["cost_usd"] == "1.200000"


def test_the_triage_agent_spends_no_more_than_the_gate_leaves_it(make_layer):
    priced = Price(Decimal(4), Decimal(20))
    layer, proposal = feed_only(
        make_layer,
        choosing=lambda: choosing(),
        price=priced,
        overrides={"gate_max_dollars": "0.000001"},
    )

    run = gating.gated(layer, proposal["id"])

    assert [stub.prompts for stub in layer.stubs] == [[]]  # one trial, and no model was asked
    first = run["held_out"]["trials"][0]
    assert (first["state"], first["failure"]) == ("failed", "cost_cap")
    assert failures(run)[-1] == (
        "The trials stopped at case made-custody-a-failed, trial 1: its next model call could "
        "pass what was left of the gate's cap of $0.000001."
    )
    assert len(run["held_out"]["trials"]) == 1


def test_a_second_gate_on_a_proposal_being_gated_is_refused(make_layer, monkeypatch):
    monkeypatch.setattr(held_out, "STALLED_SECONDS", 0.4)
    layer, proposal = first_cycle(make_layer)
    quiet = make_layer(after=layer, agent=lambda *_: stand_ins.idle_agent(), curates=lambda: [])
    first = []
    running = threading.Thread(target=lambda: first.append(gating.gate(quiet, proposal["id"])))
    running.start()
    time.sleep(0.2)

    second = gating.gate(quiet, proposal["id"])
    running.join(60)

    assert second.status_code == 409
    assert second.json() == {"detail": "The gate is already running on that proposal."}
    assert first[0].status_code == 200


# The committed cases and the bundle


def committed_cases(layer) -> list[Path]:
    """The repository's case files, copied into the layer's cases, and the files that grade."""
    shutil.rmtree(layer.settings.cases)
    shutil.copytree(ROOT / "cases", layer.settings.cases)
    files = sorted(p for p in layer.settings.cases.rglob("*") if p.is_file())
    graders = [
        p
        for path in bundle.GRADING
        for p in ([path] if path.is_file() else sorted(path.glob("*.py")))
    ]
    return files + graders


def test_each_committed_case_file_among_the_sources_stops_the_curator(make_layer, monkeypatch):
    layer = make_layer(agent=lambda *_: stand_ins.idle_agent(), curates=lambda: [call("no_change")])
    sealed = committed_cases(layer)
    kinds = {
        "case set": [p for p in sealed if p.name == "case-set.yaml"],
        "regression input": [
            p for p in sealed if p.parent.name == "regression" and p.suffix == ".yaml"
        ][:1],
        "regression notes": [
            p for p in sealed if p.parent.name == "regression" and p.suffix == ".json"
        ],
        "new-rule input": [
            p for p in sealed if p.parent.name == "new-rule" and p.suffix == ".yaml"
        ][:1],
        "new-rule notes": [
            p for p in sealed if p.parent.name == "new-rule" and p.suffix == ".json"
        ],
        "hostile": [p for p in sealed if p.parent.name == "hostile"][:1],
        "cases.py": [ROOT / "backend" / "cases.py"],
        "held_out.py": [ROOT / "backend" / "gate" / "held_out.py"],
        "matching.py": [ROOT / "backend" / "matching.py"],
    }
    assert all(len(found) == 1 for found in kinds.values())
    assembled = bundle.assemble
    for number, (_, (path,)) in enumerate(kinds.items()):
        planted = {"kind": "memo", "desk": "pricing", "scope": "none", "date": "2026-10-30"}
        item = bundle._item(
            bundle.FIRM_SOURCE,
            f"memo-planted-{'abcdefghijk'[number]}",
            planted | {"text": path.read_text()},
        )

        def with_one_planted(connection, store, item=item):
            made = assembled(connection, store)
            items = [*made.items, item]
            return bundle.Bundle(items, made.sha256, made.gap_report_ids)

        monkeypatch.setattr(bundle, "assemble", with_one_planted)
        response = curation.curate(layer)
        assert response.status_code == 500, path.name
        assert "is a held-out case or a file that grades them" in response.json()["detail"]
        assert layer.curator_stubs == []
    monkeypatch.setattr(bundle, "assemble", assembled)


def test_a_normal_runs_manifest_matches_no_committed_case_file(make_layer):
    layer = make_layer(agent=gating.trial_agent, curator=curation.idle)
    sealed = committed_cases(layer)
    curation.first_cycle(layer)

    run_id = curation.started(layer)

    listed = curation.read(layer, run_id)["bundle"]
    hashes = {each["sha256"] for each in listed["items"]} | {listed["sha256"]}
    assert len(sealed) > 20
    assert not hashes & {sha256(path.read_bytes()).hexdigest() for path in sealed}
    assert not hashes & {
        sha256(" ".join(path.read_text().split()).encode()).hexdigest() for path in sealed
    }
    assert firm.ROOT == ROOT


def test_trials_run_before_the_agent_stops_starting_are_recorded_with_the_refusal(make_layer):
    asked = []

    def tiring(url: str, key: str):
        playing = stand_ins.trial_agent(url, key)
        app = FastAPI()

        @app.post("/invocations")
        def invocations(sent: dict) -> JSONResponse:
            asked.append(sent)
            if sum(each.get("kind") == "trial" for each in asked) > 6:
                return JSONResponse({"refused": "no_model"})
            with TestClient(playing) as client:
                return JSONResponse(client.post("/invocations", json=sent).json())

        return app

    layer = make_layer(agent=tiring, curates=lambda: [call("submit_proposal", **gating.honest())])
    gating.first_cycle(layer)
    proposal = drafted(layer)
    lines = len(knowledge.ledger(layer))

    response = gating.gate(layer, proposal["id"])

    assert response.status_code == 503
    assert response.json()["detail"].startswith("The triage agent has no model")
    written = knowledge.ledger(layer)[lines:]
    assert [e["event"] for e in written] == ["held_out_trial"] * 6 + ["gate_refused"]
    refused = written[-1]["data"]
    assert refused["reason"] == "agent_did_not_start"
    assert {e["data"]["gate_run_id"] for e in written[:-1]} == {refused["gate_run_id"]}
    assert refused["trial_model"] == "stub:triage" and len(refused["case_set_sha256"]) == 64
    # They missed nothing, so the next gate runs them all again.
    assert state_of(layer, proposal["id"])["state"] == "drafted"


def test_a_trial_that_missed_fails_the_gate_even_when_the_agent_then_stops(make_layer):
    asked = []

    def failing_once(url: str, key: str):
        failing = stand_ins.trial_agent(url, key, fails=True)
        app = FastAPI()

        @app.post("/invocations")
        def invocations(sent: dict) -> JSONResponse:
            if sent.get("kind") == "trial":
                asked.append(sent)
                if len(asked) > 1:
                    return JSONResponse({"refused": "no_model"})
            with TestClient(failing) as client:
                return JSONResponse(client.post("/invocations", json=sent).json())

        return app

    layer = make_layer(
        agent=failing_once, curates=lambda: [call("submit_proposal", **gating.honest())]
    )
    gating.first_cycle(layer)
    proposal = drafted(layer)

    run = gating.gated(layer, proposal["id"])

    assert run["outcome"] == "failed"
    assert failures(run)[0].startswith(
        "Regression case made-crm-unavailable, trial 1 of 3: it ended"
    )
    assert failures(run)[-1].startswith("The trials stopped: The triage agent has no model")
    assert state_of(layer, proposal["id"])["state"] == "gate_failed"


# What the curator run of a proposal ran on


def idle_curated(make_layer, **options):
    layer = make_layer(agent=gating.trial_agent, curator=curation.idle, **options)
    held_out_cases.write(layer.settings.cases, writer=NEON)
    gating.first_cycle(layer)
    return layer


def test_a_curator_call_that_names_the_writer_is_refused_at_the_gate(make_layer):
    layer = idle_curated(make_layer)
    run_id = curation.started(layer)
    usage = curation.USAGE | {"model": "google_genai:gemini-4-neon"}
    proposal = curation.propose(layer, run_id, gating.honest(), usage=usage).json()

    response = gating.gate(layer, proposal["id"])

    assert response.status_code == 409
    assert response.json()["detail"].startswith("The proposal's curator ran on Gemini 4 Neon")
    assert state_of(layer, proposal["id"])["state"] == "drafted"


def test_a_curator_call_that_names_another_model_is_refused_at_the_gate(make_layer):
    layer = idle_curated(make_layer)
    run_id = curation.started(layer)
    usage = curation.USAGE | {"model": "stub:another"}
    proposal = curation.propose(layer, run_id, gating.honest(), usage=usage).json()

    response = gating.gate(layer, proposal["id"])

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "A call of the proposal's curator run named another model than the one the run "
        "recorded, so the gate does not run."
    )


def test_a_curator_on_the_cases_writer_is_refused_before_it_is_asked(make_layer):
    layer = idle_curated(make_layer, curation_model="stub:gemini-4-neon")
    lines = len(knowledge.ledger(layer))

    response = curation.curate(layer)

    assert response.status_code == 409
    assert response.json()["detail"].startswith("CURATOR_MODEL is Gemini 4 Neon, which wrote")
    assert len(knowledge.ledger(layer)) == lines


def test_a_curator_run_once_started_on_the_writer_is_refused_whatever_ran_it_last(
    make_layer, monkeypatch
):
    later = curation.later(monkeypatch)
    layer = idle_curated(make_layer, curation_model="stub:gemini-4-neon")
    # Started while the case set, which names its writer, is not in the folder.
    (layer.settings.cases / "case-set.yaml").rename(layer.settings.cases / "set.aside")
    run_id = curation.started(layer)
    (layer.settings.cases / "set.aside").rename(layer.settings.cases / "case-set.yaml")
    later(minutes=5)
    again = make_layer(after=layer, agent=gating.trial_agent, curator=curation.idle)
    assert curation.started(again) == run_id
    proposal = curation.propose(again, run_id, gating.honest(), attempt=2).json()
    assert curation.read(again, run_id)["model"] == "stub:curation"

    response = gating.gate(again, proposal["id"])

    assert response.status_code == 409
    assert response.json()["detail"].startswith("The proposal's curator ran on Gemini 4 Neon")


# Settings the gate cannot use


@pytest.mark.parametrize(
    ("setting", "status", "reason"),
    [
        ("google_genai:gemini-4-neon-20261122", 409, "trial_model_is_writer"),
        ("gemini-4-neon", 409, "trial_model_is_writer"),
        ("a model", 503, "trial_model_unusable"),
    ],
)
def test_a_trials_model_that_is_no_models_name_is_refused(make_layer, setting, status, reason):
    layer, proposal = first_cycle(make_layer, overrides={"triage_model": setting})
    held_out_cases.write(layer.settings.cases, writer=NEON)

    response = gating.gate(layer, proposal["id"])

    assert response.status_code == status
    assert [r["reason"] for r in state_of(layer, proposal["id"])["gate_refusals"]] == [reason]


@pytest.mark.parametrize("cap", ["NaN", "Infinity", "0", "-1"])
def test_a_gate_cap_that_is_no_amount_above_zero_stops_the_backend(make_layer, cap):
    with pytest.raises(ValueError, match="GATE_MAX_DOLLARS"):
        make_layer(overrides={"gate_max_dollars": cap})


def test_a_gate_run_on_unapproved_playbooks_gives_out_no_action(make_layer, tmp_path):
    seen = []
    layer = make_layer(
        playbooks=firm.unapproved(firm.copy(tmp_path / "unapproved")),
        agent=lambda url, key: stand_ins.trial_agent(
            url, key, before=lambda _, __, note: seen.append(json.dumps(note))
        ),
        curates=lambda: [call("submit_proposal", **gating.honest())],
    )
    gating.first_cycle(layer)

    run = gating.gated(layer, drafted(layer)["id"])

    assert run["outcome"] == "passed" and seen
    for shown in (json.dumps(run), *seen):
        for action in (
            "same_day 20:00 ET",
            "Portfolio Accounting Operations",
            "custody-operations",
        ):
            assert action not in shown


# What is kept when a gate's trials are cut short


def test_a_trials_end_is_in_the_ledger_before_the_agent_is_told(make_layer, monkeypatch):
    ending, answered = [], []
    holder = {}
    complete = trials.Trials.complete

    def kept() -> list[tuple[str, int]]:
        lines = (holder["layer"].settings.playbooks / "ledger.jsonl").read_text().splitlines()
        ended = [json.loads(line)["data"]["trial"] for line in lines if '"held_out_trial"' in line]
        return [(trial["case"], trial["trial"]) for trial in ended]

    def completing(self, run_id: str, attempt: int):
        trial = self._trials[run_id]
        ending.append(run_id)
        answer = complete(self, run_id, attempt)
        # Still in the backend: the agent has not been answered yet.
        answered.append((trial.case, trial.number) in kept())
        return answer

    monkeypatch.setattr(trials.Trials, "complete", completing)
    layer = make_layer(
        agent=stand_ins.trial_agent,
        curates=lambda: [call("submit_proposal", **gating.honest())],
    )
    holder["layer"] = layer
    gating.first_cycle(layer)

    gating.gated(layer, drafted(layer)["id"])

    # The gate can end while the last trial's answer is still on its way: wait for each.
    until = time.monotonic() + 15
    while len(answered) < len(ending):
        assert time.monotonic() < until, "a trial's end was not answered"
        time.sleep(0.02)
    # Each trial the agent played was a line of its own before its end was answered.
    assert answered and all(answered)


def test_a_miss_fails_the_gate_whatever_then_stops_the_trials(make_layer):
    asked = []

    def then_another_model(url: str, key: str):
        failing = stand_ins.trial_agent(url, key, fails=True)
        app = FastAPI()

        @app.post("/invocations")
        def invocations(sent: dict) -> JSONResponse:
            if sent.get("kind") == "trial":
                asked.append(sent)
                if len(asked) > 1:
                    return JSONResponse({"refused": "other_model"})
            with TestClient(failing) as client:
                return JSONResponse(client.post("/invocations", json=sent).json())

        return app

    layer = make_layer(
        agent=then_another_model, curates=lambda: [call("submit_proposal", **gating.honest())]
    )
    gating.first_cycle(layer)
    proposal = drafted(layer)

    run = gating.gated(layer, proposal["id"])

    assert run["outcome"] == "failed"
    assert failures(run)[-1].startswith("The trials stopped: The triage agent is configured with")
    assert state_of(layer, proposal["id"])["state"] == "gate_failed"


def test_a_call_whose_cost_is_not_reported_counts_at_the_most_it_could_cost(make_layer):
    layer, proposal = feed_only(
        make_layer,
        choosing=lambda: choosing(unreported=True),
        price=Price(Decimal(4), Decimal(20)),
        overrides={"gate_max_dollars": "1"},
    )

    run = gating.gated(layer, proposal["id"])

    held = run["held_out"]
    assert run["outcome"] == "failed"
    assert Decimal(held["cost_usd"]) <= 1
    assert all(s["cost_usd"] for t in held["trials"] for s in t["steps"])
    assert failures(run)[-1].endswith("could pass what was left of the gate's cap of $1.")
    assert len(held["trials"]) < 24


def test_a_trial_whose_call_gets_no_answer_counts_its_bound_in_the_gates_spend(make_layer):
    from modelkit.prices import PRICES

    layer, proposal = feed_only(
        make_layer,
        replies=lambda: [RuntimeError("no answer")],
        price=PRICES["openai:gpt-6-luna"],
    )

    run = gating.gated(layer, proposal["id"])

    held = run["held_out"]
    missed = [t for t in held["trials"] if t["failure"] == "model_failed"]
    assert missed, "a trial with no answer is a miss"
    lost = [s for t in missed for s in t["steps"] if s["cost_usd"]]
    assert (
        lost
        and Decimal(held["cost_usd"]) == sum(Decimal(t["cost_usd"]) for t in held["trials"]) > 0
    )
