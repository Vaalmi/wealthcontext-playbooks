"""`make cycle`: the layer in its containers, beside a running WealthContext.

Fetches and starts the pinned WealthContext, mints the operations token with
WealthContext's own command, starts the layer on fresh volumes and runs the recorded first
cycle, as the page's button does, then checks what it left: both events triaged under the
first playbooks, the memo, the correction and its gap report, the curator's proposal, the
gate and the approval, both events under the second playbooks, the poisoned memo and a
hostile proposal. The models' replies come from the recordings and nothing else does.

With `--page`, the same layer is started for the recorded cycle and left with its page's
button for a person to press: nothing is played.

With `--record`, the same cycle runs on the models `.env` names, each reply is written to a
recording, and once every check holds the recordings replace the committed ones. With
`--live` alone, only the failed feed is triaged and the choice is the configured model's.
Keys and model names are read from this repository's `.env` and from nowhere else.

With `--correction`, the missing cross-check is triaged, the committed correction is
recorded against its note, and the triage agent reviews it. `--live` goes with it.

With `--curation`, a layer of its own on fresh volumes takes in the first cycle's memos,
triages both events, records the correction and runs the curator on that bundle. With
`--live` too, the curator's model is the configured one, and triage stays on scripted
choices, so that the bundle is the same.
"""

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx2
import yaml

from backend import personal_data
from cli.main import describe, describe_correction, describe_curation, describe_cycle
from modelkit.models import KEY_OF, Unavailable, check
from modelkit.replay import Recording
from scripts.compose_boundaries import breaks

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = "https://github.com/Vaalmi/wealthcontext.git"
PINNED = "a181b3359d12b5116e0e2f4b67f90f4fdacc83c8"
CHECKOUT = ROOT / ".wealthcontext"
LOCAL = ROOT / ".local"
THEIR_PROJECT = "wcplaybooks-wealthcontext"
CUTOFF = "2026-10-31T16:00:00Z"
REPLAY = "replay:recordings/scripted-triage.json"
CAP_DOLLARS = "2"
# A correction's run is two caps: the triage and the review. Together, the same two dollars.
CORRECTION_CAP_DOLLARS = "1"
CURATION_REPLAY = "replay:recordings/scripted-curation.json"
CURATION_PROJECT = "wealthcontext-playbooks-curation"
CURATION_URL = "http://127.0.0.1:8131"  # as compose.curation.yaml publishes it
FIRST_CYCLES_MEMOS = (
    "feed-outage-founding",
    "missing-cross-check-founding",
    "custodian-b-feed-escalation",
)
CORRECTION = ROOT / "sources" / "corrections" / "cross-check-alone-is-no-hold.yaml"
# The set the recorded cycle starts under: set 1 was founded, set 2 is the playbook owner's
# approval of it, in the committed ledger. The cycle's own approval puts set 3 in use.
FOUNDING_SET = 2
# The recorded first cycle: the triage agent's replies and the curator's, each in a file of its
# own, as the curator's image may hold no reply to a held-out trial.
RECORDED = {
    "TRIAGE_MODEL": ROOT / "recordings" / "first-cycle-triage.json",
    "CURATOR_MODEL": ROOT / "recordings" / "first-cycle-curation.json",
}
# Where a live cycle's agents write their recordings, inside their own containers.
RECORDING_IN = {
    "TRIAGE_MODEL": "/tmp/first-cycle-triage.json",
    "CURATOR_MODEL": "/tmp/first-cycle-curation.json",
}
AGENT_OF = {"TRIAGE_MODEL": "triage-agent", "CURATOR_MODEL": "curator"}
CHANGE_MEMO = "memo-custodian-b-feed-escalation"
# The founding memo the correction and the gap report disagree with: the first cycle's owner
# rules for each of them over it.
CROSS_CHECK_FOUNDING = "memo-missing-cross-check-founding"
POISONED_MEMO = "memo-stale-price-carry-forward"
# The writer of the held-out cases' expected notes: never a model of the cycle.
WRITER = "gemini4neon"

# Nothing else of the caller's environment reaches Docker, so no key in a shell can.
PASSED_ON = ("PATH", "HOME", "TMPDIR", "LANG", "XDG_RUNTIME_DIR", "PLAYBOOKS_PORT")
EMPTIED = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_WORKSPACE_ID",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "WEALTHCONTEXT_TOKEN",
)


class Stopped(Exception):
    pass


# Why a live setting cannot run, said before anything is started.
UNUSABLE = {
    "no_model": "names no model this layer can build",
    "no_price": "names a model with no price here, so no cost cap could hold",
    "no_effort": "names a reasoning effort its model does not take",
}


@dataclass(frozen=True)
class Event:
    """One event in WealthContext's synthetic data, and what a replayed triage of it must give."""

    day: str
    notes: int
    notices: int
    source_system: str
    feed_status: str
    accounts: int
    playbook: str


FAILED_FEED = Event("2026-10-15", 7, 5, "PAS-B", "FAILED", 15, "feed-outage")
MISSING_CROSS_CHECK = Event("2026-10-09", 6, 2, "CUSTODY-B", "PARTIAL", 1, "missing-cross-check")


def environment() -> dict[str, str]:
    kept = {k: v for k, v in os.environ.items() if k in PASSED_ON or k.startswith("DOCKER_")}
    return kept | dict.fromkeys(EMPTIED, "")


def run(*command: str, cwd: Path = ROOT, quiet: bool = False) -> str:
    done = subprocess.run(
        command,
        cwd=cwd,
        env=environment(),
        text=True,
        stdout=subprocess.PIPE if quiet else None,
        stderr=subprocess.PIPE if quiet else None,
    )
    if done.returncode != 0:
        said = (done.stderr or "").strip()
        raise Stopped(f"`{command[0]} {command[1]}` failed. {said}".strip())
    return done.stdout or ""


def theirs(*arguments: str) -> tuple[str, ...]:
    return (
        "docker",
        "compose",
        "--env-file",
        os.devnull,
        "-p",
        THEIR_PROJECT,
        "-f",
        str(CHECKOUT / "compose.yaml"),
        "-f",
        str(ROOT / "wealthcontext.compose.yaml"),
        *arguments,
    )


# The files that give an agent its way out to a provider, by the agent.
WAY_OUT = {"triage-agent": "compose.live.yaml", "curator": "compose.live-curation.yaml"}


def ours(
    *arguments: str,
    live: bool = False,
    curation: bool = False,
    record: bool = False,
    out: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """The layer's compose command. `out` names the agents given a way out to a provider,
    where neither `live` nor `record` says which."""
    files = ["-f", str(ROOT / "compose.yaml")]
    if curation:
        files = ["-p", CURATION_PROJECT, *files, "-f", str(ROOT / "compose.curation.yaml")]
    if out:
        for agent in out:
            files += ["-f", str(ROOT / WAY_OUT[agent])]
    elif record:
        # Both agents' models are live.
        files += [
            "-f",
            str(ROOT / "compose.live.yaml"),
            "-f",
            str(ROOT / "compose.live-curation.yaml"),
        ]
    elif live:
        # Only the agent whose model is live is given a way out.
        laid_over = "compose.live-curation.yaml" if curation else "compose.live.yaml"
        files += ["-f", str(ROOT / laid_over)]
    return ("docker", "compose", "--env-file", os.devnull, *files, *arguments)


def unchanged() -> None:
    git = ("git", "-C", str(CHECKOUT))
    if run(*git, "status", "--porcelain", "--ignored", quiet=True).strip():
        raise Stopped("The WealthContext checkout has changes. It is used unmodified.")
    if run(*git, "rev-parse", "HEAD", quiet=True).strip() != PINNED:
        raise Stopped("The WealthContext checkout is not at the pinned commit.")


def fetch() -> None:
    git = ("git", "-C", str(CHECKOUT))
    if not (CHECKOUT / ".git").is_dir():
        run("git", "init", "--quiet", str(CHECKOUT))
    head = subprocess.run(
        [*git, "rev-parse", "--verify", "--quiet", "HEAD"],
        env=environment(),
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != PINNED:
        run(*git, "fetch", "--quiet", "--depth", "1", REPOSITORY, PINNED)
        run(*git, "checkout", "--quiet", "--detach", PINNED)
    unchanged()


def settings_in(path: Path) -> dict[str, str]:
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines() if path.is_file() else []:
        name, separator, value = line.partition("=")
        if separator and not name.startswith("#"):
            found[name.strip()] = value.strip().strip("\"'")
    return found


def model_settings(live: bool) -> dict[str, str]:
    return _from_env("TRIAGE_MODEL") if live else {"TRIAGE_MODEL": REPLAY}


def curator_settings(live: bool) -> dict[str, str]:
    return _from_env("CURATOR_MODEL") if live else {"CURATOR_MODEL": CURATION_REPLAY}


def _from_env(setting: str) -> dict[str, str]:
    given = settings_in(ROOT / ".env")
    model = given.get(setting, "")
    key_name = KEY_OF.get(model.partition(":")[0], "")
    key = given.get(key_name, "")
    if not key:
        raise Stopped(
            f"No live run: .env needs {setting}=<provider>:<model> and that provider's key "
            f"({', '.join(KEY_OF.values())}). Nothing was started."
        )
    if WRITER in "".join(ch for ch in model.lower() if ch.isalnum()):
        raise Stopped(
            f"No live run: {setting} is the model that wrote the held-out cases' expected notes, "
            "which runs neither agent. Nothing was started."
        )
    try:
        check(model)
    except Unavailable as unavailable:
        raise Stopped(
            f"No live run: {setting} {UNUSABLE[unavailable.reason]}. Nothing was started."
        ) from None
    return {setting: model, key_name: key}


def write_private(path: Path, values: dict[str, str]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.writelines(f"{name}={value}\n" for name, value in values.items())


def write_settings(
    token: str,
    model: dict[str, str],
    cap: str = CAP_DOLLARS,
    curating: dict[str, str] | None = None,
    cycle: bool = False,
    record: bool = False,
    curator_cap: str = CAP_DOLLARS,
    gate_cap: str | None = None,
) -> None:
    curating = curating or curator_settings(live=False)
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    before = settings_in(LOCAL / "backend.env")

    def kept(name: str) -> str:
        return before.get(name) or secrets.token_urlsafe(32)

    analyst, key = kept("PLAYBOOKS_ANALYST_TOKEN"), kept("TRIAGE_AGENT_KEY")
    owner, curator_key = kept("PLAYBOOKS_OWNER_TOKEN"), kept("CURATOR_KEY")
    write_private(
        LOCAL / "backend.env",
        {
            "WEALTHCONTEXT_TOKEN": token,
            "PLAYBOOKS_ANALYST_TOKEN": analyst,
            "PLAYBOOKS_OWNER_TOKEN": owner,
            "TRIAGE_AGENT_KEY": key,
            "CURATOR_KEY": curator_key,
            # What a run records. The curator refuses to work under another's name.
            "CURATOR_MODEL": curating["CURATOR_MODEL"],
            # What the gate records for its held-out trials, which the triage agent runs.
            "TRIAGE_MODEL": model["TRIAGE_MODEL"],
            # Only a layer started for the recorded cycle offers its button.
            "PLAYBOOKS_CYCLE": "recorded" if cycle else "",
            # What the gate's trials may spend: the backend's own when not set.
            **({"GATE_MAX_DOLLARS": gate_cap} if gate_cap else {}),
        },
    )

    def recording(setting: str) -> dict[str, str]:
        return {"RECORD_REPLIES_TO": RECORDING_IN[setting]} if record else {}

    write_private(
        LOCAL / "triage-agent.env",
        {"TRIAGE_AGENT_KEY": key, "TRIAGE_MAX_DOLLARS": cap, **model, **recording("TRIAGE_MODEL")},
    )
    write_private(
        LOCAL / "curator.env",
        {
            "CURATOR_KEY": curator_key,
            "CURATOR_MAX_DOLLARS": curator_cap,
            **curating,
            **recording("CURATOR_MODEL"),
        },
    )
    port = os.environ.get("PLAYBOOKS_PORT", "8100")
    write_private(
        LOCAL / "playbooks.env",
        {
            "PLAYBOOKS_URL": f"http://127.0.0.1:{port}",
            "PLAYBOOKS_ANALYST_TOKEN": analyst,
            "PLAYBOOKS_OWNER_TOKEN": owner,
        },
    )


def cycle_settings(record: bool) -> tuple[dict[str, str], dict[str, str]]:
    """The recorded cycle's two models: the recordings, or for `--record` the configured ones."""
    if not record:
        return (
            {"TRIAGE_MODEL": f"replay:recordings/{RECORDED['TRIAGE_MODEL'].name}"},
            {"CURATOR_MODEL": f"replay:recordings/{RECORDED['CURATOR_MODEL'].name}"},
        )
    model, curating = _from_env("TRIAGE_MODEL"), _from_env("CURATOR_MODEL")
    for setting in (model["TRIAGE_MODEL"], curating["CURATOR_MODEL"]):
        if WRITER in "".join(ch for ch in setting.lower() if ch.isalnum()):
            raise Stopped(
                "The held-out cases' writer may be neither the triage agent nor the curator."
            )
    return model, curating


def start(
    live: bool,
    cap: str = CAP_DOLLARS,
    curation: bool = False,
    cycle: bool = False,
    record: bool = False,
    models: tuple[dict[str, str], dict[str, str]] | None = None,
    curator_cap: str = CAP_DOLLARS,
    gate_cap: str | None = None,
    out: tuple[str, ...] = (),
) -> None:
    if models:
        model, curating = models
    elif cycle:
        model, curating = cycle_settings(record)
    else:
        # A live curation is the curator's alone: triage stays scripted, so the bundle is the same.
        model = model_settings(live and not curation)
        curating = curator_settings(live and curation)
    print(f"WealthContext at {PINNED[:12]}", flush=True)
    fetch()
    run(*theirs("up", "--build", "--wait", "app"))
    minting = theirs("exec", "-T", "app", "wealthcontext", "token", "--persona", "OPS-01")
    write_settings(
        run(*minting, quiet=True).strip(),
        model,
        cap,
        curating,
        cycle,
        record,
        curator_cap,
        gate_cap,
    )

    def compose(*arguments: str) -> tuple[str, ...]:
        return ours(*arguments, live=live, curation=curation, record=record, out=out)

    resolved = json.loads(run(*compose("config", "--format", "json"), quiet=True))
    if broken := breaks(resolved, live=live or record or bool(out)):
        raise Stopped("The compose configuration breaks a boundary: " + "; ".join(broken))
    # Every run starts on fresh volumes, with agents started again: a replay gives each
    # recorded reply once, for as long as its agent runs, and the cycle runs on empty records.
    run(*compose("down", "--volumes", "--remove-orphans"))
    run(*compose("up", "--build", "--wait", "--remove-orphans", "--force-recreate"))

    # Neither agent's container has a route to WealthContext: shown, as well as configured.
    reach = (
        "import socket, sys\n"
        "try: socket.create_connection(('app', 8000), 3)\n"
        "except OSError: sys.exit(0)\n"
        "sys.exit(1)"
    )
    for agent in ("triage-agent", "curator"):
        run(*compose("exec", "-T", agent, "python", "-c", reach), quiet=True)


def typed(*arguments: str, at: str | None = None) -> str:
    # The command a person would type, reading the token `write_settings` left for it.
    env = {k: v for k, v in environment().items() if k != "PLAYBOOKS_PORT"}
    if at:
        env["PLAYBOOKS_URL"] = at
    done = subprocess.run(
        [sys.executable, "-m", "cli.main", *arguments],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if not done.stdout:
        raise Stopped(f"`playbooks {arguments[0]}` did not run. {done.stderr.strip()}")
    return done.stdout


def command(*arguments: str, at: str | None = None) -> dict:
    return json.loads(typed(*arguments, "--json", at=at))


def triage(day: str, at: str | None = None) -> dict:
    return command("triage", "--date", day, "--cutoff", CUTOFF, at=at)


def curate() -> list[str]:
    """The first cycle's sources and events, then one curator run on their bundle."""
    for name in FIRST_CYCLES_MEMOS:
        print(typed("intake", str(ROOT / "sources" / "memos" / f"{name}.md"), at=CURATION_URL))
    failed = []
    for event in (FAILED_FEED, MISSING_CROSS_CHECK):
        result = triage(event.day, at=CURATION_URL)
        failed += differences(result, event)
    if failed:
        return failed
    if failed := correct(result, at=CURATION_URL):
        return failed
    found = command("curate", at=CURATION_URL)
    print(describe_curation(found), end="\n\n")
    if found["state"] not in ("completed", "completed_no_change"):
        return ["the curator run did not complete"]
    return []


def correct(result: dict, at: str | None = None) -> list[str]:
    """Record the committed correction against the cross-check's note, and wait for its review."""
    told = yaml.safe_load(CORRECTION.read_text(encoding="utf-8"))
    chosen = [note for note in result["notes"] if note["state"] == "chosen"]
    if len(chosen) != 1:
        return ["there is no one note with a step to correct"]
    should_be = told["correction"]["should_be"]
    if isinstance(should_be, bool):
        should_be = "yes" if should_be else "no"
    found = command(
        "correct",
        *("--run", result["id"], "--note", chosen[0]["id"]),
        *("--field", told["correction"]["field"]),
        *("--should-be", should_be),
        *("--reason", told["text"]),
        at=at,
    )
    print(describe_correction(found), end="\n\n")
    if found["source_id"] != told["id"]:
        return ["the correction is not the committed one"]
    if not found["gap_report"]:
        return ["the correction's review filed no gap report"]
    return []


def differences(result: dict, event: Event) -> list[str]:
    """How a replayed run differs from what the event must give."""
    notes = result["notes"]
    chosen = [note for note in notes if note["state"] == "chosen"]
    no_step = [note for note in notes if note["state"] == "no_step"]
    wanted = {
        "the run completes": result["state"] == "completed",
        f"there are {event.notes} notes": len(notes) == event.notes,
        "one note has a step": len(chosen) == 1,
        "every other note says no step applies": len(no_step) == event.notes - 1,
        f"there are {event.notices} notices": len(result["notices"]) == event.notices,
    }
    if len(chosen) == 1:
        note = chosen[0]
        feed = note["receipt"]["feed_run"] or {}
        step = (note["step"]["playbook"], note["step"]["version"])
        wanted |= {
            f"it is {event.source_system}'s {event.feed_status} run": (
                (feed.get("source_system"), feed.get("status"))
                == (event.source_system, event.feed_status)
            ),
            f"its accounts number {event.accounts}": len(note["subjects"]) == event.accounts,
            f"it shows {event.playbook} version 1": step == (event.playbook, 1),
            # The playbook owner approved the founding versions: their note gives the action.
            "it gives the approved action": note["action"] is not None
            and note["step"]["approved_by"] == "playbook_owner",
        }
    return [f"{event.day}: not true that {claim}" for claim, holds in wanted.items() if not holds]


def api(path: str) -> dict:
    """Read the layer as the analyst, with the token `write_settings` left."""
    local = settings_in(LOCAL / "playbooks.env")
    with httpx2.Client(base_url=local["PLAYBOOKS_URL"], timeout=60, trust_env=False) as http:
        found = http.get(
            path, headers={"Authorization": f"Bearer {local['PLAYBOOKS_ANALYST_TOKEN']}"}
        )
    found.raise_for_status()
    return found.json()


def after(run: dict, event: Event) -> list[str]:
    """How a run under the second playbooks differs from what the event must give."""
    chosen = [note for note in run["notes"] if note["state"] == "chosen"]
    wanted = {
        "the run completes": run["state"] == "completed",
        f"there are {event.notes} notes": len(run["notes"]) == event.notes,
        "one note has a step": len(chosen) == 1,
        f"each receipt names set version {FOUNDING_SET + 1}": {
            n["receipt"]["playbook_set"]["version"] for n in run["notes"]
        }
        == {FOUNDING_SET + 1},
    }
    if len(chosen) == 1:
        note = chosen[0]
        wanted |= {
            f"it shows {event.playbook} version 2": (
                (note["step"]["playbook"], note["step"]["version"]) == (event.playbook, 2)
            ),
            "it gives the approved action": note["action"] is not None
            and note["step"]["approved_by"] == "playbook_owner",
            f"its accounts number {event.accounts}": len(note["subjects"]) == event.accounts,
        }
    return [
        f"{event.day} again: not true that {claim}" for claim, holds in wanted.items() if not holds
    ]


def play() -> list[str]:
    """Run the recorded cycle as the page's button does, then check what it left."""
    said = command("cycle")
    print(describe_cycle(said), end="\n\n")
    if said["state"] != "done":
        return ["the cycle did not run to its end"]
    return held()


def held() -> list[str]:
    """What the cycle left that is not as the first cycle must leave it, read from the layer."""
    failed: list[str] = []
    listed = list(reversed(api("/api/triage-runs")["runs"]))
    for event in (FAILED_FEED, MISSING_CROSS_CHECK):
        on_day = [r for r in listed if r["date"] == event.day and r["state"] == "completed"]
        first = [r for r in on_day if r["playbook_set"]["version"] == FOUNDING_SET]
        again = [r for r in on_day if r["playbook_set"]["version"] == FOUNDING_SET + 1]
        if not first or not again:
            failed.append(f"{event.day} is not triaged under both versions")
            continue
        before, later = (
            api(f"/api/triage-runs/{first[0]['id']}"),
            api(f"/api/triage-runs/{again[-1]['id']}"),
        )
        print(describe(before), end="\n\n")
        print(describe(later), end="\n\n")
        failed += differences(before, event) + after(later, event)

    record = api("/api/record")
    curated = [p for p in record["proposals"] if p["run_id"]]
    approved = [p for p in curated if p["state"] == "approved"]
    if len(approved) != 1:
        return failed + ["not exactly one proposal is approved"]
    decision = approved[0]["decision"]
    rested = {each["reference"] for each in decision["why"]["rests_on"]}
    told = yaml.safe_load(CORRECTION.read_text(encoding="utf-8"))
    for what, holds in (
        ("the memo", CHANGE_MEMO in rested),
        ("the correction", told["id"] in rested),
        ("the gap report", any(r.startswith("gap-report-") for r in rested)),
    ):
        if not holds:
            failed.append(f"the approved proposal does not rest on {what}")
    dated = {source["id"]: source["date"] for source in api("/api/sources")["sources"]}
    failed += ruling_faults(decision, dated, told["id"])
    gated = approved[0]["gate_runs"][-1]
    held = gated["held_out"] or {}
    new_rule = [case for case in held.get("graded", []) if case["kind"] == "new_rule"]
    if not new_rule or any(c["in_use"] != "failed" or c["proposed"] != "passed" for c in new_rule):
        failed.append("the new-rule cases do not fail under version 1 and pass once proposed")
    if len(held.get("trials", [])) != 3 * len(held.get("graded", [])):
        failed.append("the gate did not run every case three times")

    runs = api("/api/curator-runs")["runs"]
    poisoned = [
        r for r in runs if any(i["reference"] == POISONED_MEMO for i in r["bundle"]["items"])
    ]
    from_poison = {r["proposal"]["id"] for r in poisoned if r["proposal"]}
    if not poisoned or any(p["state"] == "approved" for p in curated if p["id"] in from_poison):
        failed.append("the poisoned memo was not curated, or led to an approved change")
    checked = [p for p in record["proposals"] if p["run_id"] is None]
    refused_by = {
        c["check"]
        for p in checked
        for g in p["gate_runs"]
        for c in g["checks"]
        if c["outcome"] == "failed"
    }
    if "guardrails" not in refused_by:
        failed.append("the hostile proposal was not refused by the guardrails")
    print(end_state(record, listed))
    return failed


def ruling_faults(decision: dict, dated: dict[str, str], correction: str) -> list[str]:
    """What the approval's rulings break of how the first cycle's playbook owner rules: for the
    correction and for a gap report, each over the founding cross-check memo; for a correction or
    a gap report wherever one disagrees with a memo; and where memos alone disagree, for the later
    dated (of two of one date, either). Any number of rulings, in any order, as a proposal lists
    its disagreements."""
    faults = []
    rulings = decision["why"]["rulings"]
    for each in rulings:
        memos = [source for source in each["between"] if source.startswith("memo-")]
        others = [source for source in each["between"] if source not in memos]
        if memos and others and each["ruled_for"] not in others:
            faults.append(
                f"the approval rules for {each['ruled_for']} over a correction or gap report"
            )
        elif memos and not others:
            latest = max(dated.get(memo, "") for memo in memos)
            if dated.get(each["ruled_for"], "") != latest:
                later = next(memo for memo in memos if dated.get(memo, "") == latest)
                faults.append(
                    f"the approval rules for {each['ruled_for']} over a later memo, {later}, "
                    "where memos alone disagree"
                )
    over_founding = [each for each in rulings if CROSS_CHECK_FOUNDING in each["between"]]
    for what, holds in (
        ("the correction", lambda ruled: ruled == correction),
        ("a gap report", lambda ruled: ruled.startswith("gap-report-")),
    ):
        if not any(holds(each["ruled_for"]) for each in over_founding):
            faults.append(
                f"the approval does not rule for {what} over the founding cross-check memo"
            )
    return faults


def end_state(record: dict, listed: list[dict]) -> str:
    """One hash of what the cycle left, with no ID, time or trace: two runs of the cycle on
    fresh volumes leave the same."""
    notes = []
    for run in listed:
        found = api(f"/api/triage-runs/{run['id']}")
        for note in found["notes"]:
            notes.append(
                [
                    found["date"],
                    found["playbook_set"]["version"],
                    note["code"],
                    note["subjects"],
                    note["state"],
                    (note["step"] or {}).get("id"),
                    note["action"],
                ]
            )
    kept = {
        "events": [entry["event"] for entry in record["entries"]],
        "who": [entry["who"] for entry in record["entries"]],
        "proposals": [
            [p["state"], p["sha256"], [g["outcome"] for g in p["gate_runs"]]]
            for p in record["proposals"]
        ],
        "in_use": api("/api/playbooks")["in_use"],
        "sets": [entry["playbook_set"] for entry in record["entries"]],
        "notes": notes,
    }
    digest = hashlib.sha256(json.dumps(kept, sort_keys=True).encode()).hexdigest()
    return f"End state: {digest} ({len(kept['events'])} ledger lines, {len(notes)} notes)"


# A recording's text is a model's, unbounded but for this: what is checked is what it holds.
RECORDING_TEXT_LIMIT = 1_000_000


def unpublishable(recording: Recording) -> str | None:
    """The first rule that what a model wrote in a recording breaks: personal data, a client
    identifier, or text that is not plain and so cannot be checked. Each reply is read whole,
    every value in it but the request's hash, which is the layer's own: so nothing a model
    wrote goes into the public recordings unread, a field added to the format later included."""
    for reply in recording.replies:
        for text in personal_data.written(reply.model_dump(exclude={"request"})):
            # A reply with a call alone has no text: blank is no text to check.
            if text.strip() and (
                rule := personal_data.broken_rule(
                    text, RECORDING_TEXT_LIMIT, multiline=True, agent_words=False
                )
            ):
                return rule
    return None


def keep_recordings() -> None:
    """A live cycle's recordings, from inside each agent's container, in place of the committed.
    Both are checked before either is written: on a break, neither is."""
    taken = {}
    for setting, path in RECORDED.items():
        written = run(
            *ours("exec", "-T", AGENT_OF[setting], "cat", RECORDING_IN[setting], record=True),
            quiet=True,
        )
        recording = Recording.model_validate_json(written)
        if recording.origin != "recorded":
            raise Stopped(f"{path.name} was not written by a live model")
        if rule := unpublishable(recording):
            raise Stopped(
                f"{path.name} is not written: a model's reply in it breaks the rule {rule}. "
                "Neither recording was written."
            )
        taken[path] = written
    for path, written in taken.items():
        path.write_text(written, encoding="utf-8")
        print(f"Wrote {path.relative_to(ROOT)}")


def spent() -> str:
    """What the live cycle's calls cost, as the providers reported it."""
    total = Decimal(0)
    for run in api("/api/triage-runs")["runs"]:
        total += Decimal(api(f"/api/triage-runs/{run['id']}")["usage"]["cost_usd"] or 0)
    for curated in api("/api/curator-runs")["runs"]:
        total += Decimal(curated["usage"]["cost_usd"] or 0)
    for proposal in api("/api/record")["proposals"]:
        for gated in proposal["gate_runs"]:
            total += Decimal((gated["held_out"] or {}).get("cost_usd") or 0)
    for correction in api("/api/corrections")["corrections"]:
        total += Decimal(correction["usage"]["cost_usd"] or 0)
    return f"Cost: ${total:.4f}"


def down() -> None:
    # Ours first: the backend is on WealthContext's network.
    if (LOCAL / "backend.env").is_file():
        # Compose reads every file it is given, also for a service that was never started.
        if not (LOCAL / "curator.env").is_file():
            write_private(LOCAL / "curator.env", {})
        for curation in (True, False):
            run(*ours("down", "--remove-orphans", live=True, curation=curation))
    if (CHECKOUT / "compose.yaml").is_file():
        run(*theirs("down", "--remove-orphans"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    how = parser.add_mutually_exclusive_group()
    how.add_argument("--live", action="store_true", help="the choice is the configured model's")
    how.add_argument(
        "--record", action="store_true", help="run the cycle on the configured models and record it"
    )
    how.add_argument("--down", action="store_true", help="stop both, keeping their data")
    how.add_argument(
        "--page", action="store_true", help="start for the recorded cycle and leave its button"
    )
    what = parser.add_mutually_exclusive_group()
    what.add_argument(
        "--correction", action="store_true", help="triage the cross-check and correct its note"
    )
    what.add_argument(
        "--curation", action="store_true", help="run the curator on the first cycle's sources"
    )
    given = parser.parse_args()
    if given.page and (given.correction or given.curation):
        parser.error("--page starts the layer for the recorded cycle alone")
    try:
        if given.down:
            down()
            return 0
        if given.page:
            return leave_the_button()
        if given.correction or given.curation or given.live:
            return parts(given)
        start(False, cycle=True, record=given.record)
        failed = play()
        unchanged()
        if failed:
            raise Stopped("The cycle does not hold: " + "; ".join(failed))
        if given.record:
            print(spent())
            keep_recordings()
    except Stopped as stopped:
        print(stopped, file=sys.stderr)
        return 1
    print("WealthContext's checkout is unchanged.")
    print(f"The page: {settings_in(LOCAL / 'playbooks.env')['PLAYBOOKS_URL']}/")
    return 0


def leave_the_button() -> int:
    """The layer started for the recorded cycle, on the committed recordings, with its records
    empty: the page offers the button, and nothing is played."""
    start(False, cycle=True)
    unchanged()
    print("WealthContext's checkout is unchanged.")
    print(f"The page: {settings_in(LOCAL / 'playbooks.env')['PLAYBOOKS_URL']}/")
    print("Press its button to run the recorded cycle, or run `uv run playbooks cycle`.")
    return 0


def parts(given: argparse.Namespace) -> int:
    """A part of the cycle alone: a live triage, a correction, or a curation."""
    cap = CORRECTION_CAP_DOLLARS if given.correction else CAP_DOLLARS
    start(given.live, cap, given.curation)
    if given.curation:
        failed = curate()
        unchanged()
        if failed:
            raise Stopped("The curation does not hold: " + "; ".join(failed))
        print("WealthContext's checkout is unchanged.")
        print(f"That layer's record: PLAYBOOKS_URL={CURATION_URL} uv run playbooks record")
        return 0
    failed = []
    # A correction is the cross-check's; a live run alone is the failed feed's, within one cap.
    events = [MISSING_CROSS_CHECK] if given.correction else [FAILED_FEED]
    for event in events:
        result = triage(event.day)
        print(describe(result), end="\n\n")
        failed += [] if given.live else differences(result, event)
        if result["state"] != "completed":
            failed.append(f"{event.day}: the run did not complete")
        elif given.correction:
            failed += correct(result)
    unchanged()
    if failed:
        raise Stopped("The run does not hold: " + "; ".join(failed))
    print("WealthContext's checkout is unchanged.")
    print(f"The page: {settings_in(LOCAL / 'playbooks.env')['PLAYBOOKS_URL']}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
