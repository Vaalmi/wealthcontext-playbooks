"""`playbooks`: the command people use."""

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx2
import yaml

TOKEN_OF = {"analyst": "PLAYBOOKS_ANALYST_TOKEN", "owner": "PLAYBOOKS_OWNER_TOKEN"}
# The backend's list. The command imports nothing of the backend's.
RETIRE_REASONS = ("source_withdrawn", "wrong_action", "no_longer_applies", "put_in_use_in_error")
DISMISS_REASONS = ("not_a_gap", "duplicate", "already_covered", "out_of_scope")
REFUSE_REASONS = ("wrong_change", "against_the_sources", "source_not_trusted", "not_needed")
CONFIRM = "Type the proposal's hash to confirm: "
NOT_CONFIRMED = "That is not the proposal's hash. Nothing was approved, and nothing changed."
CORRECTED_FIELDS = ("tell", "by", "hold_report")
YES_OR_NO = {"yes": True, "no": False}
REVIEW_NOT_HEARD = (
    "The triage agent did not say whether it started the review. If nothing is heard for five "
    "minutes, `playbooks request-review` asks again."
)
CURATION_NOT_HEARD = (
    "The curator did not say whether it started. If nothing is heard for five minutes, "
    "the same command starts the run again."
)
CURATION_ENDED = ("completed", "completed_no_change")
DEFAULT_URL = "http://127.0.0.1:8100"
LOCAL_SETTINGS = Path(".local/playbooks.env")
NOT_HEARD = (
    "The triage agent did not say whether it started. If nothing is heard for five minutes, "
    "the same command starts the run again."
)


class CommandFailed(Exception):
    pass


def _settings(person: str) -> tuple[str, str]:
    found: dict[str, str] = {}
    if LOCAL_SETTINGS.is_file():
        for line in LOCAL_SETTINGS.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and not name.startswith("#"):
                found[name.strip()] = value.strip()
    found.update({k: v for k, v in os.environ.items() if k.startswith("PLAYBOOKS_")})
    return found.get("PLAYBOOKS_URL", DEFAULT_URL), found.get(TOKEN_OF[person], "")


# A gate runs every held-out case three times on the triage agent.
GATE_SECONDS = 1800


def _answer(
    http: httpx2.Client,
    method: str,
    path: str,
    body: dict | None = None,
    timeout: float | None = None,
) -> dict:
    try:
        if timeout is None:
            response = http.request(method, path, json=body)
        else:
            response = http.request(method, path, json=body, timeout=timeout)
    except httpx2.HTTPError:
        raise CommandFailed("The backend cannot be reached. Is the layer running?") from None
    try:
        answered = response.json()
    except ValueError:
        answered = {}
    if response.status_code >= 400:
        line = answered.get("detail", "The backend refused.")
        raise CommandFailed(f"{response.status_code}: {line}")
    return answered


def triage(http: httpx2.Client, day: str, cutoff: str, wait: float) -> dict:
    started = _answer(http, "POST", "/api/triage-runs", {"date": day, "cutoff": cutoff})
    if started["attempt"] > 1:
        print(f"Started the stalled run again: attempt {started['attempt']}.", file=sys.stderr)
    if started["asked"] == "unknown":
        print(NOT_HEARD, file=sys.stderr)
    until = time.monotonic() + wait
    while True:
        run = _answer(http, "GET", f"/api/triage-runs/{started['id']}")
        if run["state"] != "working" or run["stalled"] or time.monotonic() >= until:
            return run
        time.sleep(0.25)


def describe(run: dict[str, Any]) -> str:
    lines = [f"Triage of {run['date']}, cutoff {run['cutoff']}: {run['state']}"]
    lines.append(f"  run {run['id']}")
    if run["failure"]:
        lines.append(f"  {run['failure']}")
    if run["stalled"]:
        lines.append(
            "  It is stalled: nothing has been heard for five minutes. "
            "The same command starts it again."
        )
    for number, note in enumerate(run["notes"], start=1):
        lines.append(f"\n{number}. {note['code']}: {', '.join(note['subjects'])}")
        if note["receipt"]["feed_run"]:
            feed = note["receipt"]["feed_run"]
            lines.append(f"   feed run: {feed['source_system']} {feed['feed']} {feed['status']}")
        if step := note["step"]:
            lines.append(f"   step: {step['playbook']} version {step['version']}, {step['id']}")
            lines.append(f"   note {note['id']}")
        if action := note["action"]:
            hold = "hold" if action["hold_report"] else "do not hold"
            lines.append(f"   tell {action['desk']}, by {action['by']}")
            lines.append(f"   {hold} the advisor's report (an instruction to a person)")
            lines.append(f"   {action['line']}")
        else:
            lines.append(f"   {note['statement'] or 'The choice is pending.'}")
    for taken in run["steps"]:
        if taken["tool"] == "file_gap_report" and taken["outcome"] == "accepted":
            lines.append(f"\nGap report filed: {taken['option']}")
    lines.append(f"\n{len(run['notices'])} informational notices, with no note:")
    lines.extend(f"  {notice['code']}: {notice['subject']}" for notice in run["notices"])
    if run["notes"]:
        receipt = run["notes"][0]["receipt"]
        used = receipt["playbook_set"]
        lines.append(f"\nTriage receipt: {used['id']} version {used['version']}, {used['hash']}")
        lines.extend(
            f"  {read['operation']}: result {read['result_id']}, trace {read['trace_id']}"
            for read in receipt["reads"]
        )
    usage = run["usage"]
    lines.append(f"\nChoices by: {', '.join(usage['models']) or 'no model call'}")
    if usage["input_tokens"] is not None:
        lines.append(f"Tokens: {usage['input_tokens']} in, {usage['output_tokens']} out")
    if usage["cost_usd"] is not None:
        lines.append(f"Cost: ${usage['cost_usd']}")
    if run["finished_at"]:
        ended = datetime.fromisoformat(run["finished_at"])
        taken = ended - datetime.fromisoformat(run["started_at"])
        lines.append(f"Time: {taken.total_seconds():.1f} s")
    return "\n".join(lines)


def retire(http: httpx2.Client, playbook: str, version: int, reason: str) -> str:
    # Quoted whole, so that what was typed cannot name another operation.
    path = f"/api/playbooks/{quote(playbook, safe='')}/versions/{version}/retire"
    done = _answer(http, "POST", path, {"reason": reason})
    if done["now_in_use"] is None:
        then = f"No version of {done['playbook']} is in use."
    else:
        then = f"Version {done['now_in_use']} is in use again."
    used = done["playbook_set"]
    return (
        f"Retired {done['playbook']} version {done['version']}. {then}\n"
        f"Playbook set: {used['id']} version {used['version']}, {used['hash']}"
    )


def intake(http: httpx2.Client, file: Path) -> str:
    try:
        # Its bytes as they are, so that the hash taken is the file's.
        content = file.read_bytes().decode()
    except (OSError, ValueError):
        raise CommandFailed("The file cannot be read as text.") from None
    taken = _answer(http, "POST", "/api/sources", {"content": content})
    scope = taken["scope"]
    shown = scope if isinstance(scope, str) else json.dumps(scope, sort_keys=True)
    return (
        f"Taken in: {taken['id']}, {taken['kind']} of {taken['date']} from {taken['desk']}\n"
        f"Scope: {shown}\n"
        f"Hash: {taken['sha256']}"
    )


def correct(
    http: httpx2.Client,
    run: str,
    note: str,
    field: str,
    should_be: str,
    reason: str,
    wait: float,
) -> dict:
    if field == "hold_report" and should_be not in YES_OR_NO:
        raise CommandFailed("A hold instruction should be yes or no.")
    value = YES_OR_NO[should_be] if field == "hold_report" else should_be
    path = f"/api/triage-runs/{quote(run, safe='')}/notes/{quote(note, safe='')}/corrections"
    body = {"change": {"field": field, "should_be": value}, "reason": reason}
    recorded = _answer(http, "POST", path, body)
    if recorded["asked"] == "not_started":
        why = f"{recorded['why']} The correction stands. "
        print(f"{why}`playbooks request-review` asks again.", file=sys.stderr)
    return reviewed(http, recorded, wait)


def request_review(http: httpx2.Client, correction: str, wait: float) -> dict:
    path = f"/api/corrections/{quote(correction, safe='')}/request-review"
    return reviewed(http, _answer(http, "POST", path), wait)


def reviewed(http: httpx2.Client, recorded: dict, wait: float) -> dict:
    if recorded["asked"] == "unknown":
        print(REVIEW_NOT_HEARD, file=sys.stderr)
    until = time.monotonic() + wait
    while True:
        found = _answer(http, "GET", f"/api/corrections/{recorded['id']}")
        waiting = found["state"] == "recorded" and found["asked"] and not found["stalled"]
        if not waiting or time.monotonic() >= until:
            return found
        time.sleep(0.25)


def describe_correction(found: dict[str, Any]) -> str:
    scope, value = found["scope"], found["should_be"]
    if isinstance(value, bool):
        value = "yes" if value else "no"
    lines = [
        f"Correction {found['id']}: {found['state']}",
        f"  firm source {found['source_id']}",
        f"  against {scope['playbook']} version {found['version']}, {found['step']}",
        f"  {found['field']} should be {value}: {found['reason']}",
    ]
    if found["gap_report"]:
        lines.append(f"  gap report filed: {found['gap_report']}")
    elif found["outcome"] == "none":
        lines.append("  the triage agent filed no gap report")
    elif found["outcome"] == "refused_twice":
        lines.append("  no gap report: the triage agent's answer was refused twice")
    elif found["stopped"]:
        lines.append(f"  its review stopped: {found['stopped']} It can be asked for again.")
    elif not found["asked"]:
        lines.append("  nobody is reviewing it: it can be asked for again")
    elif found["stalled"]:
        lines.append("  its review is stalled: it can be asked for again")
    usage = found["usage"]
    if usage["models"]:
        lines.append(f"  reviewed by: {', '.join(usage['models'])}")
    if usage["input_tokens"] is not None:
        lines.append(f"  tokens: {usage['input_tokens']} in, {usage['output_tokens']} out")
    if usage["cost_usd"] is not None:
        lines.append(f"  cost: ${usage['cost_usd']}")
    if found["reviewed_at"]:
        ended = datetime.fromisoformat(found["reviewed_at"])
        taken = ended - datetime.fromisoformat(found["recorded_at"])
        lines.append(f"  time: {taken.total_seconds():.1f} s")
    return "\n".join(lines)


def describe_gap_reports(found: dict[str, Any]) -> str:
    lines = []
    for each in found["gap_reports"]:
        used = f"{each['playbook']} version {each['version']}" if each["playbook"] else "none"
        lines.append(f"{each['id']}: {each['state']}, {each['reference']}")
        lines.append(f"   playbook: {used}; warning code {each['code']}")
        lines.append(f"   trace: {each['trace_id']}")
        lines.append(f"   {each['summary']}")
        if each["proposal_id"]:
            how = "resolved" if each["state"] == "resolved" else "cited"
            lines.append(f"   {how} by proposal: {each['proposal_id']}")
        if each["dismissed"]:
            lines.append(f"   dismissed: {each['dismissed']}")
    return "\n".join(lines) or "No gap report."


def describe_record(record: dict[str, Any]) -> str:
    lines = ["Proposals:"]
    for proposal in record["proposals"]:
        origin = f"curator run {proposal['run_id']}" if proposal["run_id"] else "handed in"
        lines.append(f"  {proposal['id']}: {proposal['state']}, {origin}")
        lines.append(f"    hash {proposal['sha256']}")
        if proposal["content"] is None:
            lines.append("    its text is not kept: it carried what the checks keep out")
        for run in proposal["gate_runs"]:
            failed = [each["check"] for each in run["checks"] if each["outcome"] == "failed"]
            said = f", failed: {', '.join(failed)}" if failed else ""
            lines.append(f"    {run['kind']} {run['id']}: {run['outcome']}{said}")
        for refused in proposal["gate_refusals"]:
            lines.append(f"    gate refused at {refused['at']} ({refused['reason']})")
            lines.append(f"      {refused['line']}")
        if decided := proposal["decision"]:
            lines += describe_decision(decided)
    if not record["proposals"]:
        lines.append("  none")
    lines.append("\nLedger:")
    for entry in record["entries"]:
        what = entry["event"].replace("_", " ")
        lines.append(f"{entry['line']}. {entry['at']}: {what}, by {entry['who']}")
        for name, value in sorted(entry["data"].items()):
            for each in value if isinstance(value, list) else [value]:
                if each is None:
                    shown = "none"
                elif isinstance(each, str):
                    shown = each
                else:
                    shown = json.dumps(each, sort_keys=True)
                lines.append(f"   {name.replace('_', ' ')}: {shown}")
        if used := entry["playbook_set"]:
            lines.append(f"   playbook set: {used['id']} version {used['version']}, {used['hash']}")
        lines.append(f"   ledger line's hash: {entry['hash']}")
    return "\n".join(lines)


DECIDED = {
    "approved": "approved",
    "refused": "refused",
    "stale": "stale, its approval refused: the playbooks in use changed after its gate ran",
}


def describe_decision(decided: dict[str, Any]) -> list[str]:
    """An approval or a refusal: what changed, why, on what evidence and who."""
    changed, why, evidence = decided["what_changed"], decided["why"], decided["evidence"]
    head = DECIDED[decided["decision"]]
    lines = [f"    decision: {head}, at {decided['at']}, ledger line {decided['line']}"]
    approved = decided["decision"] == "approved"
    lines.append(f"    what {'changed' if approved else 'it would have changed'}:")
    for change in changed["changes"]:
        of = "a new playbook" if change["version"] is None else f"version {change['version']}"
        lines.append(f"      {change['change']} {change['playbook']}/{change['step']}, {of}")
        if change["change"] != "remove":
            lines.append(f"        {_when(change)}")
            lines.append(f"        {_action(change, change['tell'])}")
            lines.append(f"        {change['line']}")
    for each in changed["versions"]:
        lines.append(f"      in use: {each['id']} version {each['version']}, {each['sha256']}")
    lines.append("    why:")
    for each in why["rests_on"]:
        kind = each["kind"].replace("_", " ")
        lines.append(f"      rests on {kind} {each['reference']}, {each['sha256']}")
    for ruling in why["rulings"]:
        lines.append(f"      ruled for {_ruling(ruling)}")
    if why["reason"]:
        lines.append(f"      reason: {why['reason']}")
    lines.append("    evidence:")
    if evidence["gate_run"]:
        lines.append(f"      gate run {evidence['gate_run']}: {evidence['outcome']}")
        for check in evidence["checks"]:
            lines.append(f"      {check['check']}: {SHOWN.get(check['outcome'], check['outcome'])}")
    else:
        lines.append("      the gate did not run on it, so no check has a result")
    lines.append(f"    who: {decided['who']}")
    return lines


def _action(step: dict[str, Any], desk: str) -> str:
    hold = "hold" if step["hold_report"] else "do not hold"
    return f"tell {desk}, by {step['by']}, {hold} the advisor's report"


def _ruling(ruling: dict[str, Any]) -> str:
    on = f" on change {ruling['change']}" if ruling["change"] else ""
    return f"{ruling['ruled_for']}{on}, between {', '.join(ruling['between'])}"


def _when(step: dict[str, Any]) -> str:
    conditions = ", ".join(f"{name} {value}" for name, value in step["when"].items())
    return f"when {step['code']}: {conditions}"


def _quoted(cited: dict[str, Any]) -> list[str]:
    """A source a proposal cites, and its exact words, line for line."""
    about = [cited["kind"].replace("_", " ")]
    if cited["date"]:
        about.append(f"of {cited['date']}")
    if cited["desk"]:
        about.append(f"from {cited['desk']}")
    lines = [f"     {cited['reference']} ({' '.join(about)}), hash {cited['sha256']}"]
    if told := cited["correction"]:
        value = told["should_be"]
        if isinstance(value, bool):
            value = "yes" if value else "no"
        lines.append(f"       corrects {told['step']}: {told['field']} should be {value}")
    lines.extend(f"       | {each}".rstrip() for each in cited["words"].split("\n"))
    return lines


def describe_proposal(shown: dict[str, Any]) -> str:
    """The changed steps, each next to the exact words of each source it cites."""
    lines = [f"Proposal {shown['id']}: {shown['state']}", f"  hash {shown['sha256']}"]
    ran = shown["gate_run"]
    if ran["gate_run"]:
        used = ran["playbook_set"]
        lines.append(f"  gate run {ran['gate_run']}: {ran['outcome']}")
        lines.append(f"  against playbook set {used['id']} version {used['version']}")
        for check in ran["checks"]:
            lines.append(f"    {check['check']}: {SHOWN.get(check['outcome'], check['outcome'])}")
    for number, each in enumerate(shown["changes"], start=1):
        change, before = each["change"], each["before"]
        of = "a new playbook" if change["version"] is None else f"version {change['version']}"
        named = f"{change['playbook']}/{change['step']}"
        lines.append(f"\n{number}. {change['change']} {named}, {of}")
        if before:
            lines.append(f"   in version {before['version']}: {_when(before)}")
            if before["approval_recorded"]:
                lines.append(f"     {_action(before, before['desk'])}")
            else:
                lines.append("     no approval is recorded for it, so its action is not shown")
            lines.append(f"     {before['line']}")
        if change["change"] == "remove":
            lines.append("   proposed: no such step")
        else:
            lines.append(f"   proposed: {_when(change)}")
            lines.append(f"     {_action(change, each['desk'] or change['tell'])}")
            lines.append(f"     {change['line']}")
        lines.append("   the exact words of what it rests on:")
        for cited in each["cites"]:
            lines += _quoted(cited)
    for told in shown["disagreements"]:
        lines.append(f"\nDisagreement: {told['about']}")
        for cited in told["sources"]:
            lines += _quoted(cited)
    for pair in shown["found_disagreements"]:
        lines.append(f"The gate found that these disagree: {', '.join(pair)}")
    if shown["to_rule_on"]:
        lines.append("\nApproving needs your ruling on each of these disagreements:")
        for number, each in enumerate(shown["to_rule_on"], start=1):
            on = f"on change {each['change']}, " if each["change"] else ""
            lines.append(f"  {number}: {on}between {', '.join(each['between'])}")
        lines.append("Name the source you rule for on each, as --rule-for <number>=<source>.")
    return "\n".join(lines)


def ruled(shown: dict[str, Any], rule_for: list[str]) -> list[dict]:
    """Each `<number>=<source>` as the ruling on the disagreement the command numbered so."""
    numbered = {str(n): each for n, each in enumerate(shown["to_rule_on"], start=1)}
    made = []
    for given in rule_for:
        number, _, source = given.partition("=")
        if number not in numbered or source not in numbered[number]["between"]:
            raise CommandFailed(
                "A --rule-for is <number>=<source>: a disagreement's number as listed, and a "
                "source in that one. Nothing was approved."
            )
        made.append(numbered[number] | {"ruled_for": source})
    return made


def approve(http: httpx2.Client, proposal: str, rule_for: list[str]) -> str:
    """Show what approving would change, ask for the hash, and only then send the approval."""
    path = f"/api/proposals/{quote(proposal, safe='')}"
    shown = _answer(http, "GET", path)
    print(describe_proposal(shown))
    if shown["state"] != "gate_passed":
        raise CommandFailed(
            f"The proposal is {shown['state']}: only one that passed the gate is approved, "
            "so this changes nothing."
        )
    rulings = ruled(shown, rule_for)
    for ruling in rulings:
        print(f"You rule for: {_ruling(ruling)}")
    print(f"\n{describe_trials(_gate_run(http, proposal, shown['gate_run']['gate_run']))}")
    print(f"\n{CONFIRM}", end="", flush=True)
    typed = sys.stdin.readline().strip()
    print()
    if typed != shown["sha256"]:
        raise CommandFailed(NOT_CONFIRMED)
    body = {"sha256": typed, "gate_run": shown["gate_run"]["gate_run"], "rulings": rulings}
    return describe_approved(_answer(http, "POST", f"{path}/approve", body))


REPLAYED = (
    "That is a replay: the trials' replies were read from a file, and no live model ran these "
    "trials. A real promotion should rest on held-out trials run on a live model; approving on a "
    "replay is still allowed."
)


def replayed(model: str) -> bool:
    """Whether a trials' model, as the gate run records its setting, is a replay of a file."""
    provider, _, named = model.partition(":")
    return provider == "replay" and bool(named)


def _gate_run(http: httpx2.Client, proposal: str, run_id: str) -> dict[str, Any]:
    """The gate run as the record keeps it, with the held-out trials it ran."""
    for listed in _answer(http, "GET", "/api/record")["proposals"]:
        if listed["id"] == proposal:
            for run in listed["gate_runs"]:
                if run["id"] == run_id:
                    return run
    raise CommandFailed(f"The record holds no gate run {run_id}. Nothing was approved.")


def describe_trials(run: dict[str, Any]) -> str:
    """The model the gate run's held-out trials ran on, and plainly when it was a replay."""
    held = run["held_out"]
    said = f"The held-out trials of gate run {run['id']} ran on {held['trial_model']}."
    return f"{said}\n{REPLAYED}" if replayed(held["trial_model"]) else said


def describe_approved(done: dict[str, Any]) -> str:
    lines = [f"Approved proposal {done['proposal_id']}, ledger line {done['line']}."]
    for each in done["versions"]:
        lines.append(f"  in use: {each['id']} version {each['version']}, {each['sha256']}")
    for ruling in done["rulings"]:
        lines.append(f"  ruled for {_ruling(ruling)}")
    lines.extend(f"  gap report resolved: {each}" for each in done["resolved"])
    used = done["playbook_set"]
    lines.append(f"Playbook set: {used['id']} version {used['version']}, {used['hash']}")
    return "\n".join(lines)


FOUNDING_CONFIRM = "Type the founding line's hash to confirm: "


def describe_founding(shown: dict[str, Any]) -> str:
    """Each founding version whole, beside the exact words of the memo it rests on."""
    lines = [f"The founding line's hash: {shown['hash']}"]
    if shown["approved_by"]:
        when = f" at {shown['approved_at']}" if shown["approved_at"] else ""
        lines.append(f"  approved by {shown['approved_by']}{when}")
    for version in shown["versions"]:
        named = f"{version['id']} version {version['version']}"
        lines.append(f"\n{named}: {version['state']}, hash {version['sha256']}")
        if not version["as_named"]:
            lines.append("  its file is not as the founding line names it")
        lines.extend(f"  | {each}".rstrip() for each in version["text"].rstrip("\n").split("\n"))
        for memo in version["rests_on"]:
            lines.append(f"  it rests on {memo['id']}, hash {memo['sha256']}")
            if memo["words"] is None:
                lines.append("    no memo with that ID is here")
                continue
            if not memo["as_named"]:
                lines.append("    the memo here is not the one the founding line names")
            lines.extend(f"    | {each}".rstrip() for each in memo["words"].split("\n"))
    desks = shown["desks"]
    lines.append(f"\nThe desks, hash {desks['sha256']}")
    if not desks["as_named"]:
        lines.append("  the desks' file is not as the founding line names it")
    lines.extend(f"  | {each}".rstrip() for each in desks["text"].rstrip("\n").split("\n"))
    return "\n".join(lines)


def approve_founding(http: httpx2.Client) -> str:
    """Show the founding versions beside their memos' words, ask for the hash, then approve."""
    shown = _answer(http, "GET", "/api/founding")
    print(describe_founding(shown))
    if shown["approved_by"]:
        raise CommandFailed("The founding versions are approved already, so this changes nothing.")
    print(f"\n{FOUNDING_CONFIRM}", end="", flush=True)
    typed = sys.stdin.readline().strip()
    print()
    if typed != shown["hash"]:
        raise CommandFailed(
            "That is not the founding line's hash. Nothing was approved, and nothing changed."
        )
    done = _answer(http, "POST", "/api/founding/approve", {"sha256": typed})
    lines = [f"Approved the founding versions, ledger line {done['line']}."]
    for each in done["versions"]:
        lines.append(f"  {each['id']} version {each['version']}, {each['sha256']}")
    used = done["playbook_set"]
    lines.append(f"Playbook set: {used['id']} version {used['version']}, {used['hash']}")
    return "\n".join(lines)


def refuse(http: httpx2.Client, proposal: str, reason: str) -> str:
    path = f"/api/proposals/{quote(proposal, safe='')}/refuse"
    done = _answer(http, "POST", path, {"reason": reason})
    lines = [
        f"Refused proposal {done['proposal_id']}: {done['reason']}, ledger line {done['line']}."
    ]
    lines.extend(f"  gap report open again: {each}" for each in done["reopened"])
    return "\n".join(lines)


def proposal_file(file: Path) -> dict:
    """A proposal file's changes and disagreements. A hostile case says more, which is not sent."""
    try:
        found = yaml.safe_load(file.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        raise CommandFailed("The file cannot be read as a proposal in YAML or JSON.") from None
    if not isinstance(found, dict):
        raise CommandFailed("The file holds no proposal: it needs changes and disagreements.")
    sent = {name: found[name] for name in ("changes", "disagreements") if name in found}
    try:
        json.dumps(sent)
    except (TypeError, ValueError):
        # YAML reads an unquoted date or time as one: quote it.
        raise CommandFailed(
            "The file holds a value that is no text, number or yes or no."
        ) from None
    return sent


SHOWN = {"not_run": "not run: a check above failed"}


def describe_held_out(held: dict[str, Any]) -> list[str]:
    lines = [
        f"  held-out case set: {held['case_set_sha256']}, expected notes by {held['writer']}",
        f"  trials' model: {held['trial_model']}"
        + (f" (calls named {', '.join(held['models'])})" if held["models"] else ""),
    ]
    for each in held["graded"]:
        lines.append(
            f"    {each['kind'].replace('_', '-')} case {each['case']}: "
            f"{each['in_use']} with the playbooks in use, {each['proposed']} with the proposal"
        )
    trials = held["trials"]
    if trials:
        missed = [t for t in trials if t["state"] != "completed" or t["misses"]]
        lines.append(
            f"  trials: {len(trials)} run, {len(missed)} missed, "
            f"cost ${held['cost_usd']} as the provider reported it"
        )
    return lines


def describe_gate_run(run: dict[str, Any]) -> str:
    what = "Gate run" if run["kind"] == "gate" else "Check"
    used = run["playbook_set"]
    lines = [
        f"{what} {run['id']} of proposal {run['proposal_id']}: {run['outcome']}",
        f"  by {run['who']} at {run['at']}",
        f"  playbook set: {used['id']} version {used['version']}, {used['hash']}",
        f"  bundle: {run['bundle_sha256']}",
    ]
    for result in run["checks"]:
        lines.append(f"  {result['check']}: {SHOWN.get(result['outcome'], result['outcome'])}")
        for failure in result["failures"]:
            lines.append(f"    {failure['line']} (rule {failure['rule']})")
    for first, second in run["disagreements"]:
        lines.append(f"  sources that disagree about an outcome it changes: {first} and {second}")
    if run.get("held_out"):
        lines += describe_held_out(run["held_out"])
    if run["outcome"] == "passed" and run["kind"] == "gate":
        lines.append("  Every check passed: the proposal is gate_passed.")
    return "\n".join(lines)


def describe_checked(checked: dict[str, Any]) -> str:
    kept = "its text kept" if checked["content_kept"] else "only its hash kept"
    head = f"Proposal {checked['id']}: checked, {kept}, and never to be approved"
    return f"{head}\n  hash {checked['sha256']}\n{describe_gate_run(checked['gate_run'])}"


def curate(http: httpx2.Client, wait: float) -> dict:
    started = _answer(http, "POST", "/api/curator-runs")
    if started["attempt"] > 1:
        print(f"Started the stalled run again: attempt {started['attempt']}.", file=sys.stderr)
    if started["asked"] == "unknown":
        print(CURATION_NOT_HEARD, file=sys.stderr)
    until = time.monotonic() + wait
    while True:
        found = _answer(http, "GET", f"/api/curator-runs/{started['id']}")
        if found["state"] != "working" or found["stalled"] or time.monotonic() >= until:
            return found
        time.sleep(0.25)


def describe_curation(found: dict[str, Any]) -> str:
    manifest = found["bundle"]
    lines = [f"Curator run {found['id']}: {found['state']}"]
    lines.append(f"  bundle {manifest['sha256']}")
    lines.extend(
        f"    {item['kind']} {item['reference']}: {item['sha256']}" for item in manifest["items"]
    )
    if found["failure"]:
        lines.append(f"  {found['failure']}")
    if found["stalled"]:
        lines.append(
            "  It is stalled: nothing has been heard for five minutes. "
            "The same command starts it again."
        )
    if proposal := found["proposal"]:
        in_use = "" if proposal["state"] == "approved" else ", not in use"
        lines.append(f"\nProposal {proposal['id']}: {proposal['state']}{in_use}")
        lines.append(f"  hash {proposal['sha256']}")
        for number, change in enumerate(proposal["changes"], start=1):
            of = "a new playbook"
            if change["version"] is not None:
                of = f"version {change['version']}"
            named = f"{change['playbook']}/{change['step']}"
            lines.append(f"  {number}. {change['change']} {named}, {of}")
            if change["change"] != "remove":
                conditions = ", ".join(f"{k} {v}" for k, v in change["when"].items())
                hold = "hold" if change["hold_report"] else "do not hold"
                lines.append(f"     when {change['code']}: {conditions}")
                lines.append(f"     proposed: tell {change['tell']}, {change['by']}, {hold}")
                lines.append(f"     {change['line']}")
            lines.append(f"     rests on {', '.join(change['rests_on'])}")
        for told in proposal["disagreements"]:
            lines.append(f"  disagreement between {', '.join(told['between'])}: {told['about']}")
    elif found["state"] == "completed_no_change":
        lines.append("\nNo change, and no proposal.")
    for taken in found["steps"]:
        if taken["outcome"] == "refused":
            lines.append(f"  refused: {taken['line']}")
    usage = found["usage"]
    lines.append(f"\nCurator's configured model: {found['model']}")
    lines.append(f"Calls by: {', '.join(usage['models']) or 'no model call'}")
    if usage["input_tokens"] is not None:
        lines.append(f"Tokens: {usage['input_tokens']} in, {usage['output_tokens']} out")
    if usage["cost_usd"] is not None:
        lines.append(f"Cost: ${usage['cost_usd']}")
    if found["finished_at"]:
        ended = datetime.fromisoformat(found["finished_at"])
        taken_for = ended - datetime.fromisoformat(found["started_at"])
        lines.append(f"Time: {taken_for.total_seconds():.1f} s")
    return "\n".join(lines)


STEPS = (
    "The failed feed and the missing cross-check, triaged under the first playbooks",
    "What arrived since: the memo, the person's correction, and the agent's gap report",
    "The curator's proposal",
    "The gate, then the playbook owner's approval",
    "Both dates triaged again under the second playbooks",
    "The poisoned memo and a hostile proposal",
)


def cycle(http: httpx2.Client, wait: float) -> dict:
    """Press the page's button, then follow the cycle step by step until it ends."""
    try:
        pressed = http.post("/cycle", follow_redirects=False)
    except httpx2.HTTPError:
        raise CommandFailed("The backend cannot be reached. Is the layer running?") from None
    if pressed.status_code != 303:
        try:
            line = pressed.json().get("detail", "The backend refused.")
        except ValueError:
            line = "The backend refused."
        raise CommandFailed(f"{pressed.status_code}: {line}")
    until, shown = time.monotonic() + wait, 0
    while True:
        state = _answer(http, "GET", "/api/cycle")
        # The step it is at has ended only once the whole cycle is done.
        while shown < state["step"] - (state["state"] != "done"):
            print(f"  {shown + 1}. {STEPS[shown]}: done", file=sys.stderr)
            shown += 1
        if state["state"] != "running" or time.monotonic() >= until:
            return state
        time.sleep(0.25)


def describe_cycle(state: dict[str, Any]) -> str:
    match state["state"]:
        case "done":
            return f"The recorded cycle ran from {state['started_at']} to {state['finished_at']}."
        case "running":
            return f"The recorded cycle is still running, at step {state['step']}."
        case _:
            return f"The recorded cycle stopped at step {state['step']}: {state['line']}"


def _commands() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="playbooks")
    commands = parser.add_subparsers(dest="command", required=True)

    def added(name: str, person: str, what: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=what)
        command.add_argument(
            "--as",
            dest="person",
            choices=sorted(TOKEN_OF),
            default=person,
            help=f"whose token to send (default: {person})",
        )
        return command

    command = added("triage", "analyst", "triage one date, or start its stalled run again")
    command.add_argument("--date", required=True)
    command.add_argument("--cutoff", required=True)
    command.add_argument("--wait", type=float, default=300, help="seconds to wait for the run")
    command.add_argument("--json", action="store_true", help="print the run as JSON")
    command = added("retire", "owner", "retire the version in use; the one before it is used again")
    command.add_argument("--playbook", required=True)
    command.add_argument("--version", required=True, type=int)
    command.add_argument("--reason", required=True, choices=RETIRE_REASONS)
    command = added("record", "analyst", "list what happened to the firm's playbooks")
    command.add_argument("--json", action="store_true", help="print the record as JSON")
    command = added("intake", "owner", "take a memo in as a firm source, with its scope")
    command.add_argument("file", type=Path)
    command = added("correct", "analyst", "correct a note's step; the agent then reviews it")
    command.add_argument("--run", required=True, help="the run, as `triage` prints it")
    command.add_argument("--note", required=True, help="the note, as `triage` prints it")
    command.add_argument("--field", required=True, choices=CORRECTED_FIELDS)
    command.add_argument("--should-be", required=True, help="a desk, a deadline, or yes or no")
    command.add_argument("--reason", required=True, help="one line, at most 300 characters")
    command.add_argument("--wait", type=float, default=300, help="seconds to wait for the review")
    command.add_argument("--json", action="store_true", help="print the correction as JSON")
    command = added("request-review", "analyst", "ask again for a correction's review")
    command.add_argument("--correction", required=True)
    command.add_argument("--wait", type=float, default=300, help="seconds to wait for the review")
    command.add_argument("--json", action="store_true", help="print the correction as JSON")
    command = added("gap-reports", "analyst", "list the gap reports and their states")
    command.add_argument("--json", action="store_true", help="print the gap reports as JSON")
    command = added("dismiss", "owner", "dismiss an open gap report")
    command.add_argument("--gap-report", required=True)
    command.add_argument("--reason", required=True, choices=DISMISS_REASONS)
    command = added("curate", "owner", "run the curator on a bundle of what has been taken in")
    command.add_argument("--wait", type=float, default=300, help="seconds to wait for the run")
    command.add_argument("--json", action="store_true", help="print the run as JSON")
    command = added("gate", "analyst", "run every check on a drafted proposal")
    command.add_argument("--proposal", required=True, help="the proposal, as `curate` prints it")
    command.add_argument("--json", action="store_true", help="print the gate run as JSON")
    command = added("check", "analyst", "run the fixed-rule checks on a proposal file")
    command.add_argument("file", type=Path, help="a proposal in YAML or JSON")
    command.add_argument("--json", action="store_true", help="print the result as JSON")
    command = added("approve", "owner", "read a proposal next to its sources, then confirm it")
    command.add_argument("--proposal", required=True, help="the proposal, as `curate` prints it")
    command.add_argument(
        "--rule-for",
        action="append",
        default=[],
        metavar="NUMBER=SOURCE",
        help="the source you rule for on a disagreement as numbered; give it once for each",
    )
    added("approve-founding", "owner", "read the founding versions whole, then approve them")
    command = added("refuse", "owner", "refuse a proposal that is not yet decided")
    command.add_argument("--proposal", required=True, help="the proposal, as `curate` prints it")
    command.add_argument("--reason", required=True, choices=REFUSE_REASONS)
    added("rebuild", "owner", "make the database again from the ledger and the triage log")
    command = added("cycle", "analyst", "run the recorded cycle, as the page's button does")
    command.add_argument("--wait", type=float, default=1800, help="seconds to wait for it")
    command.add_argument("--json", action="store_true", help="print its end as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    given = _commands().parse_args(argv)
    url, token = _settings(given.person)
    succeeded = True
    try:
        with httpx2.Client(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=300,
            trust_env=False,
        ) as http:
            match given.command:
                case "triage":
                    run = triage(http, given.date, given.cutoff, given.wait)
                    said = json.dumps(run, indent=2) if given.json else describe(run)
                    succeeded = run["state"] == "completed"
                case "retire":
                    said = retire(http, given.playbook, given.version, given.reason)
                case "record":
                    found = _answer(http, "GET", "/api/record")
                    said = json.dumps(found, indent=2) if given.json else describe_record(found)
                case "intake":
                    said = intake(http, given.file)
                case "correct" | "request-review":
                    if given.command == "correct":
                        found = correct(
                            http,
                            given.run,
                            given.note,
                            given.field,
                            given.should_be,
                            given.reason,
                            given.wait,
                        )
                    else:
                        found = request_review(http, given.correction, given.wait)
                    said = json.dumps(found, indent=2) if given.json else describe_correction(found)
                    succeeded = found["state"] == "reviewed"
                case "gap-reports":
                    found = _answer(http, "GET", "/api/gap-reports")
                    said = (
                        json.dumps(found, indent=2) if given.json else describe_gap_reports(found)
                    )
                case "dismiss":
                    path = f"/api/gap-reports/{quote(given.gap_report, safe='')}/dismiss"
                    done = _answer(http, "POST", path, {"reason": given.reason})
                    said = f"Dismissed gap report {done['id']}: {done['dismissed']}."
                case "gate":
                    path = f"/api/proposals/{quote(given.proposal, safe='')}/gate-runs"
                    # Its held-out trials take a model call or more each.
                    found = _answer(http, "POST", path, timeout=GATE_SECONDS)
                    said = json.dumps(found, indent=2) if given.json else describe_gate_run(found)
                    succeeded = found["outcome"] == "passed"
                case "check":
                    sent = proposal_file(given.file)
                    found = _answer(http, "POST", "/api/checked-proposals", sent)
                    said = json.dumps(found, indent=2) if given.json else describe_checked(found)
                    succeeded = found["gate_run"]["outcome"] == "passed"
                case "approve":
                    said = approve(http, given.proposal, given.rule_for)
                case "refuse":
                    said = refuse(http, given.proposal, given.reason)
                case "approve-founding":
                    said = approve_founding(http)
                case "curate":
                    found = curate(http, given.wait)
                    said = json.dumps(found, indent=2) if given.json else describe_curation(found)
                    succeeded = found["state"] in CURATION_ENDED
                case "cycle":
                    found = cycle(http, given.wait)
                    said = json.dumps(found, indent=2) if given.json else describe_cycle(found)
                    succeeded = found["state"] == "done"
                case _:  # rebuild: the parser takes no other command
                    done = _answer(http, "POST", "/api/rebuild")
                    said = (
                        f"Rebuilt the database from {done['ledger']} ledger lines "
                        f"and {done['triage_log']} triage log lines."
                    )
    except CommandFailed as failed:
        print(failed, file=sys.stderr)
        return 1
    print(said)
    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
