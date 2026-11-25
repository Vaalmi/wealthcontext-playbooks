"""The playbook owner's rulings on the disagreements a proposal carries.

The gate records each changed outcome that is disputed: which change makes it, which pairs
of sources disagree there, which sources speak there, and what supports the change there.
The owner rules on each pair for each such change, so that one change can follow one
source and another change the other. A disagreement the proposal lists that is no more
than those pairs is ruled on as them. Any other is ruled on for each change that rests on
some of it, and beats a source at an outcome only where the one ruled for speaks.

Approving cannot contradict a ruling. A correction or a gap report ruled against for a memo
would override it by itself (which source wins). A change all of whose support at an
outcome is ruled against follows no ruling, and neither does one that rests on a listed
disagreement and not on the side ruled for. Each is refused, and for any proposal the gate
passes some ruling can settle all of it.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from backend import models
from backend.refusals import Refusal
from backend.store import Ruling

NOT_IN_ONE = (
    "A ruling names no disagreement of the proposal, or a source that is not in that one. "
    "Nothing changes."
)
TWICE = "A disagreement has more than one ruling: name one source for it."

# A disagreement to rule on: the change it is about, if any, and the sources in it.
Key = tuple[int | None, tuple[str, ...]]


@dataclass(frozen=True)
class _ToSettle:
    # The pairs the gate found, for each change responsible where they disagree.
    found: set[Key]
    # The disagreements only the proposal lists, for each change that rests on some of one.
    listed: set[Key]

    def keys(self) -> list[Key]:
        return sorted(self.found | self.listed, key=lambda key: (key[0] or 0, key[1]))


def _to_settle(proposal: models.Proposed, disputed: Sequence[Mapping[str, Any]]) -> _ToSettle:
    found: set[Key] = set()
    for outcome in disputed:
        found |= {(outcome["change"], tuple(sorted(pair))) for pair in outcome["pairs"]}
    listed: set[Key] = set()
    for told in proposal.disagreements:
        between = set(told.between)
        if set().union(*(set(pair) for _, pair in found if set(pair) <= between)) == between:
            continue  # no more than the pairs the gate found
        resting = [
            number
            for number, change in enumerate(proposal.changes, start=1)
            if between & set(change.rests_on)
        ]
        listed |= {(change, tuple(sorted(between))) for change in resting or [None]}
    return _ToSettle(found, listed)


def to_rule_on(
    proposal: models.Proposed, disputed: Sequence[Mapping[str, Any]]
) -> list[models.ToRuleOn]:
    """Every disagreement an approval must rule on, each once, always in one order."""
    return [
        models.ToRuleOn(change=change, between=list(between))
        for change, between in _to_settle(proposal, disputed).keys()
    ]


def settled(
    proposal: models.Proposed,
    disputed: Sequence[Mapping[str, Any]],
    memos: Collection[str],
    sent: Sequence[models.RuleFor],
) -> list[Ruling]:
    """The rulings as the ledger keeps them, or a 422 that says what is wrong.

    A refusal repeats references of the bundle's own, and never what was sent.
    """
    to_settle = _to_settle(proposal, disputed)
    needed = to_settle.keys()
    ruled: dict[Key, str] = {}
    for each in sent:
        key = (each.change, tuple(sorted(set(each.between))))
        if key not in needed or each.ruled_for not in key[1]:
            raise Refusal(422, NOT_IN_ONE)
        if key in ruled:
            raise Refusal(422, TWICE)
        ruled[key] = each.ruled_for
    for key in needed:
        if key not in ruled:
            change, between = key
            on = f" on change {change}" if change else ""
            raise Refusal(
                422,
                f"The disagreement between {', '.join(between)}{on} has no ruling: "
                "name the source you rule for.",
            )
    for outcome in disputed:
        _followed_at(outcome, to_settle, ruled, memos)
    for change, between in sorted(to_settle.listed, key=lambda key: (key[0] or 0, key[1])):
        if change is not None:
            _followed_as_listed(proposal, disputed, change, between, ruled[change, between], memos)
    return [
        Ruling(change=change, between=list(between), ruled_for=ruled[change, between])
        for change, between in needed
    ]


def _followed_at(
    outcome: Mapping[str, Any],
    to_settle: _ToSettle,
    ruled: Mapping[Key, str],
    memos: Collection[str],
) -> None:
    """At one disputed outcome, the change rests on something there not ruled against."""
    pairs = {tuple(sorted(pair)) for pair in outcome["pairs"]}
    supported_by, lost = set(outcome["supported_by"]), set()
    for key, winner in ruled.items():
        change, between = key
        if change != outcome["change"]:
            continue
        if between in pairs or (key in to_settle.listed and winner in outcome["speaking"]):
            against = (set(between) - {winner}) & supported_by
            _not_overridden(outcome["change"], winner, against, memos)
            lost |= against
    if supported_by and supported_by <= lost:
        raise Refusal(
            422,
            f"Change {outcome['change']} rests on no source you rule for, where you rule against "
            f"{', '.join(sorted(lost))}: refuse the proposal instead.",
        )


def _followed_as_listed(
    proposal: models.Proposed,
    disputed: Sequence[Mapping[str, Any]],
    change: int,
    between: tuple[str, ...],
    winner: str,
    memos: Collection[str],
) -> None:
    """A change on a disagreement only the proposal lists rests on the side ruled for."""
    covering = {s for each in disputed if each["change"] == change for s in each["supported_by"]}
    _not_overridden(change, winner, (set(between) - {winner}) & covering, memos)
    if winner not in proposal.changes[change - 1].rests_on:
        raise Refusal(
            422,
            f"Change {change} rests on no source you rule for, in the disagreement between "
            f"{', '.join(between)}: refuse the proposal instead.",
        )


def _not_overridden(change: int, winner: str, against: set[str], memos: Collection[str]) -> None:
    if winner in memos and (overruled := sorted(against - set(memos))):
        raise Refusal(
            422,
            f"Change {change} rests on {overruled[0]}, which you rule against for "
            "a memo: refuse the proposal instead.",
        )
