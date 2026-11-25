"""Each hostile proposal in `cases/hostile/`, handed straight to the checks, is refused by the
check it names, and only by that check. The honest proposal they are made from passes."""

import json

import curation
import gating
import knowledge
import pytest
from stand_ins import idle_agent

KINDS = {
    "guardrails": {
        "forbidden_instruction": 4,
        "financial_figure": 2,
        "addressed_to_an_agent": 1,
    },
    "personal": {"personal_data": 1, "client_identifier": 1},
    "support": {
        "rests_on_nothing": 1,
        "unlisted_outcome": 1,
        "outside_scope": 2,
        "notice_supports_nothing": 1,
        "replaced_by_later_memo": 1,
        "hidden_disagreement": 1,
    },
    "base": {"version_not_in_use": 1},
    "overlap": {"overlap": 2},
}
FORBIDDEN = {
    "guardrails-total-on-incomplete-household": "show a total on an incomplete household",
    "guardrails-old-price-carried-forward": "carry an old price forward",
    "guardrails-warning-left-out": "leave out a warning",
    "guardrails-personal-data-to-operations": "give personal data to operations",
}


@pytest.fixture
def cycle(make_layer):
    layer = make_layer(agent=lambda *_: idle_agent(), curator=curation.idle)
    gating.first_cycle(layer)
    return layer


def test_the_hostile_set_has_a_proposal_of_every_kind():
    found: dict[str, dict[str, int]] = {}
    for file in gating.hostile_files():
        case = gating.hostile(file)
        named = found.setdefault(case["refused_by"], {})
        named[case["rule"]] = named.get(case["rule"], 0) + 1

    assert found == KINDS


def test_the_honest_first_cycle_proposal_passes_every_fixed_check(cycle):
    done = gating.checked(cycle, gating.honest())

    run = done["gate_run"]
    assert gating.outcomes(run) == dict.fromkeys(gating.CHECKS, "passed")
    assert run["outcome"] == "passed" and run["kind"] == "check"
    # The cross-check outcome it changes is one the sources disagree about, and it says so.
    report = gating.HONEST["disagreements"][0]["between"][2]
    assert sorted(run["disagreements"]) == sorted(
        [
            sorted([curation.CORRECTION, gating.CROSS_CHECK_FOUNDING]),
            sorted([report, gating.CROSS_CHECK_FOUNDING]),
        ]
    )


def test_the_honest_feed_outage_change_alone_passes_the_support_check(cycle):
    # Narrowing the general step to custodian A with its action
    # unchanged, and adding a custodian B step that rests on the custodian B memo.
    narrowed, added = gating.honest()["changes"][:2]
    narrowed["rests_on"] = [curation.MEMO]

    run = gating.checked(cycle, {"changes": [narrowed, added], "disagreements": []})["gate_run"]

    assert gating.outcomes(run) == dict.fromkeys(gating.CHECKS, "passed")
    assert run["disagreements"] == []


@pytest.mark.parametrize("file", gating.hostile_files(), ids=lambda file: file.stem)
def test_each_hostile_proposal_is_refused_by_the_check_it_names(cycle, file):
    case = gating.hostile(file)
    if retired := case.get("retired_first"):
        assert cycle.retire(retired["playbook"], retired["version"]).status_code == 200

    done = gating.checked(cycle, gating.sent(case))

    run = done["gate_run"]
    assert run["outcome"] == "failed"
    also = set(case.get("also_refused_by", []))
    assert gating.failed(run) == {case["refused_by"], *also}, "refused by another check besides"
    assert case["rule"] in gating.rules(run, case["refused_by"])
    if file.stem in FORBIDDEN:
        (failure,) = next(c for c in run["checks"] if c["check"] == "guardrails")["failures"]
        assert FORBIDDEN[file.stem] in failure["line"]
    assert done["state"] == "checked"
    refused = gating.approve(cycle, done["id"], done["sha256"], run["id"])
    assert refused.status_code == 409
    assert refused.json() == {
        "detail": "The proposal is checked: it was handed in to be checked, "
        "and can never be approved."
    }


@pytest.mark.parametrize("file", gating.hostile_files(), ids=lambda file: file.stem)
def test_no_failure_repeats_what_the_proposal_said(cycle, file):
    case = gating.hostile(file)
    if retired := case.get("retired_first"):
        assert cycle.retire(retired["playbook"], retired["version"]).status_code == 200

    run = gating.checked(cycle, gating.sent(case))["gate_run"]

    lines = json.dumps([f["line"] for c in run["checks"] for f in c["failures"]])
    for change in case["changes"]:
        if change.get("line") and change["line"] not in json.dumps(gating.HONEST):
            assert change["line"] not in lines
    for text in ("Pennyfeather", "wc-ac-0016", "250", "ten dollars", "curator", "household total"):
        assert text not in lines


def test_a_hostile_proposal_that_carries_personal_data_is_kept_by_its_hash_alone(cycle):
    for name, kept in (("personal-name-in-a-line", False), ("guardrails-figure-in-words", True)):
        case = gating.hostile(gating.HOSTILE / f"{name}.yaml")

        done = gating.checked(cycle, gating.sent(case))

        assert done["content_kept"] is kept
        (listed,) = [p for p in gating.record(cycle)["proposals"] if p["id"] == done["id"]]
        assert (listed["content"] is not None) is kept
    stored = knowledge.stored(cycle)
    assert b"Pennyfeather" not in stored
    assert b"ten dollars" in stored
