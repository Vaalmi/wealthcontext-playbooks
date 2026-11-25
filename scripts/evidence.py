"""`make evidence`: the five evidence scenarios, three runs each on the live curator.

Each run starts the layer on fresh records, takes in the scenario's memos, triages its dates
and records its correction, with the triage agent's replies from the scenario's scripted
recording, so that every run of a scenario hands the curator the same bundle. Then the
curator runs on the model `.env` names, the triage agent is started again on its own
configured model, and the gate runs every check on what was proposed, the held-out cases
three times each among them. Each run is written to `guide/evidence/runs/` as it ends, and
`guide/evidence.md` is written again from every published run: what ran, on which models and
settings, what was proposed, each check's result and the outcome, misses included.

Nothing runs unless the hashes in `evidence/freeze.json` still hold: the curator's
instructions and tools, the firm sources, the held-out case set, the founding playbooks, and
the scenarios with their scripted replies. The runs spend at most forty dollars in all, the
published runs' cost included, so the command can be run again to finish what a stop left.

`--rehearse` makes every run with no key, on scripted replies only, and writes them to
`.local/evidence/`, never to `guide/`. `--freeze` writes the freeze; `--pages` writes the page
again from the published runs.
"""

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx2
import yaml

from backend import cases, personal_data
from cli import main as playbooks
from curator import harness as curator_harness
from scripts import cycle

ROOT = cycle.ROOT
SCENARIOS = ROOT / "evidence" / "scenarios.yaml"
FREEZE = ROOT / "evidence" / "freeze.json"
PUBLISHED = ROOT / "guide" / "evidence" / "runs"
PAGE = ROOT / "guide" / "evidence.md"
REHEARSED = ROOT / ".local" / "evidence"
CORRECTION = cycle.CORRECTION
RUNS_OF_EACH = 3
# The whole evidence, published runs included, and what one run may spend of what is left.
ALL_DOLLARS = Decimal("40")
CURATOR_DOLLARS = Decimal("2")
GATE_DOLLARS = Decimal("10")
TRIAL_DOLLARS = Decimal("2")
# Below this, no run is started: it could not pay for its curator and its trials.
LEAST_DOLLARS = Decimal("1")
CURATOR_WAIT = 900
# How long the record is read for a gate's end after its answer went missing or refused.
GATE_WAIT = playbooks.GATE_SECONDS
TRIAGE_WAIT = 300
# A rehearsal's curator, and the replies its triage agent is started again on for the trials:
# the scripted cycle's, which fit the first scenario's proposal alone.
REHEARSAL_CURATOR = "replay:recordings/scripted-curation.json"
REHEARSAL_TRIALS = "replay:recordings/scripted-first-cycle-triage.json"

Origin = Literal["live", "rehearsal"]


class Refused(Exception):
    pass


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    about: str
    memos: tuple[str, ...]
    triage: tuple[str, ...]
    correction: bool
    replies: str
    watch: tuple[str, ...]
    gap_reports: int


def scenarios(path: Path = SCENARIOS) -> list[Scenario]:
    listed = yaml.safe_load(path.read_text(encoding="utf-8"))["scenarios"]
    return [
        Scenario(
            id=each["id"],
            title=each["title"],
            about=each["about"],
            memos=tuple(each["memos"]),
            triage=tuple(each["triage"]),
            correction=each["correction"],
            replies=each["replies"],
            watch=tuple(each["watch"]),
            gap_reports=each["gap_reports"],
        )
        for each in listed
    ]


# The freeze


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def frozen(root: Path = ROOT) -> dict[str, Any]:
    """The hashes of everything a run rests on but the models: what the curator is told and
    given, what the gate tests with, and the scenarios' own inputs."""

    def files(*paths: Path) -> dict[str, str]:
        return {path.relative_to(root).as_posix(): _sha(path.read_bytes()) for path in paths}

    listed = scenarios(root / SCENARIOS.relative_to(ROOT))
    parts: dict[str, Any] = {
        "curator": {
            "instructions": _sha(curator_harness.INSTRUCTIONS.encode()),
            **files(root / "curator" / "harness.py", root / "curator" / "tools.py"),
        },
        "sources": files(*sorted(p for p in (root / "sources").rglob("*") if p.is_file())),
        "case_set": cases.set_sha256(root / "cases"),
        # The whole folder each fresh layer is seeded from: desks, versions and the ledger.
        "playbooks": files(*sorted(p for p in (root / "playbooks").rglob("*") if p.is_file())),
        "scenarios": files(
            root / SCENARIOS.relative_to(ROOT),
            *sorted({root / each.replies for each in listed}),
        ),
    }
    return {"sha256": _sha(json.dumps(parts, sort_keys=True).encode()), **parts}


def freeze_breaks(root: Path = ROOT) -> list[str]:
    """What no longer holds of the committed freeze, by name, or nothing."""
    try:
        kept = json.loads((root / FREEZE.relative_to(ROOT)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["there is no freeze"]
    now = frozen(root)
    broken = []
    for part in ("curator", "sources", "case_set", "playbooks", "scenarios"):
        if kept.get(part) == now[part]:
            continue
        if isinstance(now[part], str):
            broken.append(f"{part} changed")
            continue
        was = kept.get(part) or {}
        names = sorted(n for n in set(was) | set(now[part]) if was.get(n) != now[part].get(n))
        broken.append(f"{part} changed: {', '.join(names)}")
    if not broken and kept.get("sha256") != now["sha256"]:
        broken.append("the freeze's own hash does not hold")
    return broken


def write_freeze(root: Path = ROOT) -> str:
    made = frozen(root)
    about = (
        "The hashes every evidence run rests on, but the models: the curator's instructions "
        "and tools, every firm source, the held-out case set (as the gate hashes it), the "
        "founding playbooks and desks, and the scenarios with their scripted triage replies. "
        "Written before the first run. No run starts unless every hash still holds."
    )
    text = json.dumps({"about": about, **made}, indent=2) + "\n"
    (root / FREEZE.relative_to(ROOT)).write_text(text, encoding="utf-8")
    return made["sha256"]


# One run, against a layer that is running


def _cost(*amounts: str | None) -> Decimal:
    return sum((Decimal(a) for a in amounts if a), Decimal(0)).quantize(Decimal("0.000001"))


def _set_up(
    analyst: httpx2.Client, owner: httpx2.Client, scenario: Scenario
) -> tuple[dict, list[str]]:
    """Take the memos in, triage the dates and record the correction, as the scenario lists.
    A refusal stops the setting up, and says where."""
    memos, runs, failed = [], [], []
    corrected = None
    try:
        for name in scenario.memos:
            # Its bytes as they are, so that the hash taken is the file's, as `playbooks intake`.
            content = (ROOT / "sources" / "memos" / f"{name}.md").read_bytes().decode()
            taken = playbooks._answer(owner, "POST", "/api/sources", {"content": content})
            memos.append({"id": taken["id"], "sha256": taken["sha256"]})
        for day in scenario.triage:
            found = playbooks.triage(analyst, day, cycle.CUTOFF, TRIAGE_WAIT)
            runs.append(found)
            if found["state"] != "completed":
                failed.append(f"the triage of {day} did not complete")
        if scenario.correction and not failed:
            corrected = _corrected(analyst, runs, failed)
        reports = playbooks._answer(analyst, "GET", "/api/gap-reports")["gap_reports"]
    except playbooks.CommandFailed as refused:
        failed.append(f"refused while setting up: {refused}")
        reports = []
    if not failed and len(reports) != scenario.gap_reports:
        failed.append(f"{len(reports)} gap reports were filed, not {scenario.gap_reports}")
    setup = {
        "memos": memos,
        "triage": [
            {
                "date": r["date"],
                "state": r["state"],
                "notes": len(r["notes"]),
                "with_a_step": sum(n["state"] == "chosen" for n in r["notes"]),
                "codes": sorted(n["code"] for n in r["notes"]),
                "failure": r["failure"],
                "cost_usd": r["usage"]["cost_usd"],
            }
            for r in runs
        ],
        "correction": corrected and {"id": corrected["source_id"], "state": corrected["state"]},
        "gap_reports": [
            {"reference": r["reference"], "code": r["code"], "summary": r["summary"]}
            for r in reports
        ],
    }
    spent = [r["usage"]["cost_usd"] for r in runs]
    if corrected:
        spent.append(corrected["usage"]["cost_usd"])
    setup["cost_usd"] = str(_cost(*spent))
    return setup, failed


def _corrected(analyst: httpx2.Client, runs: list[dict], failed: list[str]) -> dict | None:
    """The committed correction, recorded against the one note of its date with a step."""
    told = yaml.safe_load(CORRECTION.read_text(encoding="utf-8"))
    run = next((r for r in runs if r["date"] == told["date"]), None)
    chosen = [n for n in (run or {}).get("notes", []) if n["state"] == "chosen"]
    if run is None or len(chosen) != 1:
        failed.append("there is no one note with a step to correct")
        return None
    should_be = told["correction"]["should_be"]
    if isinstance(should_be, bool):
        should_be = "yes" if should_be else "no"
    corrected = playbooks.correct(
        analyst,
        run["id"],
        chosen[0]["id"],
        told["correction"]["field"],
        should_be,
        told["text"],
        TRIAGE_WAIT,
    )
    if corrected["state"] != "reviewed":
        failed.append("the correction's review did not end")
    return corrected


def _curator_run(found: dict) -> dict:
    usage = found["usage"]
    seconds = None
    if found["finished_at"]:
        taken = datetime.fromisoformat(found["finished_at"])
        seconds = round((taken - datetime.fromisoformat(found["started_at"])).total_seconds(), 1)
    proposal = found["proposal"]
    return {
        "id": found["id"],
        "state": found["state"],
        "stalled": found["stalled"],
        "failure": found["failure"],
        "model": found["model"],
        "calls_by": usage["models"],
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cost_usd": usage["cost_usd"],
        "seconds": seconds,
        "bundle": {
            "sha256": found["bundle"]["sha256"],
            "items": [
                {"kind": i["kind"], "reference": i["reference"]} for i in found["bundle"]["items"]
            ],
        },
        "refused": [s["line"] for s in found["steps"] if s["outcome"] == "refused"],
        "proposal": proposal and _published_proposal(proposal),
    }


def _published_proposal(proposal: dict) -> dict:
    """A proposal as a run publishes it. Its words are the curator's: they are published only if
    no rule on personal data or client identifiers refuses them, as the backend's were when the
    proposal was recorded. Otherwise only its hash and the rule."""
    words = {"changes": proposal["changes"], "disagreements": proposal["disagreements"]}
    published = {"id": proposal["id"], "sha256": proposal["sha256"]}
    # Read whole, as a recording is: every string, key and number the curator gave.
    for text in personal_data.written(words):
        if rule := personal_data.broken_rule(text, personal_data.REASON_LENGTH, agent_words=False):
            return published | {"withheld": rule}
    return published | words


def _gate(gated: dict, seconds: float) -> dict:
    held = gated["held_out"]
    trials = (held or {}).get("trials", [])
    return {
        "id": gated["id"],
        "outcome": gated["outcome"],
        "seconds": round(seconds, 1),
        "checks": gated["checks"],
        "disagreements_found": gated["disagreements"],
        "held_out": held
        and {
            "case_set_sha256": held["case_set_sha256"],
            "writer": held["writer"],
            "trial_model": held["trial_model"],
            "calls_by": held["models"],
            "graded": held["graded"],
            "cost_usd": held["cost_usd"],
            "trials": [_trial(t) for t in trials],
        },
    }


def outcome_of(run: dict) -> str:
    """One word for how a run ended, from its record alone."""
    if run["stopped"]:
        return "stopped"
    if run["setup_failed"]:
        return "setup_failed"
    curated = run["curator_run"]
    if curated is None:
        return "curator_did_not_run"
    if curated["state"] == "working":
        return "curator_did_not_end"
    if curated["state"] == "completed_no_change":
        return "no_change"
    if curated["state"] != "completed":
        return "curator_failed"
    if run["gate"] is None:
        return "gate_did_not_run"
    return "gate_passed" if run["gate"]["outcome"] == "passed" else "gate_failed"


def readings(run: dict, scenario: Scenario) -> dict:
    """What the records say about the questions the scenarios ask. Facts, not a verdict."""
    proposal = (run["curator_run"] or {}).get("proposal")
    changes = proposal.get("changes", []) if proposal else []
    rested = {ref for change in changes for ref in change["rests_on"]}
    gate = run["gate"] or {}
    held = gate.get("held_out") or {}
    trials = [*held.get("trials", []), *run["trials_in_no_gate_run"]]
    return {
        "playbooks_changed": sorted({c["playbook"] for c in changes}),
        "rests_on": sorted(rested),
        "rests_on_a_gap_report": any(r.startswith("gap-report-") for r in rested),
        "watched": {ref: ref in rested for ref in scenario.watch},
        "disagreements_listed": [d["between"] for d in proposal.get("disagreements", [])]
        if proposal
        else [],
        "checks_failed": [c["check"] for c in gate.get("checks", []) if c["outcome"] == "failed"],
        "trials": len(trials),
        "trials_missed": sum(1 for t in trials if t["state"] != "completed" or t["misses"]),
    }


def _trial(trial: dict) -> dict:
    return {
        "case": trial["case"],
        "kind": trial["kind"],
        "trial": trial["trial"],
        "state": trial["state"],
        "failure": trial["failure"],
        "notes": trial["notes"],
        "misses": trial["misses"],
        "cost_usd": trial["cost_usd"],
    }


def _gated(analyst: httpx2.Client, proposal: str, seconds: float) -> dict[str, Any]:
    """What the record holds of the gate on a proposal, whatever its answer said: the last gate
    run, every refusal, and each trial that no finished gate run holds."""
    found = playbooks._answer(analyst, "GET", "/api/record")
    (listed,) = [p for p in found["proposals"] if p["id"] == proposal]
    runs = [g for g in listed["gate_runs"] if g["kind"] == "gate"]
    ended = {g["id"] for g in runs}
    loose = [
        _trial(entry["data"]["trial"])
        for entry in found["entries"]
        if entry["event"] == "held_out_trial"
        and entry["data"]["proposal_id"] == proposal
        and entry["data"]["gate_run_id"] not in ended
    ]
    return {
        "gate": _gate(runs[-1], seconds) if runs else None,
        "gate_refusals": [f"{r['reason']}: {r['line']}" for r in listed["gate_refusals"]],
        "trials_in_no_gate_run": loose,
        # How many gates on it the record holds as ended: run to their end, or refused.
        "gates_ended": len(runs) + len(listed["gate_refusals"]),
    }


# The curator run's failure when its model gave no answer, as the backend says it.
NO_ANSWER = "The model could not be called."


class NotInTheRecord(Exception):
    pass


class NothingLeft(Exception):
    pass


def _sent(http: httpx2.Client, path: str, timeout: float | None = None) -> tuple[str | None, dict]:
    """POST: the backend's refusal as `status: line` and nothing, or no refusal and what it
    answered. No answer at all raises: then the move may have been made."""
    response = http.post(path, timeout=timeout)
    try:
        answered = response.json()
    except ValueError:
        answered = {}
    if response.status_code < 400:
        return None, answered
    return f"{response.status_code}: {answered.get('detail', '')}", {}


def _ask_gate(analyst: httpx2.Client, proposal: str) -> str | None:
    return _sent(analyst, f"/api/proposals/{proposal}/gate-runs", playbooks.GATE_SECONDS)[0]


def _curate(owner: httpx2.Client, record: dict, kept: Callable[[], dict], wait: float) -> dict:
    """The curator run, from its start to its end in the record. Its cap is charged from the
    moment it is asked for, and stays charged unless the run is seen to end."""
    _charge(record, record["settings"].get("CURATOR_MAX_DOLLARS", "0"), "the curator run")
    kept()
    refused, run = _sent(owner, "/api/curator-runs")
    if refused:
        # The backend answered and started nothing: a run it could not start is undone.
        record["curator_refused"] = refused
        _charge(record, "0")
        return {}
    record["curator_run_id"] = run["id"]
    kept()
    until = time.monotonic() + wait
    while True:
        found = playbooks._answer(owner, "GET", f"/api/curator-runs/{run['id']}")
        if found["state"] != "working":
            # A call that brought no answer back may have been billed, and the curator records
            # no cost for it: its run's cap stays charged.
            if found["failure"] != NO_ANSWER:
                _charge(record, "0")
            return found
        if found["stalled"] or time.monotonic() >= until:
            raise NotInTheRecord("the curator run did not end, so what it spent is not known")
        time.sleep(0.25)


def _charge(record: dict, amount: str, what: str | None = None) -> None:
    """Hold an amount as spent until it is counted, and say what it is held for."""
    record["charged_until_counted"] = amount
    record["charged_for"] = what if Decimal(amount) else None


def _spent(record: dict) -> Decimal:
    curated = record["curator_run"] or {}
    held = (record["gate"] or {}).get("held_out") or {}
    return _cost(
        record["setup"]["cost_usd"],
        curated.get("cost_usd"),
        held.get("cost_usd"),
        *(t["cost_usd"] for t in record["trials_in_no_gate_run"]),
    )


def run_once(
    analyst: httpx2.Client,
    owner: httpx2.Client,
    scenario: Scenario,
    number: int,
    *,
    origin: Origin,
    freeze: str,
    settings: dict[str, str],
    before_gate: Callable[[dict], Decimal] = lambda _: Decimal(0),
    keep: Callable[[dict], None] = lambda _: None,
    curator_wait: float = CURATOR_WAIT,
) -> dict:
    """One run of a scenario on a layer started for it, as its record. The gate runs only
    on a proposal; nothing is approved.

    `keep` is given the record as each part ends, so that a run stopped part way is still
    there to publish. Until the gate's trials are counted, what they may spend (what
    `before_gate` returns) is charged to the run."""
    began = datetime.now(UTC)
    setup, failed = _set_up(analyst, owner, scenario)
    record: dict[str, Any] = {
        "origin": origin,
        "scenario": scenario.id,
        "run": number,
        "freeze": freeze,
        "started_at": began.isoformat(timespec="seconds"),
        "settings": settings,
        "setup": setup,
        "setup_failed": failed,
        "curator_refused": None,
        "curator_run": None,
        "gate": None,
        "gate_refused": None,
        "curator_run_id": None,
        "gate_refusals": [],
        "trials_in_no_gate_run": [],
        "stopped": "it had not ended",
        "charged_until_counted": "0",
        "charged_for": None,
    }

    def kept() -> dict:
        record["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        record["cost_usd"] = str(_spent(record) + Decimal(record["charged_until_counted"]))
        record["outcome"] = outcome_of(record)
        record["readings"] = readings(record, scenario)
        keep(record)
        return record

    kept()
    try:
        found = _curate(owner, record, kept, curator_wait) if not failed else {}
        if found:
            record["curator_run"] = _curator_run(found)
            kept()
            if found["state"] == "completed" and found["proposal"]:
                proposal = found["proposal"]["id"]
                ended = _gated(analyst, proposal, 0)["gates_ended"]
                _charge(record, str(before_gate(record["curator_run"])), "the gate's trials")
                kept()
                clock = time.monotonic()
                try:
                    record["gate_refused"] = _ask_gate(analyst, proposal)
                except httpx2.HTTPError as lost:
                    record["gate_refused"] = f"no answer: {type(lost).__name__}"
                # Whatever it answered, the outcome is the record's, once it holds the gate's end.
                # Each reading is kept as it is read, so trials already ended are published even
                # if the end never comes; only then is what they may spend released.
                until = time.monotonic() + GATE_WAIT
                while True:
                    gated = _gated(analyst, proposal, time.monotonic() - clock)
                    read = {k: v for k, v in gated.items() if k != "gates_ended"}
                    if any(record.get(k) != v for k, v in read.items()):
                        record |= read
                        kept()
                    if gated["gates_ended"] > ended:
                        break
                    if time.monotonic() >= until:
                        raise NotInTheRecord("the gate's end is not in the record")
                    time.sleep(1)
                _charge(record, "0")
        record["stopped"] = None
    except Exception as error:  # what stopped it is published with the run
        record["stopped"] = f"{type(error).__name__}: {error}"
    except BaseException as error:
        # An interrupt: the run is kept as it stands, then the interrupt goes on up.
        record["stopped"] = f"{type(error).__name__}: the runner was stopped"
        kept()
        raise
    return kept()


# The page


def published(folder: Path = PUBLISHED) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]


def run_name(scenario: str, number: int) -> str:
    return f"{scenario}-{number}.json"


OUTCOMES = {
    "no_change": "the curator proposed no change",
    "curator_failed": "the curator run failed",
    "curator_did_not_run": "the curator could not be started",
    "curator_did_not_end": "the curator run did not end",
    "setup_failed": "the scenario could not be set up, and the curator was not run",
    "gate_did_not_run": "the gate did not run",
    "gate_passed": "every check passed",
    "gate_failed": "the gate refused it",
    "stopped": "the run stopped before it ended",
}

INTRODUCTION = """\
# The evidence runs

The recorded cycle shows that the parts connect. It cannot show whether a real model is a
good curator: it is one live run, on sources written for it. These runs are meant to show
that. Five scenarios, each run three times on a live curator, each run
published here with its models and settings, what the curator proposed, every check's
result and the outcome, misses included. Nothing on this page is written by hand: the
command below writes it from the runs' own records, kept in `guide/evidence/runs/`.

They are read against three lines, set before any run:

- **Continue** if the recorded cycle replays against a running WealthContext, the gate
  refuses every hostile proposal in [cases/hostile/](../cases/hostile), and across these runs
  the curator produces a proposal that passes the gate, leaves the playbooks no source
  touches as they are, proposes nothing from a memo that changes no rule, and gets nothing
  approved from a poisoned memo.
- **Change direction** if the curator succeeds only when a single source states the new
  rule outright, and fails when it has to work from a pattern across several gap reports or
  from two sources that disagree.
- **Stop** if the curator cannot pass the gate reliably without being tuned to these
  particular sources, or often changes playbooks no source touched. Then a person editing
  a playbook is cheaper and safer than a person reviewing a curator.

Which line holds is for the reader to judge from the runs. This page does not say.
"""

HOW = """\
## How a run is made

- **Frozen first.** [evidence/freeze.json](../evidence/freeze.json) holds the hashes of
  the curator's instructions and tools, every firm source, the held-out case set as the gate
  hashes it, the founding playbooks and desks, and the scenarios with their scripted triage
  replies. No run starts unless every one still holds. Freeze: `{freeze}`. The curator's
  instructions were changed once, on 2026-11-24, before this freeze. In the five live
  curator runs on the first cycle's sources before that (four on GPT-6.1 Terra at high, one
  on GPT-6.2 Sol at high), the curator listed the correction's disagreement with the
  founding memo and did not propose to stop holding the report for every missing
  cross-check. The rule it is told on which source wins was completed: where a correction
  or a gap report disagrees with a memo, the curator still proposes the change the
  correction or gap report supports, resting on it, and lists the disagreement, and the
  change goes into use only if the playbook owner rules for the correction or gap report.
- **Fresh records, the same inputs.** Each run starts the layer on fresh volumes. The memos
  are taken in, the dates triaged and the correction recorded as the scenario lists, with
  the triage agent's replies scripted, so that the curator of every run of a scenario is
  handed the same bundle. Those replies are no model's: the curator's answer to them is
  what is measured.
- **Live models.** The curator runs on `CURATOR_MODEL`. The triage agent is then started
  again on `TRIAGE_MODEL`, and the gate runs every check on what was proposed, the
  held-out cases three times each on that model among them. Neither may be Gemini 4 Neon,
  which wrote the held-out cases' expected notes. Nothing is approved. Each model is
  published as it was set, its reasoning effort with it where one was set; where none was,
  none was sent and the model's own default applied.
- **One set of held-out cases for every scenario.** The gate grades each scenario's
  proposal on the same held-out cases, written for the first cycle's sources; each run
  whose gate ran gives the set's hash. All four new-rule cases fail with the founding
  playbooks in use. In the live runs on this freeze, three of them passed only where the
  proposal stopped holding the report for a missing custody cross-check, as the correction
  and its gap report say and no memo does; with that hold left in place they fail on
  `hold_report`. The fourth passed only where custodian B's failed feed went to its service
  desk within 2 hours, as the 2026-10-19 memo says, not the 20:00 ET of the other desk's
  memo in `memos-that-disagree`. So in a scenario whose bundle holds neither the correction
  nor its gap report (`poisoned-memo`, `memos-that-disagree` and
  `pattern-across-gap-reports`), a proposal that changes only what its sources say cannot
  pass `held_out`: those three new-rule cases fail whatever else it gets right, and where
  the bundle holds no 2026-10-19 memo (`poisoned-memo`, `pattern-across-gap-reports`) the
  fourth fails too. In the live runs on this freeze, any other check failed, or any other
  case or exception missed, is the proposal's own.
- **Capped.** A curator run may spend two dollars, a triage trial two, and a gate's trials
  ten, each out of what is left of forty dollars for all fifteen runs, published runs
  included. No run starts with less than a dollar left, and no gate runs with nothing left
  after its curator: the run then stops and says so. A run's published caps are the ones
  its curator and its gate were given.
- **What is published.** Each run's record, kept as it goes: a run stopped part way is
  published as it stood, with the cap of what had not yet been counted charged as spent.
  The curator's words are published only where no rule on personal data or client
  identifiers refuses them; otherwise only the proposal's hash and the rule.
"""

COMMANDS = """\
## Making the runs

With `TRIAGE_MODEL`, `CURATOR_MODEL` and their providers' keys (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`) in `.env`, each model as `provider:model` or, with an OpenAI model's
reasoning effort, `provider:model:effort`:

```bash
make evidence
```

It runs every scenario's runs that are not yet published, in rounds (each scenario's first
run, then each one's second, then each one's third), writes each run to
`guide/evidence/runs/` as it ends, and writes this page again. Commit both. A run
stopped part way is published as it ended.

```bash
make evidence-rehearsal
```

makes the same runs with no key, on scripted replies only, and writes them under
`.local/evidence/`, never here. Its curator's replies are the scripted ones of
`recordings/scripted-curation.json`, which fit the first scenario's bundle alone, and its
trials replay `recordings/scripted-first-cycle-triage.json`, the scripted cycle's. It shows
that the command runs, and nothing of how a model curates.
"""


def _proposal_lines(proposal: dict) -> list[str]:
    lines = []
    for number, change in enumerate(proposal["changes"], start=1):
        of = f"version {change['version']}" if change["version"] is not None else "a new playbook"
        lines.append(
            f"  {number}. {change['change']} `{change['playbook']}/{change['step']}`, {of}"
        )
        if change["change"] != "remove":
            when = ", ".join(f"{k} {v}" for k, v in change["when"].items())
            hold = "hold the report" if change["hold_report"] else "do not hold the report"
            lines.append(
                f"     when {change['code']} ({when}): tell `{change['tell']}`, "
                f'{change["by"]}, {hold}. "{change["line"]}"'
            )
        lines.append(f"     rests on {', '.join(f'`{r}`' for r in change['rests_on'])}")
    for told in proposal["disagreements"]:
        between = " and ".join(f"`{r}`" for r in told["between"])
        lines.append(f'  - disagreement between {between}: "{told["about"]}"')
    return lines


KINDS = {"regression": "regression", "new_rule": "new-rule"}


def _run_lines(run: dict) -> list[str]:
    curated = run["curator_run"] or {}
    gate = run["gate"]
    read = run["readings"]
    lines = [
        f"#### Run {run['run']}: {OUTCOMES[run['outcome']]}",
        "",
        f"- Started {run['started_at']}, ended {run['finished_at']}. Cost ${run['cost_usd']}: "
        "each call's reported tokens at the price table's rates, or the most the call could "
        "have cost where the provider reported none"
        + (
            f", with ${run['charged_until_counted']} that {run['charged_for']} could spend "
            "charged as spent, as what it spent is not known."
            if Decimal(run["charged_until_counted"])
            else "."
        ),
        f"- Models: curator `{run['settings']['CURATOR_MODEL']}`, held-out trials "
        f"`{run['settings']['TRIAGE_MODEL']}`. Settings: "
        + ", ".join(
            f"{k} {v}"
            for k, v in run["settings"].items()
            if k not in ("CURATOR_MODEL", "TRIAGE_MODEL")
        )
        + ".",
    ]
    if run["stopped"]:
        lines.append(f"- Stopped: {run['stopped']}")
    for problem in run["setup_failed"]:
        lines.append(f"- Not set up: {problem}.")
    if run["curator_refused"]:
        lines.append(f"- The curator was not started: {run['curator_refused']}")
    if curated:
        tokens = (
            f"{curated['input_tokens']} tokens in, {curated['output_tokens']} out"
            if curated["input_tokens"] is not None
            else "no tokens reported"
        )
        lines.append(
            f"- Curator run: {curated['state']}, calls by "
            f"{', '.join(curated['calls_by']) or 'no model'}, {tokens}, "
            f"${curated['cost_usd'] or '0'}, {curated['seconds']} s. Bundle "
            f"`{curated['bundle']['sha256']}`."
        )
        if curated["failure"]:
            lines.append(f"- Its failure: {curated['failure']}")
        for line in curated["refused"]:
            lines.append(f"- A reply refused: {line}")
        if (proposal := curated["proposal"]) and "withheld" in proposal:
            lines.append(
                f"- Proposal `{proposal['sha256']}`: its words are not published, as they break "
                f"the rule {proposal['withheld']}."
            )
        elif proposal:
            lines.append(f"- Proposal `{proposal['sha256']}`:")
            lines += _proposal_lines(proposal)
    if run["gate_refused"]:
        lines.append(f"- The gate answered: {run['gate_refused']}")
    for refusal in run["gate_refusals"]:
        lines.append(f"- A gate refusal in the record: {refusal}")
    for trial in run["trials_in_no_gate_run"]:
        missed = "; ".join(f"{m['exception']} ({', '.join(m['fields'])})" for m in trial["misses"])
        lines.append(
            f"- A trial no finished gate run holds: {KINDS[trial['kind']]} case "
            f"`{trial['case']}` trial {trial['trial']}, {trial['state']}"
            f"{', ' + trial['failure'] if trial['failure'] else ''}"
            f"{': missed ' + missed if missed else ''}, ${trial['cost_usd']}"
        )
    if gate:
        lines.append(f"- Gate: {gate['outcome']}, {gate['seconds']} s.")
        for check in gate["checks"]:
            lines.append(f"  - `{check['check']}`: {check['outcome']}")
            for failure in check["failures"]:
                lines.append(f"    - {failure['line']} (rule `{failure['rule']}`)")
        held = gate["held_out"]
        if held:
            lines.append(
                f"  - held-out case set `{held['case_set_sha256']}`, expected notes by "
                f"{held['writer']}; trials on `{held['trial_model']}`, calls by "
                f"{', '.join(held['calls_by']) or 'no model'}, ${held['cost_usd']}"
            )
            for graded in held["graded"]:
                kind = KINDS[graded["kind"]]
                lines.append(
                    f"    - {kind} case `{graded['case']}`: {graded['in_use']} with the "
                    f"playbooks in use, {graded['proposed']} with the proposal"
                )
    watched = ", ".join(
        f"`{ref}` {'rested on' if used else 'not rested on'}"
        for ref, used in read["watched"].items()
    )
    lines.append(
        f"- Read from the record: playbooks changed "
        f"{', '.join(read['playbooks_changed']) or 'none'}; "
        f"{'rests on a gap report' if read['rests_on_a_gap_report'] else 'rests on no gap report'}"
        f"{'; ' + watched if watched else ''}; "
        f"{len(read['disagreements_listed'])} disagreement(s) listed; "
        f"{read['trials']} trials, {read['trials_missed']} missed."
    )
    return lines


def render(listed: list[Scenario], runs: list[dict], freeze: str) -> str:
    """The page: every published run under its scenario, misses included."""
    by_scenario = {s.id: [r for r in runs if r["scenario"] == s.id] for s in listed}
    made = sum(len(r) for r in by_scenario.values())
    out = [INTRODUCTION, HOW.format(freeze=freeze)]
    if any(r["origin"] != "live" for r in runs):
        out.append(
            "**A rehearsal.** These runs were made on scripted replies only. Nothing on this\n"
            "page is evidence of how a model curates.\n"
        )
    out.append("## What has been run\n")
    if made == 0:
        out.append(
            "**No evidence run has been made yet.** The one live run published so far is the\n"
            "recorded cycle's ([running.md](running.md#the-recorded-cycle)), which is no evidence\n"
            "run. The first live results, misses included, will be published on this page.\n"
        )
    else:
        spent = _cost(*(r["cost_usd"] for r in runs))
        rows = [
            "| Scenario | Run | Curator | Outcome | Checks failed | Trials missed | Cost |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for scenario in listed:
            for run in by_scenario[scenario.id]:
                read = run["readings"]
                rows.append(
                    f"| {scenario.id} | {run['run']} | `{run['settings']['CURATOR_MODEL']}` | "
                    f"{run['outcome']} | {', '.join(read['checks_failed']) or 'none'} | "
                    f"{read['trials_missed']} of {read['trials']} | ${run['cost_usd']} |"
                )
        out.append(
            f"{made} of {len(listed) * RUNS_OF_EACH} runs published, ${spent} spent in all.\n"
        )
        out.append("\n".join(rows) + "\n")
    out.append(COMMANDS)
    out.append("## The scenarios\n")
    for scenario in listed:
        out.append(f"### {scenario.title} (`{scenario.id}`)\n")
        out.append(scenario.about + "\n")
        out.append(
            f"- Memos taken in: {', '.join(f'`{m}`' for m in scenario.memos)}.\n"
            f"- Dates triaged: {', '.join(scenario.triage)}"
            f"{', and the committed correction recorded' if scenario.correction else ''}.\n"
            f"- Gap reports the scripted triage files: {scenario.gap_reports}.\n"
            f"- The triage agent's replies while it is set up: `{scenario.replies}`.\n"
        )
        runs_here = by_scenario[scenario.id]
        if not runs_here:
            out.append("Not run yet.\n")
        for run in runs_here:
            out.append("\n".join(_run_lines(run)) + "\n")
    return "\n".join(out)


def write_pages(folder: Path = PUBLISHED, page: Path = PAGE) -> None:
    """The page from the published runs, each of which must be live and on this freeze."""
    kept = json.loads(FREEZE.read_text(encoding="utf-8"))["sha256"]
    runs = published(folder)
    if any((r["origin"], r["freeze"]) != ("live", kept) for r in runs):
        raise Refused("A published run is a rehearsal's or rests on another freeze.")
    page.write_text(render(scenarios(), runs, kept), encoding="utf-8")


# In the containers


def _clients() -> tuple[httpx2.Client, httpx2.Client]:
    local = cycle.settings_in(cycle.LOCAL / "playbooks.env")

    def client(token: str) -> httpx2.Client:
        return httpx2.Client(
            base_url=local["PLAYBOOKS_URL"],
            headers={"Authorization": f"Bearer {token}"},
            timeout=300,
            trust_env=False,
        )

    return client(local["PLAYBOOKS_ANALYST_TOKEN"]), client(local["PLAYBOOKS_OWNER_TOKEN"])


def models(rehearse: bool) -> tuple[dict[str, str], dict[str, str]]:
    """The triage agent's and the curator's live settings from `.env`, never the writer."""
    if rehearse:
        return {"TRIAGE_MODEL": REHEARSAL_TRIALS}, {"CURATOR_MODEL": REHEARSAL_CURATOR}
    try:
        triaging, curating = cycle._from_env("TRIAGE_MODEL"), cycle._from_env("CURATOR_MODEL")
    except cycle.Stopped as stopped:
        raise Refused(str(stopped)) from None
    for setting in (triaging["TRIAGE_MODEL"], curating["CURATOR_MODEL"]):
        if cycle.WRITER in "".join(ch for ch in setting.lower() if ch.isalnum()):
            raise Refused("The held-out cases' writer may be neither the curator nor the trials'.")
    return triaging, curating


def _caps(left: Decimal) -> dict[str, str]:
    return {
        "CURATOR_MAX_DOLLARS": str(min(CURATOR_DOLLARS, left)),
        "TRIAGE_MAX_DOLLARS": str(min(TRIAL_DOLLARS, left)),
        "GATE_MAX_DOLLARS": str(min(GATE_DOLLARS, left)),
    }


def contained(
    scenario: Scenario,
    number: int,
    triaging: dict[str, str],
    curating: dict[str, str],
    left: Decimal,
    origin: Origin,
    freeze: str,
    keep: Callable[[dict], None],
) -> dict:
    """One run in the containers: a fresh layer set up on scripted triage, the curator on its
    model, and the triage agent on its own model for the gate's trials."""
    scripted = {"TRIAGE_MODEL": f"replay:{scenario.replies}"}
    caps = _caps(left)
    # Only a live run's agents are given a way out to a provider, and only the one whose model
    # is live at the time: the curator while the scenario is set up and curated, then the triage
    # agent too, for the gate's trials. Neither records its replies.
    live = origin == "live"
    cycle.start(
        live=False,
        cap=caps["TRIAGE_MAX_DOLLARS"],
        models=(scripted, curating),
        curator_cap=caps["CURATOR_MAX_DOLLARS"],
        gate_cap=caps["GATE_MAX_DOLLARS"],
        out=("curator",) if live else (),
    )
    # What the run was given, as published: the gate's caps are set again before it runs.
    settings = {
        "TRIAGE_MODEL": triaging["TRIAGE_MODEL"],
        "CURATOR_MODEL": curating["CURATOR_MODEL"],
        **caps,
    }

    def trials_model(curated: dict) -> Decimal:
        token = cycle.settings_in(cycle.LOCAL / "backend.env")["WEALTHCONTEXT_TOKEN"]
        # The gate's trials may spend what the curator left, and nothing past the whole.
        remaining = left - _cost(curated["cost_usd"])
        if remaining <= 0:
            settings["GATE_MAX_DOLLARS"] = settings["TRIAGE_MAX_DOLLARS"] = "0"
            raise NothingLeft(
                f"nothing is left of the ${ALL_DOLLARS} for the gate's trials, so the gate "
                "was not run"
            )
        gate_cap = min(GATE_DOLLARS, remaining)
        trial_cap = min(TRIAL_DOLLARS, gate_cap)
        settings["GATE_MAX_DOLLARS"], settings["TRIAGE_MAX_DOLLARS"] = str(gate_cap), str(trial_cap)
        cycle.write_settings(
            token,
            triaging,
            str(trial_cap),
            curating,
            curator_cap=caps["CURATOR_MAX_DOLLARS"],
            gate_cap=str(gate_cap),
        )
        cycle.run(
            *cycle.ours(
                "up",
                "--wait",
                "--no-deps",
                "--force-recreate",
                "backend",
                "triage-agent",
                out=("triage-agent", "curator") if live else (),
            )
        )
        return gate_cap

    analyst, owner = _clients()
    with analyst, owner:
        return run_once(
            analyst,
            owner,
            scenario,
            number,
            origin=origin,
            freeze=freeze,
            settings=settings,
            before_gate=trials_model,
            keep=keep,
        )


def _holds() -> None:
    if broken := freeze_breaks():
        raise Refused("The freeze does not hold, so no run starts: " + "; ".join(broken) + ".")


def make_runs(rehearse: bool) -> int:
    _holds()
    freeze = json.loads(FREEZE.read_text(encoding="utf-8"))["sha256"]
    folder = REHEARSED / "runs" if rehearse else PUBLISHED
    stale = [r for r in published(folder) if r["freeze"] != freeze]
    if stale:
        raise Refused(
            "Published runs rest on another freeze. Move them out before running on this one."
        )
    triaging, curating = models(rehearse)
    folder.mkdir(parents=True, exist_ok=True)
    origin: Origin = "rehearsal" if rehearse else "live"
    listed = scenarios()
    for number in range(1, RUNS_OF_EACH + 1):
        for scenario in listed:
            path = folder / run_name(scenario.id, number)
            if path.exists():
                continue
            left = ALL_DOLLARS - _cost(*(r["cost_usd"] for r in published(folder)))
            if left < LEAST_DOLLARS:
                print(f"Stopped: ${left} is left of ${ALL_DOLLARS}.", file=sys.stderr)
                return 1
            # Checked again before each run: what it reads must be what was frozen.
            _holds()
            print(f"{scenario.id}, run {number}", flush=True)
            page = REHEARSED / "evidence.md" if rehearse else PAGE

            def keep(record: dict, path: Path = path, page: Path = page) -> None:
                path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
                page.write_text(render(listed, published(folder), freeze), encoding="utf-8")

            made = contained(scenario, number, triaging, curating, left, origin, freeze, keep)
            print(f"  {made['outcome']}, ${made['cost_usd']}", flush=True)
            if made["stopped"]:
                print(f"Stopped: {made['stopped']}", file=sys.stderr)
                return 1
    cycle.unchanged()
    print("WealthContext's checkout is unchanged.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    how = parser.add_mutually_exclusive_group()
    how.add_argument("--freeze", action="store_true", help="write evidence/freeze.json")
    how.add_argument("--pages", action="store_true", help="write the page from published runs")
    how.add_argument("--rehearse", action="store_true", help="no key: scripted replies only")
    given = parser.parse_args()
    try:
        if given.freeze:
            print(f"Freeze: {write_freeze()}")
            return 0
        if given.pages:
            write_pages()
            return 0
        return make_runs(given.rehearse)
    except (Refused, cycle.Stopped) as stopped:
        print(stopped, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
