"""The gate, as the tests reach it through the API, and the hostile proposals it must refuse."""

import copy
import json
from pathlib import Path
from typing import Any

import curation
import held_out_cases
import knowledge
import stand_ins
import yaml
from layer import ROOT

HOSTILE = ROOT / "cases" / "hostile"
NOTICE = "custody-meeting-notice"
CROSS_CHECK_FOUNDING = curation.CROSS_CHECK_FOUNDING
_SCRIPTED = json.loads((ROOT / "recordings" / "scripted-curation.json").read_text())
# The honest first-cycle proposal, as the scripted curator gives it.
HONEST = _SCRIPTED["replies"][0]["tool_calls"][0]["args"]
_TRIAGE = json.loads((ROOT / "recordings" / "scripted-triage.json").read_text())
# The gap report the scripted review files, so that its reference is the recorded one.
SCRIPTED_SUMMARY = next(
    each["args"]["summary"]
    for reply in _TRIAGE["replies"]
    for each in reply.get("tool_calls", [])
    if each["name"] == "file_gap_report"
)
CHECKS = ["support", "personal", "guardrails", "base", "overlap"]


def honest() -> dict[str, Any]:
    return copy.deepcopy(HONEST)


def hostile_files() -> list[Path]:
    return sorted(HOSTILE.glob("*.yaml"))


def hostile(file: Path) -> dict[str, Any]:
    return yaml.safe_load(file.read_text())


def sent(case: dict[str, Any]) -> dict[str, Any]:
    """What `playbooks check` sends of a proposal file."""
    return {name: case[name] for name in ("changes", "disagreements")}


def trial_agent(backend_url: str, key: str):
    """An agent that plays the gate's held-out trials, each step that fits, and nothing else."""
    return stand_ins.trial_agent(backend_url, key)


def first_cycle(layer) -> dict[str, Any]:
    """The first cycle's knowledge before curation: what the hostile proposals are written for.
    The gate's held-out cases are a set made for the tests, which the honest proposal passes."""
    if not (layer.settings.cases / "case-set.yaml").exists():
        held_out_cases.write(layer.settings.cases)
    made = curation.first_cycle(layer, summary=SCRIPTED_SUMMARY)
    assert knowledge.intake(layer, knowledge.committed_memo(NOTICE)).status_code == 200
    return made


def check(layer, proposal: dict[str, Any], as_owner: bool = False):
    send = layer.as_owner if as_owner else layer.as_analyst
    return send("POST", "/api/checked-proposals", json=proposal)


def checked(layer, proposal: dict[str, Any]) -> dict[str, Any]:
    response = check(layer, proposal)
    assert response.status_code == 200, response.text
    return response.json()


def gate(layer, proposal_id: str, as_owner: bool = False):
    send = layer.as_owner if as_owner else layer.as_analyst
    return send("POST", f"/api/proposals/{proposal_id}/gate-runs")


def gated(layer, proposal_id: str) -> dict[str, Any]:
    response = gate(layer, proposal_id)
    assert response.status_code == 200, response.text
    return response.json()


def approve(layer, proposal_id: str, sha256: str = "0" * 64, gate_run: str = "0" * 32, send=None):
    send = send or layer.as_owner
    body = {"sha256": sha256, "gate_run": gate_run}
    return send("POST", f"/api/proposals/{proposal_id}/approve", json=body)


def outcomes(run: dict[str, Any]) -> dict[str, str]:
    return {each["check"]: each["outcome"] for each in run["checks"]}


def failed(run: dict[str, Any]) -> set[str]:
    return {name for name, outcome in outcomes(run).items() if outcome == "failed"}


def rules(run: dict[str, Any], check_name: str) -> set[str]:
    (found,) = [each for each in run["checks"] if each["check"] == check_name]
    return {failure["rule"] for failure in found["failures"]}


def record(layer) -> dict[str, Any]:
    response = layer.as_analyst("GET", "/api/record")
    assert response.status_code == 200, response.text
    return response.json()


def approved_files(folder: Path) -> dict[str, bytes]:
    """Every approved playbook's file, and the desks: all but the ledger."""
    return {
        str(file.relative_to(folder)): file.read_bytes()
        for file in sorted(folder.rglob("*"))
        if file.is_file() and file.name != "ledger.jsonl"
    }
