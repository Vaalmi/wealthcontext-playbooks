"""The rulings on disagreements, on sources named by what they are and on what the gate finds."""

from random import Random
from typing import Any

import firm
import pytest

from backend import models, rulings
from backend.gate import effect, support
from backend.refusals import Refusal
from backend.store import PlaybookSet, StepInUse

MEMOS = {f"memo-{each}" for each in "abcdef"}
A, B, C, D = "memo-a", "memo-b", "memo-c", "memo-d"


def proposed(rests_on: list[list[str]], *between: list[str]) -> models.Proposed:
    changes = [
        {
            "change": "remove",
            "playbook": "a-playbook",
            "version": 1,
            "step": f"step-{chr(97 + number)}",
            "rests_on": cited,
        }
        for number, cited in enumerate(rests_on)
    ]
    listed = [{"between": each, "about": "They disagree."} for each in between]
    return models.Proposed.model_validate({"changes": changes, "disagreements": listed})


def dispute(change: int, supported_by: list[str], *pairs: list[str]) -> dict:
    speaking = sorted({*supported_by, *(each for pair in pairs for each in pair)})
    return {
        "change": change,
        "pairs": list(pairs),
        "speaking": speaking,
        "supported_by": supported_by,
    }


def settled(proposal, recorded, *named: tuple) -> dict[tuple, str]:
    """`named` is (change, the sources that disagree, the one ruled for) each."""
    sent = [
        models.RuleFor(change=change, between=between, ruled_for=winner)
        for change, between, winner in named
    ]
    made = rulings.settled(proposal, recorded, MEMOS, sent)
    return {(each.change, *each.between): each.ruled_for for each in made}


def refused(proposal, recorded, *named: tuple) -> str:
    with pytest.raises(Refusal) as refusal:
        settled(proposal, recorded, *named)
    assert refusal.value.status == 422
    return refusal.value.line


def test_one_of_a_pair_settles_it():
    proposal = proposed([[A, B]], [A, B])
    recorded = [dispute(1, [A, B], [A, B])]

    assert settled(proposal, recorded, (1, [B, A], B)) == {(1, A, B): B}
    assert "has no ruling" in refused(proposal, recorded)
    twice = refused(proposal, recorded, (1, [A, B], A), (1, [A, B], B))
    assert twice == rulings.TWICE


def test_a_group_the_curator_listed_settles_no_pair_the_gate_found():
    proposal = proposed([[A], [C]], [A, B, C, D])
    recorded = [dispute(1, [A], [A, B]), dispute(2, [C], [C, D])]

    line = refused(proposal, recorded, (1, [A, B], A))
    group = refused(proposal, recorded, (1, [A, B, C, D], A))

    assert f"{C}, {D} on change 2 has no ruling" in line
    assert group == rulings.NOT_IN_ONE
    assert settled(proposal, recorded, (1, [A, B], A), (2, [C, D], C)) == {
        (1, A, B): A,
        (2, C, D): C,
    }


def test_one_change_can_follow_one_source_and_another_change_the_other():
    # A split by custodian, between three desks' memos that disagree with each other.
    proposal = proposed([[A], [C]], [A, B, C])
    recorded = [dispute(1, [A], [A, B]), dispute(2, [C], [A, B], [A, C], [B, C])]
    named = ((1, [A, B], A), (2, [A, B], B), (2, [A, C], C), (2, [B, C], C))

    assert len(settled(proposal, recorded, *named)) == 4


def test_pairs_only_the_proposal_lists_can_all_be_ruled_on():
    proposal = proposed([[A]], [A, B], [A, C], [B, C])
    named = ((1, [A, B], A), (1, [A, C], A), (None, [B, C], B))

    assert len(settled(proposal, [], *named)) == 3


def test_another_changes_winner_does_not_stand_in_for_a_change_that_lost():
    proposal = proposed([[A], [B]], [A, B])
    recorded = [dispute(2, [B], [A, B])]

    line = refused(proposal, recorded, (2, [A, B], A))

    assert "Change 2 rests on no source you rule for" in line


def test_another_disagreements_winner_does_not_stand_in_for_the_one_that_lost():
    # One change over two outcomes, resting on one source at each.
    proposal = proposed([[B, C]], [A, B], [C, D])
    recorded = [dispute(1, [B], [A, B]), dispute(1, [C], [C, D])]

    line = refused(proposal, recorded, (1, [A, B], A), (1, [C, D], C))

    assert f"where you rule against {B}" in line
    assert settled(proposal, recorded, (1, [A, B], B), (1, [C, D], C))


def test_a_winner_that_does_not_support_the_change_at_that_outcome_does_not_save_it():
    proposal = proposed([[A, B]], [A, B])
    # A speaks there for another playbook: only B supports the change.
    recorded = [dispute(1, [B], [A, B])]

    assert "rests on no source you rule for" in refused(proposal, recorded, (1, [A, B], A))


def test_an_uncited_source_the_owner_disagrees_with_need_not_be_ruled_for():
    report, correction = "gap-report-x", "correction-x"
    proposal = proposed([[correction]], [correction, report, A])
    recorded = [dispute(1, [correction], [correction, A], [report, A])]

    named = ((1, [correction, A], correction), (1, [report, A], A))

    assert settled(proposal, recorded, *named)[1, report, A] == A


def test_a_correction_ruled_against_for_a_memo_is_not_approved_though_the_memo_is_cited():
    proposal = proposed([["correction-x", A]], ["correction-x", A])
    recorded = [dispute(1, ["correction-x", A], ["correction-x", A])]

    line = refused(proposal, recorded, (1, ["correction-x", A], A))

    assert "Change 1 rests on correction-x, which you rule against for a memo" in line


def test_a_ruling_on_no_disagreement_is_refused_and_not_repeated():
    proposal = proposed([[A]])
    named = (1, ["quillon-vantree", A], "quillon-vantree")

    assert refused(proposal, [], named) == rulings.NOT_IN_ONE
    assert settled(proposal, []) == {}


# On what the gate itself finds


def memo(
    name: str, desk: str, when: dict, playbook: str = "feed-outage", date="2026-10-20"
) -> dict:
    scope = {"playbook": playbook, "code": "SOURCE_UNAVAILABLE", "when": when}
    content = {"kind": "memo", "desk": desk, "date": date, "scope": scope, "text": "It says so."}
    return {"kind": "firm_source", "reference": name, "sha256": "a" * 64, "content": content}


def adding(playbook: str, number: int, when: dict, rests_on: list[str]) -> dict:
    return {
        "change": "add",
        "playbook": playbook,
        "version": None,
        "step": f"step-{chr(97 + number)}",
        "code": "SOURCE_UNAVAILABLE",
        "when": when,
        "tell": "pricing",
        "by": "within 2 hours",
        "hold_report": False,
        "line": "Ask the desk.",
        "rests_on": rests_on,
    }


NONE_IN_USE = PlaybookSet("set", 1, "0" * 64, (), ())


def judged(
    items: list[dict],
    changes: list[dict],
    groups: list | None = None,
    used: PlaybookSet = NONE_IN_USE,
) -> tuple[models.Proposed, support.Supported]:
    sent: dict[str, Any] = {"changes": changes, "disagreements": []}

    def run() -> tuple[models.Proposed, support.Supported]:
        proposal = models.Proposed.model_validate(sent)
        given = effect.Given(proposal, items, used, {"pricing": "Pricing Desk"})
        return proposal, support.check(given)

    pairs = run()[1].disagreements
    merged = sorted({each for pair in pairs for each in pair})
    between = (groups or []) + ([merged] if merged else [])
    sent["disagreements"] = [{"between": each, "about": "They disagree."} for each in between]
    return run()


def own_rulings(
    proposal: models.Proposed, supported: support.Supported, memos: set[str]
) -> list[models.RuleFor]:
    """The ruling an owner who agrees with each change makes: for what it rests on, a source
    that covers what it changes first, and of those a correction or gap report first.
    """
    made = []
    for each in rulings.to_rule_on(proposal, _dumped(supported)):
        rested = set(proposal.changes[each.change - 1].rests_on) if each.change else set()
        ours = [found for found in supported.disputed if found.change == each.change]
        covering = {source for found in ours for source in found.supported_by}
        ranks = {}
        for source in each.between:
            if source in covering:
                ranks[source] = 1 if source in memos else 0
            else:
                ranks[source] = 2 if source in rested else 3
        winner = min(sorted(each.between), key=ranks.__getitem__)
        made.append(models.RuleFor(change=each.change, between=each.between, ruled_for=winner))
    return made


def _dumped(supported: support.Supported) -> list[dict]:
    return [each.model_dump(mode="json") for each in supported.disputed]


def test_five_desks_memos_that_disagree_in_a_ring_can_be_ruled_on():
    whens = {
        "a": ("any", "any", "FAILED"),
        "b": ("any", "A", "any"),
        "c": ("any", "B", "PARTIAL"),
        "d": ("PAS", "any", "PARTIAL"),
        "e": ("CUSTODY", "B", "any"),
    }
    names = ("source_family", "custodian", "feed_status")
    items = [
        memo(f"memo-{key}", f"desk-{key}", dict(zip(names, values, strict=True)))
        for key, values in whens.items()
    ]
    cells = [
        dict(zip(names, (family, custodian, status), strict=True))
        for family in ("PAS", "CUSTODY")
        for custodian in "AB"
        for status in ("FAILED", "PARTIAL")
    ]
    changes = []
    for cell in cells:
        covering = [
            each["reference"]
            for each in items
            if all(v in ("any", cell[k]) for k, v in each["content"]["scope"]["when"].items())
        ]
        if covering:
            changes.append(adding("feed-outage", len(changes), cell, covering[:1]))
    proposal, supported = judged(items, changes)
    assert supported.failures == [] and len(supported.disagreements) >= 5

    sent = own_rulings(proposal, supported, MEMOS)

    assert rulings.settled(proposal, _dumped(supported), MEMOS, sent)


def random_proposal(random: Random) -> tuple[list[dict], list[dict], list, PlaybookSet]:
    items = []
    for number in range(random.randint(2, 5)):
        when = {
            "source_family": random.choice(["PAS", "CUSTODY", "any", "any"]),
            "custodian": random.choice(["A", "B", "any", "any"]),
            "feed_status": random.choice(["FAILED", "any"]),
        }
        playbook = random.choice(["feed-outage", "feed-outage", "other-feed"])
        item = memo(f"memo-{chr(97 + number)}", f"desk-{random.choice('wxyz')}", when, playbook)
        if random.random() < 0.3:
            item["reference"] = f"correction-{chr(97 + number)}"
            item["content"] |= {"kind": "correction", "desk": None}
            del item["content"]["date"]
        items.append(item)
    changes = []
    for item in random.sample(items, len(items)):
        scope = item["content"]["scope"]
        if any(each["when"] == scope["when"] for each in changes):
            continue
        others = [each["reference"] for each in items if random.random() < 0.3]
        cited = list({item["reference"], *others})
        changes.append(adding(scope["playbook"], len(changes), scope["when"], cited))
        if random.random() < 0.4:
            break
    used = NONE_IN_USE
    if random.random() < 0.4:
        (founded,) = firm.steps("feed-outage")
        step = StepInUse(playbook="feed-outage", version=1, desk="Desk", step=founded)
        used = PlaybookSet("set", 1, "0" * 64, (), (step,))
    groups = []
    if random.random() < 0.4:
        groups.append([each["reference"] for each in random.sample(items, 2)])
    return items, changes, groups, used


def test_whatever_disagreements_the_gate_passes_some_ruling_settles():
    random, tried = Random(8), 0
    for _ in range(4000):
        items, changes, groups, used = random_proposal(random)
        proposal, supported = judged(items, changes, groups, used)
        if supported.failures or not proposal.disagreements:
            continue
        tried += 1
        memos = {each["reference"] for each in items if each["content"]["kind"] == "memo"}
        sent = own_rulings(proposal, supported, memos)
        recorded = _dumped(supported)

        assert rulings.settled(proposal, recorded, memos, sent) is not None, items

    assert tried > 500, "too few proposals with a disagreement passed support"


def test_the_gate_keeps_as_support_only_what_covers_that_outcome():
    feed = {"source_family": "PAS", "custodian": "B", "feed_status": "FAILED"}
    items = [memo(A, "desk-a", feed, "other-feed"), memo(B, "desk-b", feed)]
    proposal, supported = judged(items, [adding("feed-outage", 0, feed, [A, B])])
    assert supported.failures == []

    # A speaks there, in its own playbook, and cited or not it supports nothing in this one.
    assert _dumped(supported) == [dispute(1, [B], [A, B])]
    line = refused(proposal, _dumped(supported), (1, [A, B], A))
    assert "rests on no source you rule for" in line


def test_a_listed_disagreements_winner_beats_the_only_support_where_it_speaks():
    feed = {"source_family": "PAS", "custodian": "B", "feed_status": "FAILED"}
    # One desk and one date: the rules find no disagreement, and the proposal lists one.
    items = [memo(A, "desk-a", feed, "other-feed"), memo(B, "desk-a", feed)]
    proposal, supported = judged(items, [adding("feed-outage", 0, feed, [A, B])], [[A, B]])
    assert supported.failures == [] and supported.disagreements == []
    recorded = _dumped(supported)

    line = refused(proposal, recorded, (1, [A, B], A))

    assert recorded == [{"change": 1, "pairs": [], "speaking": [A, B], "supported_by": [B]}]
    assert f"rests on no source you rule for, where you rule against {B}" in line
    assert settled(proposal, recorded, (1, [A, B], B))


def test_a_change_on_a_listed_disagreement_rests_on_the_side_ruled_for():
    proposal = proposed([[B]], [A, B])

    line = refused(proposal, [], (1, [A, B], A))

    assert "Change 1 rests on no source you rule for" in line


def test_no_ruling_that_is_accepted_beats_all_that_supports_a_change_where_it_speaks():
    random, taken = Random(9), 0
    for _ in range(4000):
        items, changes, groups, used = random_proposal(random)
        proposal, supported = judged(items, changes, groups, used)
        if supported.failures or not proposal.disagreements:
            continue
        memos = {each["reference"] for each in items if each["content"]["kind"] == "memo"}
        recorded = _dumped(supported)
        sent = [
            models.RuleFor(
                change=each.change, between=each.between, ruled_for=random.choice(each.between)
            )
            for each in rulings.to_rule_on(proposal, recorded)
        ]
        try:
            rulings.settled(proposal, recorded, memos, sent)
        except Refusal:
            continue
        taken += 1
        for outcome in recorded:
            here = [
                each
                for each in sent
                if each.change == outcome["change"] and each.ruled_for in outcome["speaking"]
            ]
            supporting = set(outcome["supported_by"])
            for each in here:
                beaten = supporting & (set(each.between) - {each.ruled_for})
                if each.ruled_for in memos:
                    assert beaten <= memos, "a correction overrode the memo ruled for"
            lost = {s for each in here for s in each.between if s != each.ruled_for}
            assert not supporting or supporting - lost, "a change follows only what lost"

    assert taken > 500, "too few random rulings were accepted"
