"""Held-out case sets made for the tests, never the repository's own.

Each case's inputs are made here from the recorded envelopes of 2026-10-15 and 2026-10-09,
in WealthContext's shapes, on days and accounts of their own. Their expected notes are this
file's, written from the first cycle's firm sources: a failed feed of custodian B goes
to its service desk within 2 hours, and a missing cross-check alone holds no report.
"""

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from layer import ROOT

from backend import exceptions
from backend.wealthcontext import read_envelopes

RECORDED = ROOT / "tests" / "fixtures" / "wealthcontext"
CUTOFF = "2026-10-31T16:00:00Z"
WRITER = {"name": "A writer of these tests", "model": "test-writer"}


def _load(day: str, name: str) -> dict:
    return json.loads((RECORDED / day / f"{name}.json").read_text())


QUALITY = _load("2026-10-15", "get_data_quality_report")
RUNS = _load("2026-10-15", "feed_runs")
IMPACTS = {
    "pas_snapshots": _load("2026-10-15", "feed_run_impact"),
    "custody_snapshots": _load("2026-10-09", "feed_run_impact"),
}


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _listed(like: dict, items: list) -> dict:
    return {**like, "items": items, "item_count": len(items), "total_item_count": len(items)}


def _household(subject: str) -> str:
    return f"WC-HH-{(int(subject.split('-')[2].split(':')[0]) + 1) // 2:04d}"


def _made(case: str, name: str, envelope: dict, day: str) -> dict:
    envelope = copy.deepcopy(envelope)
    envelope.update(
        trace_id=_h(f"{case} trace {name}")[:32],
        result_id="r_" + _h(f"{case} result {name}")[:32],
        as_of=day,
        valuation_date=day,
        known_as_of=CUTOFF,
        quality_flags=[],
        status="COMPLETE",
    )
    return envelope


def envelopes(case: str, day: str, failing: list[tuple], flags: list[dict]) -> dict[str, Any]:
    """A day's three kinds of envelope. `failing` is (system, feed, status, accounts) each."""
    runs, impacts = [], {}
    for each in RUNS["data"]["items"]:
        run = copy.deepcopy(each) | {"business_date": day, "status": "SUCCESS"}
        run["id"] = "RUN-" + _h(f"{case} {run['source_system']} {run['feed']}")
        for system, feed, status, accounts in failing:
            if (run["source_system"], run["feed"]) == (system, feed):
                run.update(status=status, upstream_status=status)
                impact = _made(case, run["id"], IMPACTS[feed], day)
                data = impact["data"]
                for name in ("source_system", "feed", "business_date", "status"):
                    data[name] = run[name]
                data["run_id"] = run["id"]
                data["affected_accounts"] = _listed(
                    data["affected_accounts"],
                    [
                        {
                            "account_id": a,
                            "manifest_account_id": a,
                            "reason": "ACCOUNT_MANIFEST_MISMATCH",
                            "household_id": _household(a),
                            "household_attribution": "RESOLVED",
                        }
                        for a in accounts
                    ],
                )
                impacts[run["id"]] = impact
        runs.append(run)
    listed_runs = _made(case, "feed_runs", RUNS, day)
    listed_runs["data"] = _listed(RUNS["data"], runs)
    quality = _made(case, "quality", QUALITY, day)
    homes: dict[str, list] = {}
    for flag in flags:
        homes.setdefault(_household(flag["subject"]), []).append(flag)
    like = QUALITY["data"]["items"][0]
    items = [
        {**like, "household_id": home, "flags": _listed(like["flags"], mine)}
        for home, mine in sorted(homes.items())
    ]
    quality["quality_flags"] = flags
    quality["data"] = _listed(QUALITY["data"], items)
    return {
        "get_data_quality_report": quality,
        "feed_runs": listed_runs,
        "feed_run_impact": impacts,
    }


def warn(code: str, subject: str, **detail: Any) -> dict:
    return {"code": code, "severity": "warning", "subject": subject, "detail": detail}


def pas(*accounts: str) -> list[dict]:
    return [warn("SOURCE_UNAVAILABLE", a, source="PAS") for a in accounts]


def custody(*accounts: str) -> list[dict]:
    return [warn("RECONCILIATION_UNAVAILABLE", a, source="CUSTODY") for a in accounts]


def step(playbook: str, tell: str, by: str, hold: bool) -> dict:
    return {"outcome": "step", "playbook": playbook, "tell": tell, "by": by, "hold_report": hold}


NO_STEP = {"outcome": "no_step", "playbook": None, "tell": None, "by": None, "hold_report": None}
FEED_A = step("feed-outage", "portfolio-accounting", "same_day 20:00 ET", True)
FEED_B = step("feed-outage", "custodian-b-service", "within 2 hours", True)
CROSS_CHECK_HELD = step(
    "missing-cross-check", "custody-operations", "next_business_day 10:00 ET", True
)
CROSS_CHECK = CROSS_CHECK_HELD | {"hold_report": False}

# (case, kind, day, failing runs, flags, the expected note of each exception in build order)
FIRST_CYCLE = [
    (
        "made-pas-a-failed",
        "regression",
        "2026-11-02",
        [("PAS-A", "pas_snapshots", "FAILED", ["WC-AC-0001", "WC-AC-0003"])],
        pas("WC-AC-0001", "WC-AC-0003"),
        [FEED_A],
    ),
    (
        "made-price-stale",
        "regression",
        "2026-11-03",
        [],
        [warn("PRICE_STALE", "WC-AC-0005:WC-SEC-0003")],
        [NO_STEP],
    ),
    (
        "made-pas-b-timeout",
        "regression",
        "2026-11-04",
        [("PAS-B", "pas_snapshots", "TIMEOUT", ["WC-AC-0002"])],
        pas("WC-AC-0002"),
        [NO_STEP],
    ),
    (
        "made-crm-unavailable",
        "regression",
        "2026-11-05",
        [],
        [warn("SOURCE_UNAVAILABLE", "WC-AC-0007", source="CRM")],
        [NO_STEP],
    ),
    (
        "made-pas-b-failed",
        "new_rule",
        "2026-11-06",
        [("PAS-B", "pas_snapshots", "FAILED", ["WC-AC-0004", "WC-AC-0006"])],
        pas("WC-AC-0004", "WC-AC-0006"),
        [FEED_B],
    ),
    (
        "made-custody-b-partial",
        "new_rule",
        "2026-11-09",
        [("CUSTODY-B", "custody_snapshots", "PARTIAL", ["WC-AC-0008"])],
        custody("WC-AC-0008"),
        [CROSS_CHECK],
    ),
    (
        "made-custody-a-failed",
        "new_rule",
        "2026-11-10",
        [("CUSTODY-A", "custody_snapshots", "FAILED", ["WC-AC-0009"])],
        custody("WC-AC-0009"),
        [CROSS_CHECK],
    ),
    (
        "made-both-pas-feeds",
        "new_rule",
        "2026-11-11",
        [
            ("PAS-A", "pas_snapshots", "FAILED", ["WC-AC-0011"]),
            ("PAS-B", "pas_snapshots", "FAILED", ["WC-AC-0012"]),
        ],
        pas("WC-AC-0011", "WC-AC-0012"),
        [FEED_A, FEED_B],
    ),
]


def changed(cases: list[tuple], name: str, notes: list[dict]) -> list[tuple]:
    """The same cases, with one case's expected notes given otherwise."""
    return [each[:5] + (notes,) if each[0] == name else each for each in cases]


# The first cycle's feed change alone: the cross-check is a rule that does not change.
FEED_ONLY = changed(
    changed(FIRST_CYCLE, "made-custody-b-partial", [CROSS_CHECK_HELD]),
    "made-custody-a-failed",
    [CROSS_CHECK_HELD],
)
FEED_ONLY = [
    (name, "regression" if name.startswith("made-custody") else kind, *rest)
    for name, kind, *rest in FEED_ONLY
]
FEED_ONLY += [
    (
        "made-pas-b-failed-again",
        "new_rule",
        "2026-11-12",
        [("PAS-B", "pas_snapshots", "FAILED", ["WC-AC-0014"])],
        pas("WC-AC-0014"),
        [FEED_B],
    ),
    (
        "made-pas-b-failed-with-a-conflict",
        "new_rule",
        "2026-11-13",
        [("PAS-B", "pas_snapshots", "FAILED", ["WC-AC-0016"])],
        [*pas("WC-AC-0016"), warn("OWNERSHIP_CONFLICT", "WC-AC-0013")],
        [FEED_B, NO_STEP],
    ),
]
FEED_ONLY = [
    each for each in FEED_ONLY if each[0] not in ("made-price-stale", "made-crm-unavailable")
]

FOLDER = {"regression": "regression", "new_rule": "new-rule"}


def write(folder: Path, cases: list[tuple] = FIRST_CYCLE, writer: dict = WRITER) -> Path:
    """A whole case set in `folder`: inputs, expected notes and the writer."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "case-set.yaml").write_text(yaml.safe_dump({"writer": writer}))
    expected: dict[str, list] = {"regression": [], "new_rule": []}
    for name, kind, day, failing, flags, notes in cases:
        made = envelopes(name, day, failing, flags)
        found, notices = exceptions.build(read_envelopes(day, CUTOFF, made))
        assert len(found) == len(notes), name
        listed = [
            {
                "id": f"e{number}",
                "code": each.code,
                "subjects": each.subjects,
                "feed_facts": each.facts.model_dump() if each.facts else None,
            }
            for number, each in enumerate(found, start=1)
        ]
        case = {
            "case": name,
            "kind": kind,
            "date": day,
            "cutoff": CUTOFF,
            "about": "Made for a test.",
            "exceptions": listed,
            "notices": [each.model_dump() for each in notices],
            "envelopes": made,
        }
        (folder / FOLDER[kind]).mkdir(exist_ok=True)
        (folder / FOLDER[kind] / f"{name}.yaml").write_text(yaml.safe_dump(case))
        expected[kind].append(
            {
                "case": name,
                "notes": [
                    {"exception": f"e{number}", **note}
                    for number, note in enumerate(notes, start=1)
                ],
            }
        )
    for kind, given in expected.items():
        (folder / FOLDER[kind] / "expected-notes.json").write_text(json.dumps({"cases": given}))
    return folder
