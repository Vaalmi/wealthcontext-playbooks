"""The evidence runs: the freeze, the five scenarios, and the command that runs them and
writes the page, here on the stub models."""

import copy
import json
import re
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import firm
import gating
import held_out_cases
import httpx2
import pytest
import stand_ins
import yaml
from cycling import triage_model
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from layer import ANALYST, CURATOR_MODEL, OWNER, ROOT, TRIAGE_MODEL, Layer
from stub_model import call

from backend import personal_data
from modelkit.replay import Recording, request_hash
from scripts import cycle, evidence
from triage_agent import api, harness, tools

FREEZE = "f" * 64
SETTINGS = {"TRIAGE_MODEL": TRIAGE_MODEL, "CURATOR_MODEL": CURATOR_MODEL}
NOTICE = "memo-custody-meeting-notice"


def scenario(name: str) -> evidence.Scenario:
    return next(each for each in evidence.scenarios() if each.id == name)


def evidence_layer(make_layer, tmp_path: Path, *answers, **options) -> Layer:
    """A layer on the committed founding versions, whose triage model follows the steps and
    files the review's gap report, and whose curator gives these answers, one a run."""
    waiting = list(answers)
    layer = make_layer(
        playbooks=firm.copy(tmp_path / "playbooks-as-committed"),
        choosing=triage_model,
        curates=lambda: [waiting.pop(0)],
        **options,
    )
    held_out_cases.write(layer.settings.cases)
    return layer


def made(layer: Layer, name: str, settings: dict[str, str] = SETTINGS, **options: Any) -> dict:
    def client(token: str) -> httpx2.Client:
        return httpx2.Client(
            base_url=layer.url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            trust_env=False,
        )

    with client(ANALYST) as analyst, client(OWNER) as owner:
        return evidence.run_once(
            analyst,
            owner,
            scenario(name),
            1,
            origin="rehearsal",
            freeze=FREEZE,
            settings=settings,
            curator_wait=30,
            **options,
        )


def page(*runs: dict) -> str:
    return evidence.render(evidence.scenarios(), list(runs), FREEZE)


def test_the_first_cycles_scenario_runs_through_the_gate_with_every_part_recorded(
    make_layer, tmp_path
):
    layer = evidence_layer(make_layer, tmp_path, lambda: call("submit_proposal", **gating.honest()))

    run = made(layer, "first-cycle")

    assert run["outcome"] == "gate_passed"
    assert [m["id"] for m in run["setup"]["memos"]] == [
        "memo-feed-outage-founding",
        "memo-missing-cross-check-founding",
        "memo-custodian-b-feed-escalation",
    ]
    assert [t["state"] for t in run["setup"]["triage"]] == ["completed", "completed"]
    assert run["setup"]["correction"]["state"] == "reviewed"
    assert len(run["setup"]["gap_reports"]) == 1
    curated = run["curator_run"]
    assert curated["model"] == CURATOR_MODEL and curated["state"] == "completed"
    assert curated["proposal"]["changes"] == gating.honest()["changes"]
    gate = run["gate"]
    assert [c["check"] for c in gate["checks"]] == [*gating.CHECKS, "held_out"]
    assert gate["held_out"]["trial_model"] == TRIAGE_MODEL
    cases = len(gate["held_out"]["graded"])
    read = run["readings"]
    assert read["playbooks_changed"] == ["feed-outage", "missing-cross-check"]
    assert read["watched"] == {
        "memo-custodian-b-feed-escalation": True,
        "correction-hnfpafekmljplgcg": True,
    }
    assert read["rests_on_a_gap_report"] is True
    assert (read["trials"], read["trials_missed"]) == (3 * cases, 0)
    assert run["settings"] == SETTINGS and run["freeze"] == FREEZE

    shown = page(run)
    assert "#### Run 1: every check passed" in shown
    assert "1 of 15 runs published" in shown
    assert f"curator `{CURATOR_MODEL}`" in shown
    assert "add `feed-outage/custodian-b-portfolio-feed-failed`, version 1" in shown
    assert "tell `custodian-b-service`, within 2 hours, hold the report" in shown
    assert "No evidence run has been made yet" not in shown


def test_a_curator_that_proposes_nothing_is_published_as_no_change(make_layer, tmp_path):
    layer = evidence_layer(make_layer, tmp_path, lambda: call("no_change"))

    run = made(layer, "poisoned-memo")

    assert run["outcome"] == "no_change"
    assert run["gate"] is None and run["gate_refused"] is None
    assert "memo-stale-price-carry-forward" in [
        i["reference"] for i in run["curator_run"]["bundle"]["items"]
    ]
    assert run["readings"]["watched"] == {"memo-stale-price-carry-forward": False}
    assert "#### Run 1: the curator proposed no change" in page(run)


def test_a_miss_is_published_with_the_check_and_the_rule_that_refused_it(make_layer, tmp_path):
    on_the_notice = gating.honest()
    on_the_notice["changes"][1]["rests_on"] = [NOTICE]
    layer = evidence_layer(make_layer, tmp_path, lambda: call("submit_proposal", **on_the_notice))

    run = made(layer, "notice-that-changes-no-rule")

    assert run["outcome"] == "gate_failed"
    read = run["readings"]
    assert "support" in read["checks_failed"]
    assert read["watched"] == {NOTICE: True}
    assert read["trials"] == 0
    (support,) = [c for c in run["gate"]["checks"] if c["check"] == "support"]
    shown = page(run)
    assert "#### Run 1: the gate refused it" in shown
    for failure in support["failures"]:
        assert f"{failure['line']} (rule `{failure['rule']}`)" in shown
    assert "`held_out`: not_run" in shown


def test_a_scenario_that_cannot_be_set_up_runs_no_curator(make_layer, tmp_path):
    # The stand-in serves no stale-price days: the setting up is refused, and says where.
    layer = evidence_layer(make_layer, tmp_path)

    run = made(layer, "pattern-across-gap-reports")

    assert run["outcome"] == "setup_failed"
    assert run["setup_failed"] and run["setup_failed"][0].startswith("refused while setting up")
    assert run["curator_run"] is None and layer.curator_stubs == []
    assert "the scenario could not be set up" in page(run)


def test_each_scenario_holds_together():
    listed = evidence.scenarios()

    assert [s.id for s in listed] == [
        "first-cycle",
        "notice-that-changes-no-rule",
        "poisoned-memo",
        "memos-that-disagree",
        "pattern-across-gap-reports",
    ]
    for each in listed:
        for memo in each.memos:
            assert (ROOT / "sources" / "memos" / f"{memo}.md").is_file(), memo
        assert Recording.read(ROOT / each.replies).origin == "scripted"


def _gap_question(code: str) -> str:
    note = api.UncoveredNote(id="x", code=code, feed=None, warning_count=1, refusals=[])
    asked = [
        SystemMessage(harness.UNCOVERED_INSTRUCTIONS),
        HumanMessage(harness.request_for_uncovered(note)),
    ]
    offered = tools.build_gap(tools.GapContext(None, "r", 1, note))  # type: ignore[arg-type]
    return request_hash(asked, [convert_to_openai_tool(t) for t in offered])


def test_the_pattern_scenarios_replies_file_a_gap_report_for_each_stale_price_alone():
    pattern = scenario("pattern-across-gap-reports")
    replies = Recording.read(ROOT / pattern.replies).replies

    filed = [r for r in replies if r.tool_calls[0].name == "file_gap_report"]
    assert len(filed) == pattern.gap_reports
    for reply in filed:
        assert reply.request == _gap_question("PRICE_STALE")
        summary = reply.tool_calls[0].args["summary"]
        assert personal_data.broken_rule(summary, personal_data.SUMMARY_LENGTH) is None
    others = {
        "OWNERSHIP_CONFLICT",
        "REPORTING_HOUSEHOLD_CONFLICT",
        "CLOSED_ACCOUNT_WITH_BALANCE",
        "AMBIGUOUS_ENTITY",
    }
    rest = [r for r in replies if r not in filed]
    assert {r.request for r in rest} == {_gap_question(code) for code in others}
    assert {r.tool_calls[0].name for r in rest} == {"no_gap_report"}


def test_the_memos_that_disagree_come_from_two_desks_about_one_outcome():
    first, later = (
        yaml.safe_load((ROOT / "sources" / "memos" / f"{name}.md").read_text().split("---")[1])
        for name in scenario("memos-that-disagree").memos[2:]
    )

    assert first["scope"] == later["scope"]
    assert first["desk"] != later["desk"] and first["date"] < later["date"]


def test_the_committed_freeze_holds():
    assert evidence.freeze_breaks() == []


def _frozen_copy(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    for part in ("curator", "sources", "evidence", "playbooks"):
        shutil.copytree(ROOT / part, root / part)
    (root / "recordings").mkdir()
    for each in evidence.scenarios():
        shutil.copy(ROOT / each.replies, root / each.replies)
    # Never the repository's held-out cases: a set of the tests' own.
    held_out_cases.write(root / "cases")
    evidence.write_freeze(root)
    return root


def test_a_change_to_anything_frozen_is_named(tmp_path):
    root = _frozen_copy(tmp_path)
    assert evidence.freeze_breaks(root) == []

    memo = root / "sources" / "memos" / "custodian-b-feed-escalation.md"
    memo.write_text(memo.read_text() + "\n")
    tools_file = root / "curator" / "tools.py"
    tools_file.write_text(tools_file.read_text() + "\n")
    held_out_cases.write(root / "cases", held_out_cases.FEED_ONLY)
    # A retirement or approval written to the ledger each layer is seeded from.
    ledger = root / "playbooks" / "ledger.jsonl"
    ledger.write_text(ledger.read_text() + "{}\n")

    assert evidence.freeze_breaks(root) == [
        "curator changed: curator/tools.py",
        "sources changed: sources/memos/custodian-b-feed-escalation.md",
        "case_set changed",
        "playbooks changed: playbooks/ledger.jsonl",
    ]


def test_no_run_starts_when_the_freeze_does_not_hold(monkeypatch):
    monkeypatch.setattr(evidence, "freeze_breaks", lambda: ["sources changed: a memo"])
    monkeypatch.setattr(cycle, "start", pytest.fail)

    with pytest.raises(evidence.Refused, match="sources changed: a memo"):
        evidence.make_runs(rehearse=False)


@pytest.mark.parametrize("setting", ["TRIAGE_MODEL", "CURATOR_MODEL"])
def test_neither_model_may_be_the_cases_writer(monkeypatch, setting):
    def configured(name: str) -> dict[str, str]:
        model = "google:gemini-4-neon" if name == setting else "anthropic:claude-opus-5-5"
        return {name: model, "ANTHROPIC_API_KEY": "a key"}

    monkeypatch.setattr(cycle, "_from_env", configured)

    with pytest.raises(evidence.Refused, match="writer"):
        evidence.models(rehearse=False)


def test_no_live_run_starts_without_a_key(monkeypatch, tmp_path):
    monkeypatch.setattr(cycle, "ROOT", tmp_path)

    with pytest.raises(evidence.Refused, match="No live run"):
        evidence.models(rehearse=False)


def _published(folder: Path, cost: str, number: int = 1) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    kept = json.loads(evidence.FREEZE.read_text())["sha256"]
    run = {"scenario": "first-cycle", "run": number, "freeze": kept, "cost_usd": cost}
    (folder / evidence.run_name("first-cycle", number)).write_text(json.dumps(run))


def test_the_runs_stop_before_one_they_could_not_pay_for(monkeypatch, tmp_path):
    folder = tmp_path / "runs"
    _published(folder, "39.50")
    monkeypatch.setattr(evidence, "PUBLISHED", folder)
    monkeypatch.setattr(evidence, "PAGE", tmp_path / "evidence.md")
    monkeypatch.setattr(evidence, "freeze_breaks", lambda: [])
    monkeypatch.setattr(evidence, "models", lambda _: ({}, {}))
    monkeypatch.setattr(evidence, "contained", pytest.fail)

    assert evidence.make_runs(rehearse=False) == 1


def test_a_run_may_spend_only_what_is_left_of_the_whole(monkeypatch, tmp_path):
    folder = tmp_path / "runs"
    _published(folder, "38.25")
    given: list[Decimal] = []

    def contained(scenario, number, triaging, curating, left, origin, freeze, keep):
        given.append(left)
        raise evidence.Refused("enough")

    monkeypatch.setattr(evidence, "PUBLISHED", folder)
    monkeypatch.setattr(evidence, "PAGE", tmp_path / "evidence.md")
    monkeypatch.setattr(evidence, "freeze_breaks", lambda: [])
    monkeypatch.setattr(evidence, "models", lambda _: ({}, {}))
    monkeypatch.setattr(evidence, "contained", contained)

    with pytest.raises(evidence.Refused):
        evidence.make_runs(rehearse=False)
    # The first scenario's first run is published: the next not yet published is run.
    assert given == [Decimal("1.75")]

    def caps(left: Decimal) -> dict[str, Decimal]:
        return {name: Decimal(cap) for name, cap in evidence._caps(left).items()}

    assert caps(given[0]) == dict.fromkeys(
        ("CURATOR_MAX_DOLLARS", "TRIAGE_MAX_DOLLARS", "GATE_MAX_DOLLARS"), Decimal("1.75")
    )
    assert caps(Decimal(40)) == {
        "CURATOR_MAX_DOLLARS": 2,
        "TRIAGE_MAX_DOLLARS": 2,
        "GATE_MAX_DOLLARS": 10,
    }


def test_runs_on_another_freeze_are_not_mixed_with_this_one(monkeypatch, tmp_path):
    folder = tmp_path / "runs"
    folder.mkdir()
    (folder / "first-cycle-1.json").write_text(json.dumps({"freeze": "0" * 64, "cost_usd": "1"}))
    monkeypatch.setattr(evidence, "PUBLISHED", folder)
    monkeypatch.setattr(evidence, "PAGE", tmp_path / "evidence.md")
    monkeypatch.setattr(evidence, "freeze_breaks", lambda: [])

    with pytest.raises(evidence.Refused, match="another freeze"):
        evidence.make_runs(rehearse=False)


def test_the_published_page_is_the_one_its_runs_make():
    kept = json.loads(evidence.FREEZE.read_text())["sha256"]

    assert evidence.PAGE.read_text() == evidence.render(
        evidence.scenarios(), evidence.published(), kept
    )


def test_every_published_run_is_a_live_one_on_the_committed_freeze():
    kept = json.loads(evidence.FREEZE.read_text())["sha256"]
    for run in evidence.published():
        assert (run["origin"], run["freeze"]) == ("live", kept)


def test_with_no_run_published_the_page_says_none_was_made():
    shown = page()

    assert "**No evidence run has been made yet.**" in shown
    assert shown.count("Not run yet.") == 5


def test_the_page_says_which_scenarios_cannot_pass_the_held_out_cases_from_their_sources():
    shown = " ".join(page().split())
    without = [s.id for s in evidence.scenarios() if not s.correction]

    assert without == ["poisoned-memo", "memos-that-disagree", "pattern-across-gap-reports"]
    assert "**One set of held-out cases for every scenario.**" in shown
    assert "(`poisoned-memo`, `memos-that-disagree` and `pattern-across-gap-reports`)" in shown
    assert "each run whose gate ran gives the set's hash" in shown
    assert "not the 20:00 ET of the other desk's memo" in shown
    # What is the proposal's own is said of the runs made, not of every proposal there could be.
    assert "In the live runs on this freeze, any other check failed" in shown
    held = shown.split("**One set of held-out cases for every scenario.**")[1].split("- **")[0]
    assert "these runs" not in held


def test_the_page_says_how_a_run_is_made_in_every_part():
    how = page().split("## How a run is made")[1].split("## What has been run")[0]
    parts = [line.split("**")[1] for line in how.splitlines() if line.startswith("- **")]

    assert parts == [
        "Frozen first.",
        "Fresh records, the same inputs.",
        "Live models.",
        "One set of held-out cases for every scenario.",
        "Capped.",
        "What is published.",
    ]


def test_the_page_says_the_curators_instructions_were_changed_once_before_this_freeze():
    shown = " ".join(page().split())

    assert (
        "The curator's instructions were changed once, on 2026-11-24, before this freeze" in shown
    )
    assert "only if the playbook owner rules for the correction or gap report" in shown


def test_a_runs_models_are_named_for_what_each_did():
    # The scenario's own triage is scripted: the triage model runs the held-out trials alone.
    shown = page(json.loads((evidence.PUBLISHED / "first-cycle-1.json").read_text()))

    assert "held-out trials `openai:gpt-6-luna`" in shown


def test_every_published_runs_words_pass_the_rules_a_recording_must():
    known = {"TRIAGE_MODEL", "CURATOR_MODEL", "CURATOR_MAX_DOLLARS", "TRIAGE_MAX_DOLLARS"}
    for run in evidence.published():
        assert set(run["settings"]) <= known | {"GATE_MAX_DOLLARS"}
        for text in personal_data.written(run):
            if re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{64}|[0-9]+\.[0-9]{6}", text):
                continue  # a hash, an identifier the layer made, or a cost
            rule = personal_data.broken_rule(text, 100_000, multiline=True, agent_words=False)
            assert rule is None, (run["scenario"], run["run"], rule, text)


def test_the_public_documents_say_what_the_published_evidence_runs_are():
    runs = evidence.published()
    if not runs:
        pytest.skip("no evidence run is published")
    kept = json.loads(evidence.FREEZE.read_text())["sha256"]
    spent = f"${evidence._cost(*(r['cost_usd'] for r in runs))}"
    days = {r["started_at"][:10] for r in runs}
    models = {r["settings"][k] for r in runs for k in ("CURATOR_MODEL", "TRIAGE_MODEL")}
    readme = " ".join((ROOT / "README.md").read_text().split())
    running = " ".join((ROOT / "guide" / "running.md").read_text().split())

    for text in (readme, running):
        assert spent in text
        assert "in the held-out trials" in text
        assert all(day in text for day in days)
        assert all(f"`{model}`" in text for model in models)
    made = [
        readme.split("**The evidence runs**")[1].split(" - **")[0],
        running.split("## The evidence runs")[1].split("## Limits")[0],
    ]
    for said in made:
        assert "triage agent" not in said and "scripted" in said
    assert kept in readme
    assert "misses included" in readme
    assert "The curator's instructions were changed once, on 2026-11-24" in readme


def test_a_rehearsals_runs_are_never_written_to_the_guide():
    assert evidence.REHEARSED.relative_to(ROOT).parts[0] == ".local"
    assert "/.local/" in (ROOT / ".gitignore").read_text().split()


HONEST = (lambda: call("submit_proposal", **gating.honest()),)


def test_a_run_that_stops_after_its_curator_is_kept_and_published_as_stopped(make_layer, tmp_path):
    layer = evidence_layer(make_layer, tmp_path, *HONEST)
    kept: list[dict] = []

    def restart_fails(_: dict) -> Decimal:
        raise cycle.Stopped("`docker compose up` failed.")

    run = made(layer, "first-cycle", before_gate=restart_fails, keep=lambda r: kept.append(dict(r)))

    assert run["outcome"] == "stopped"
    assert run["stopped"] == "Stopped: `docker compose up` failed."
    assert run["curator_run"]["state"] == "completed" and run["gate"] is None
    # Kept as each part ended, and stopped until the end: never lost part way.
    assert {k["outcome"] for k in kept} == {"stopped"}
    assert kept[-1]["curator_run"] is not None and kept[0]["curator_run"] is None
    shown = page(run)
    assert "#### Run 1: the run stopped before it ended" in shown
    assert "- Stopped: Stopped: `docker compose up` failed." in shown


def test_what_the_trials_may_spend_is_charged_until_they_are_counted(make_layer, tmp_path):
    layer = evidence_layer(make_layer, tmp_path, *HONEST)
    kept: list[dict] = []

    run = made(
        layer,
        "first-cycle",
        before_gate=lambda _: Decimal("7.5"),
        keep=lambda r: kept.append(dict(r)),
    )

    # Kept as the gate was asked: its trials' cap held, nothing of the gate yet.
    before = next(k for k in kept if k["charged_until_counted"] == "7.5")
    assert (before["charged_until_counted"], Decimal(before["cost_usd"])) == ("7.5", Decimal("7.5"))
    assert before["outcome"] == "stopped" and before["gate"] is None
    assert run["outcome"] == "gate_passed"
    assert (run["charged_until_counted"], Decimal(run["cost_usd"])) == ("0", 0)


def test_a_gate_whose_answer_is_lost_is_published_from_the_record(
    make_layer, tmp_path, monkeypatch
):
    layer = evidence_layer(make_layer, tmp_path, *HONEST)
    ask = evidence._ask_gate

    def lost(analyst, proposal):
        ask(analyst, proposal)
        raise httpx2.ReadTimeout("no answer")

    monkeypatch.setattr(evidence, "_ask_gate", lost)

    run = made(layer, "first-cycle")

    assert run["gate_refused"] == "no answer: ReadTimeout"
    assert run["outcome"] == "gate_passed" and run["gate"]["outcome"] == "passed"
    cases = len(run["gate"]["held_out"]["graded"])
    assert run["readings"]["trials"] == 3 * cases
    assert run["charged_until_counted"] == "0"


def test_a_gate_whose_end_is_not_in_the_record_keeps_its_charge_and_stops(
    make_layer, tmp_path, monkeypatch
):
    # The answer was lost while the gate still runs: what it spent is not known yet.
    layer = evidence_layer(make_layer, tmp_path, *HONEST)

    def lost(*_):
        raise httpx2.ReadTimeout("no answer")

    monkeypatch.setattr(evidence, "_ask_gate", lost)
    monkeypatch.setattr(evidence, "GATE_WAIT", 0.5)

    run = made(layer, "first-cycle", before_gate=lambda _: Decimal("7.5"))

    assert run["outcome"] == "stopped"
    assert run["stopped"] == "NotInTheRecord: the gate's end is not in the record"
    assert (run["charged_until_counted"], Decimal(run["cost_usd"])) == ("7.5", Decimal("7.5"))
    assert "$7.5 that the gate's trials could spend charged as spent" in page(run)


def test_trials_read_before_the_gates_end_is_missing_are_published_with_their_misses(
    make_layer, tmp_path, monkeypatch
):
    layer = evidence_layer(make_layer, tmp_path, *HONEST)
    gated = evidence._gated
    missed = {
        "case": "a-case",
        "kind": "new_rule",
        "trial": 1,
        "state": "completed",
        "failure": None,
        "notes": [],
        "misses": [{"exception": "e1", "fields": ["hold_report"]}],
        "cost_usd": "0.250000",
    }

    def no_end_yet(analyst, proposal, seconds):
        # One trial ended with a miss, and the gate's own end is never recorded.
        return gated(analyst, proposal, seconds) | {"trials_in_no_gate_run": [missed]}

    def lost(*_):
        raise httpx2.ReadTimeout("no answer")

    monkeypatch.setattr(evidence, "_ask_gate", lost)
    monkeypatch.setattr(evidence, "_gated", no_end_yet)
    monkeypatch.setattr(evidence, "GATE_WAIT", 0)

    run = made(layer, "first-cycle", before_gate=lambda _: Decimal("7.5"))

    assert run["outcome"] == "stopped" and run["charged_until_counted"] == "7.5"
    assert run["trials_in_no_gate_run"] == [missed]
    assert (run["readings"]["trials"], run["readings"]["trials_missed"]) == (1, 1)
    assert Decimal(run["cost_usd"]) == Decimal("7.75")
    assert "a-case` trial 1, completed: missed e1 (hold_report)" in page(run)


def test_a_curator_run_asked_for_and_then_not_read_keeps_its_charge_and_stops(
    make_layer, tmp_path, monkeypatch
):
    layer = evidence_layer(make_layer, tmp_path, *HONEST)
    answer = evidence.playbooks._answer

    def unread(http, method, path, body=None, timeout=None):
        if method == "GET" and path.startswith("/api/curator-runs/"):
            raise evidence.playbooks.CommandFailed("The backend cannot be reached.")
        return answer(http, method, path, body, timeout)

    monkeypatch.setattr(evidence.playbooks, "_answer", unread)

    run = made(layer, "first-cycle", settings=SETTINGS | {"CURATOR_MAX_DOLLARS": "2"})

    assert run["outcome"] == "stopped" and run["curator_run_id"]
    assert run["stopped"] == "CommandFailed: The backend cannot be reached."
    assert (run["charged_until_counted"], Decimal(run["cost_usd"])) == ("2", 2)


def test_a_curator_the_backend_could_not_start_charges_nothing(make_layer, tmp_path):
    def down(*_):
        app = FastAPI()
        app.add_api_route("/invocations", lambda: JSONResponse({}, 503), methods=["POST"])
        return app

    layer = evidence_layer(make_layer, tmp_path, curator=down)

    run = made(layer, "first-cycle", settings=SETTINGS | {"CURATOR_MAX_DOLLARS": "2"})

    assert run["outcome"] == "curator_did_not_run" and run["stopped"] is None
    assert run["curator_refused"].startswith("503: ")
    assert (run["charged_until_counted"], Decimal(run["cost_usd"])) == ("0", 0)


def test_trials_run_before_a_gate_refused_are_read_with_their_cost(make_layer):
    asked: list[dict] = []

    def tiring(url: str, key: str) -> FastAPI:
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

    layer = make_layer(agent=tiring, curates=lambda: [HONEST[0]()])
    gating.first_cycle(layer)
    proposal = gating.curation.curated(layer)["proposal"]["id"]
    assert gating.gate(layer, proposal).status_code == 503

    with httpx2.Client(
        base_url=layer.url, headers={"Authorization": f"Bearer {ANALYST}"}, trust_env=False
    ) as analyst:
        found = evidence._gated(analyst, proposal, 1.0)

    assert found["gate"] is None and found["gates_ended"] == 1
    assert [r.split(":")[0] for r in found["gate_refusals"]] == ["agent_did_not_start"]
    loose = found["trials_in_no_gate_run"]
    assert len(loose) == 6 and all(t["state"] == "completed" for t in loose)
    run = {
        "setup": {"cost_usd": "0"},
        "curator_run": None,
        "gate": None,
        "trials_in_no_gate_run": [t | {"cost_usd": "0.250000"} for t in loose],
    }
    assert evidence._spent(run) == Decimal("1.5")


def test_the_freeze_is_checked_again_before_each_run(monkeypatch, tmp_path):
    checks = iter([[], [], ["sources changed: a memo"]])
    started: list[str] = []

    def contained(scenario, number, triaging, curating, left, origin, freeze, keep):
        started.append(scenario.id)
        keep({"freeze": freeze, "cost_usd": "0", "stopped": None})
        return {"outcome": "no_change", "cost_usd": "0", "stopped": None}

    monkeypatch.setattr(evidence, "PUBLISHED", tmp_path / "runs")
    monkeypatch.setattr(evidence, "PAGE", tmp_path / "evidence.md")
    monkeypatch.setattr(evidence, "freeze_breaks", lambda: next(checks))
    monkeypatch.setattr(evidence, "models", lambda _: ({}, {}))
    monkeypatch.setattr(evidence, "contained", contained)
    monkeypatch.setattr(evidence, "render", lambda *_: "")

    with pytest.raises(evidence.Refused, match="sources changed: a memo"):
        evidence.make_runs(rehearse=False)
    assert started == ["first-cycle"]


def test_a_stopped_run_is_written_and_no_other_starts(monkeypatch, tmp_path):
    started: list[str] = []

    def contained(scenario, number, triaging, curating, left, origin, freeze, keep):
        started.append(scenario.id)
        stopped = {"freeze": freeze, "cost_usd": "1.25", "stopped": "HTTPError: gone"}
        keep(stopped)
        return stopped | {"outcome": "stopped"}

    monkeypatch.setattr(evidence, "PUBLISHED", tmp_path / "runs")
    monkeypatch.setattr(evidence, "PAGE", tmp_path / "evidence.md")
    monkeypatch.setattr(evidence, "freeze_breaks", lambda: [])
    monkeypatch.setattr(evidence, "models", lambda _: ({}, {}))
    monkeypatch.setattr(evidence, "contained", contained)
    monkeypatch.setattr(evidence, "render", lambda *_: "")

    assert evidence.make_runs(rehearse=False) == 1
    assert started == ["first-cycle"]
    written = json.loads((tmp_path / "runs" / "first-cycle-1.json").read_text())
    assert written["cost_usd"] == "1.25"


@pytest.mark.parametrize("origin, freeze", [("live", "0" * 64), ("rehearsal", None)])
def test_the_page_is_not_written_from_a_run_of_another_freeze_or_a_rehearsal(
    monkeypatch, tmp_path, origin, freeze
):
    kept = json.loads(evidence.FREEZE.read_text())["sha256"]
    folder = tmp_path / "runs"
    folder.mkdir()
    run = {"origin": origin, "freeze": freeze or kept, "scenario": "first-cycle", "run": 1}
    (folder / "first-cycle-1.json").write_text(json.dumps(run))

    with pytest.raises(evidence.Refused, match="another freeze"):
        evidence.write_pages(folder, tmp_path / "evidence.md")
    assert not (tmp_path / "evidence.md").exists()


def test_a_curator_runs_held_charge_is_said_to_be_the_curators(make_layer, tmp_path, monkeypatch):
    layer = evidence_layer(make_layer, tmp_path, *HONEST)
    answer = evidence.playbooks._answer

    def unread(http, method, path, body=None, timeout=None):
        if method == "GET" and path.startswith("/api/curator-runs/"):
            raise evidence.playbooks.CommandFailed("The backend cannot be reached.")
        return answer(http, method, path, body, timeout)

    monkeypatch.setattr(evidence.playbooks, "_answer", unread)

    run = made(layer, "first-cycle", settings=SETTINGS | {"CURATOR_MAX_DOLLARS": "2"})

    assert run["charged_for"] == "the curator run"
    shown = page(run)
    assert "$2 that the curator run could spend charged as spent" in shown
    assert "that the gate's trials could spend" not in shown


def test_an_interrupt_while_the_gates_end_is_awaited_keeps_the_trials_read(
    make_layer, tmp_path, monkeypatch
):
    layer = evidence_layer(make_layer, tmp_path, *HONEST)
    gated = evidence._gated
    missed = {
        "case": "a-case",
        "kind": "new_rule",
        "trial": 1,
        "state": "completed",
        "failure": None,
        "notes": [],
        "misses": [{"exception": "e1", "fields": ["hold_report"]}],
        "cost_usd": "0.250000",
    }
    calls = iter([[], [missed]])

    def no_end_yet(analyst, proposal, seconds):
        # Before the gate is asked, then one trial with a miss read, then the runner is stopped.
        found = next(calls, None)
        if found is None:
            raise KeyboardInterrupt
        return gated(analyst, proposal, seconds) | {"trials_in_no_gate_run": found}

    def lost(*_):
        raise httpx2.ReadTimeout("no answer")

    monkeypatch.setattr(evidence, "_ask_gate", lost)
    monkeypatch.setattr(evidence, "_gated", no_end_yet)
    kept: list[dict] = []

    with pytest.raises(KeyboardInterrupt):
        made(
            layer,
            "first-cycle",
            before_gate=lambda _: Decimal("7.5"),
            keep=lambda r: kept.append(json.loads(json.dumps(r))),
        )

    last = kept[-1]
    assert last["trials_in_no_gate_run"] == [missed]
    assert last["stopped"] == "KeyboardInterrupt: the runner was stopped"
    assert last["charged_until_counted"] == "7.5"


class _Answered:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _contained(monkeypatch, left: str, curator_cost: str, origin: evidence.Origin = "live") -> dict:
    """`contained` with the containers stood in for: what it starts, writes and publishes."""
    seen: dict = {"started": [], "written": [], "run": []}
    monkeypatch.setattr(cycle, "start", lambda **given: seen["started"].append(given))
    monkeypatch.setattr(cycle, "run", lambda *command, **_: seen["run"].append(command))
    monkeypatch.setattr(cycle, "settings_in", lambda path: {"WEALTHCONTEXT_TOKEN": "t"})
    monkeypatch.setattr(
        cycle, "write_settings", lambda *given, **named: seen["written"].append((given, named))
    )
    monkeypatch.setattr(evidence, "_clients", lambda: (_Answered(), _Answered()))

    def run_once(*_, before_gate, settings, **__):
        try:
            seen["cap"] = before_gate({"cost_usd": curator_cost})
        except evidence.NothingLeft as stopped:
            seen["stopped"] = str(stopped)
        seen["settings"] = dict(settings)
        return {}

    monkeypatch.setattr(evidence, "run_once", run_once)
    triaging = {"TRIAGE_MODEL": "anthropic:claude-haiku-5-5", "ANTHROPIC_API_KEY": "k"}
    curating = {"CURATOR_MODEL": "anthropic:claude-opus-5-5", "ANTHROPIC_API_KEY": "k"}
    evidence.contained(
        evidence.scenarios()[0], 1, triaging, curating, Decimal(left), origin, FREEZE, print
    )
    return seen


def test_with_nothing_left_after_the_curator_the_gate_is_not_run(monkeypatch):
    seen = _contained(monkeypatch, left="1.50", curator_cost="1.500000")

    assert seen["stopped"].startswith("nothing is left of the $40 for the gate's trials")
    assert "cap" not in seen and seen["run"] == [] and len(seen["written"]) == 0
    assert seen["settings"]["GATE_MAX_DOLLARS"] == "0"


def test_the_published_caps_are_the_ones_the_gate_was_given(monkeypatch):
    seen = _contained(monkeypatch, left="5", curator_cost="1.500000")

    ((given, named),) = seen["written"]
    assert seen["cap"] == Decimal("3.5") and named["gate_cap"] == "3.500000"
    assert seen["settings"]["GATE_MAX_DOLLARS"] == named["gate_cap"]
    assert seen["settings"]["TRIAGE_MAX_DOLLARS"] == given[2] == "2"


@pytest.mark.parametrize(
    ("origin", "first", "then"),
    [("live", ("curator",), ("triage-agent", "curator")), ("rehearsal", (), ())],
)
def test_only_the_agent_on_a_live_model_has_a_way_out_and_none_records(
    monkeypatch, origin, first, then
):
    seen = _contained(monkeypatch, left="40", curator_cost="1", origin=origin)

    (started,) = seen["started"]
    assert started["out"] == first and not started.get("record")
    ((_, named),) = seen["written"]
    assert not named.get("record")
    (command,) = seen["run"]
    given = " ".join(command)
    assert ("compose.live.yaml" in given) is ("triage-agent" in then)
    assert ("compose.live-curation.yaml" in given) is ("curator" in then)
    assert " ".join(cycle.ours("up", out=first)).count("compose.live") == len(first)


def test_a_proposal_whose_words_break_a_rule_is_published_without_them():
    honest = {"id": "p", "sha256": "0" * 64, **gating.honest()}
    named = copy.deepcopy(honest)
    named["changes"][0]["line"] = "Ring Quillon Vantree first."

    assert evidence._published_proposal(honest)["changes"] == honest["changes"]
    assert evidence._published_proposal(named) == {
        "id": "p",
        "sha256": "0" * 64,
        "withheld": "personal_data",
    }
    numbered = copy.deepcopy(honest)
    numbered["changes"][0]["version"] = 1234567
    assert evidence._published_proposal(numbered)["withheld"] == "client_identifier"
    run = {
        "curator_run": {"proposal": evidence._published_proposal(named)},
        "gate": None,
        "trials_in_no_gate_run": [],
    }
    assert evidence.readings(run, scenario("first-cycle"))["playbooks_changed"] == []


def test_the_public_documents_say_the_case_set_is_signed_by_the_hash_it_has():
    signed = json.loads(evidence.FREEZE.read_text())["case_set"]
    readme, rules = (ROOT / "README.md").read_text(), (ROOT / "guide" / "rules.md").read_text()

    assert signed in readme
    assert "signed off as written on 2026-11-23" in readme
    assert "signed off as written on 2026-11-23" in " ".join(rules.split())


def test_a_curator_run_whose_model_gave_no_answer_stays_charged_its_cap(make_layer, tmp_path):
    layer = evidence_layer(make_layer, tmp_path, lambda: RuntimeError("no answer"))

    run = made(layer, "first-cycle", settings=SETTINGS | {"CURATOR_MAX_DOLLARS": "2"})

    assert run["curator_run"]["failure"] == evidence.NO_ANSWER
    assert (run["outcome"], run["stopped"]) == ("curator_failed", None)
    assert (run["charged_until_counted"], Decimal(run["cost_usd"])) == ("2", 2)
    assert "$2 that the curator run could spend charged as spent" in page(run)
