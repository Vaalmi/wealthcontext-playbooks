"""The support check: by its effect, every change a proposal makes rests on its bundle.

It works out which step applies, with what action, at every condition an exception can
have, before the proposal and after it. Each condition whose outcome differs must lie in
the scope of a bundle item named by the change that made the difference: the step that now
applies there, or the change that took the old step away. A scope names a playbook, a
warning code and conditions, and covers a change only in that playbook. A gap report filed
where no playbook covered the warning names none, and covers its warning in any. A scope
was set when the item was taken in or filed, and the proposal cannot change it.

Which source wins: a later memo from the same desk replaces an earlier one, so resting on
the earlier one fails. Two sources from different desks, or a correction or gap report and
a memo, that both cover a changed outcome disagree, whichever playbooks they speak for, and
the proposal must list them as a disagreement, so that the playbook owner rules on it. A
notice whose scope is "none" supports nothing. Every change also reaches some exception,
and rests on an item whose scope covers one it reaches, even where its outcome does not
change.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from backend import bundle, matching, models
from backend.gate.effect import (
    CODES,
    Given,
    Placed,
    after,
    conditions,
    described,
    fitting,
    in_use,
    shown,
    where,
)
from backend.playbook_format import Scope
from backend.vocabulary import FeedFacts

MEMO, CORRECTION, NOTICE, GAP_REPORT = "memo", "correction", "notice", "gap_report"


@dataclass(frozen=True)
class Source:
    """A bundle item a proposal may rest on, as the support check reads it."""

    reference: str
    kind: str
    # The playbook its scope names. None: a gap report on a warning no playbook covered.
    playbook: str | None
    desk: str | None
    date: str | None
    code: str | None
    # A memo's or a correction's conditions. A gap report's are its feed facts exactly.
    scope: Scope | None
    feed: FeedFacts | None

    def covers(self, playbook: str, code: str, facts: FeedFacts | None) -> bool:
        """Whether its scope covers this condition in this playbook."""
        if self.kind == NOTICE or self.code != code:
            return False
        if self.playbook is not None and self.playbook != playbook:
            return False
        if self.kind == GAP_REPORT:
            return self.feed == facts
        assert self.scope is not None
        return matching.covers(self.scope.when, facts)


def _source(item: Mapping[str, Any]) -> Source:
    content = item["content"]
    if item["kind"] == bundle.GAP_REPORT:
        feed = content["feed"]
        return Source(
            reference=item["reference"],
            kind=GAP_REPORT,
            playbook=content["playbook"],
            desk=None,
            date=None,
            code=content["code"],
            scope=None,
            feed=FeedFacts.model_validate(feed) if feed else None,
        )
    if content["scope"] == "none":
        scope, kind = None, NOTICE
    else:
        scope, kind = Scope.model_validate(content["scope"]), content["kind"]
    return Source(
        reference=item["reference"],
        kind=kind,
        playbook=scope.playbook if scope else None,
        desk=content["desk"],
        date=content.get("date"),
        code=scope.code if scope else None,
        scope=scope,
        feed=None,
    )


def _sources(given: Given) -> dict[str, Source]:
    return {
        item["reference"]: _source(item) for item in given.items if item["kind"] in bundle.CITABLE
    }


def _replacing(
    memo: Source, sources: Mapping[str, Source], playbook: str, code: str, facts
) -> Source | None:
    """The latest memo of the same desk that replaces this one at this condition, or nothing."""
    if memo.kind != MEMO:
        return None
    later = [
        each
        for each in sources.values()
        if each.kind == MEMO
        and each.desk == memo.desk
        and (each.date or "") > (memo.date or "")
        and each.covers(playbook, code, facts)
    ]
    return max(later, key=lambda each: each.date or "", default=None)


def _disagree(first: Source, second: Source) -> bool:
    kinds = {first.kind, second.kind}
    if kinds == {MEMO}:
        return first.desk != second.desk
    return MEMO in kinds and bool(kinds & {CORRECTION, GAP_REPORT})


@dataclass
class Supported:
    failures: list[models.Failure]
    # The pairs of items that disagree at an outcome the proposal changes, for the owner's ruling.
    disagreements: list[list[str]]
    # Each change's disputed outcomes: who speaks and disagrees there, and what supports it.
    disputed: list[models.Disputed]


class _Judging:
    def __init__(self, given: Given):
        self.given = given
        self.changes = given.proposal.changes
        self.sources = _sources(given)
        self.failures: list[models.Failure] = []
        self.needed: set[tuple[str, str]] = set()
        self.disputed: list[models.Disputed] = []

    def fail(self, number: int | None, rule: str, why: str) -> None:
        if number is None:
            found = models.Failure(change=None, step=None, rule=rule, line=f"{why}.")
        else:
            change = self.changes[number - 1]
            line = f"{where(number, change)}: {why}."
            found = models.Failure(change=number, step=shown(change), rule=rule, line=line)
        if found not in self.failures:
            self.failures.append(found)

    def named(self) -> None:
        """Every change names at least one bundle item, and every name is the bundle's."""
        for number, change in enumerate(self.changes, start=1):
            if not change.rests_on:
                self.fail(number, "rests_on_nothing", "it names no bundle item that it rests on")
            elif set(change.rests_on) - self.sources.keys():
                why = "it names an item that is not in the bundle as a firm source or a gap report"
                self.fail(number, "not_in_bundle", why)

    def reaching(self) -> None:
        """Every change reaches an exception, and rests on an item whose scope covers one it
        reaches, whether or not its outcome changes there.
        """
        before = in_use(self.given.playbooks)
        now_in_use = after(before, self.given.proposal)
        for number, change in enumerate(self.changes, start=1):
            cited = [self.sources[each] for each in change.rests_on if each in self.sources]
            key = (change.playbook, change.step)
            new = [each for each in now_in_use if each.change == number]
            old = [each for each in before if (each.playbook, each.step) == key]
            reached = [
                (each.code, facts)
                for each in [*new, *old]
                for facts in conditions(each.code)
                if matching.covers(each.when, facts)
            ]
            if new and not any(matching.covers(new[0].when, f) for f in conditions(new[0].code)):
                why = "its step fits no exception that its warning code can have"
                self.fail(number, "fits_no_exception", why)
            if not cited:
                continue  # said already: it names nothing, or nothing of the bundle
            if all(each.kind == NOTICE for each in cited):
                why = "it rests only on notices, which change no rule"
                self.fail(number, "notice_supports_nothing", why)
            elif reached and not any(
                source.covers(change.playbook, code, facts)
                for source in cited
                for code, facts in reached
            ):
                why = "nothing it rests on covers any exception that its step fits"
                self.fail(number, "outside_scope", why)
        for number, told in enumerate(self.given.proposal.disagreements, start=1):
            if set(told.between) - self.sources.keys():
                why = f"Disagreement {number} names an item that is not in the bundle"
                self.fail(None, "not_in_bundle", why)

    def outcomes(self) -> None:
        before = in_use(self.given.playbooks)
        now_in_use = after(before, self.given.proposal)
        touched = {(each.playbook, each.step): n for n, each in enumerate(self.changes, start=1)}
        for code in CODES:
            for facts in conditions(code):
                was = fitting(before, code, facts)
                now = fitting(now_in_use, code, facts)
                if {each.outcome() for each in was} == {each.outcome() for each in now}:
                    continue
                made = {each.change for each in now if each.change is not None}
                # Where no step of the proposal applies now, the change that took the old away.
                responsible = made or {
                    touched[key] for each in was if (key := (each.playbook, each.step)) in touched
                }
                for number in sorted(responsible):
                    self.supported(number, code, facts, now)
                self.listed(code, facts, sorted(responsible))

    def supported(self, number: int, code: str, facts, now: list[Placed]) -> None:
        change = self.changes[number - 1]
        cited = [self.sources[each] for each in change.rests_on if each in self.sources]
        if not cited or all(each.kind == NOTICE for each in cited):
            return  # said already, once for the change
        at = described(code, facts)
        covering = [each for each in cited if each.covers(change.playbook, code, facts)]
        if not covering:
            if any(each.change == number for each in now):
                rule = "outside_scope"
                why = f"it changes the outcome for {at}, and nothing it rests on covers that"
            else:
                rule = "unlisted_outcome"
                why = (
                    f"it changes the outcome for {at}, which its step no longer covers, "
                    "and nothing it rests on covers that"
                )
            self.fail(number, rule, why)
            return
        for memo in covering:
            if later := _replacing(memo, self.sources, change.playbook, code, facts):
                why = (
                    f"it rests on {memo.reference} for {at}, and the later memo "
                    f"{later.reference} from the same desk replaces it there"
                )
                self.fail(number, "replaced_by_later_memo", why)

    def dispute(
        self, number: int, code: str, facts, pairs: list[tuple[str, str]], speaking: set[str]
    ) -> None:
        """Keep what a ruling needs of this change at this outcome, once for outcomes alike."""
        change = self.changes[number - 1]
        found = models.Disputed(
            change=number,
            pairs=sorted([list(pair) for pair in pairs]),
            speaking=sorted(speaking),
            supported_by=sorted(
                {
                    each
                    for each in change.rests_on
                    if each in self.sources
                    and self.sources[each].covers(change.playbook, code, facts)
                }
            ),
        )
        if found not in self.disputed:
            self.disputed.append(found)

    def speaks(self, source: Source, code: str, facts) -> bool:
        """Whether a source speaks about this outcome: its scope, in its own playbook, covers it,
        and no later memo of its desk replaces it there. At most one step in use fits an
        exception, whatever its playbook, so a proposal cannot step round a source by naming
        another playbook.
        """
        own = source.playbook or ""  # a gap report naming none covers its warning in any
        return source.covers(own, code, facts) and not _replacing(
            source, self.sources, own, code, facts
        )

    def listed(self, code: str, facts, responsible: list[int]) -> None:
        """Sources that disagree about a changed outcome are listed as a disagreement."""
        speaking = sorted(
            (each for each in self.sources.values() if self.speaks(each, code, facts)),
            key=lambda each: each.reference,
        )
        told = [set(each.between) for each in self.given.proposal.disagreements]
        pairs = [
            (first.reference, second.reference)
            for first, second in combinations(speaking, 2)
            if _disagree(first, second)
        ]
        names = {each.reference for each in speaking}
        # Disputed by the rules, or as the proposal lists it.
        if pairs or any(len(names & group) > 1 for group in told):
            for number in responsible:
                self.dispute(number, code, facts, pairs, names)
        for pair in pairs:
            if pair in self.needed:
                continue  # said once, at the first condition where they disagree
            self.needed.add(pair)
            if not any(set(pair) <= each for each in told):
                why = (
                    f"{pair[0]} and {pair[1]} disagree about the outcome for "
                    f"{described(code, facts)}, which it changes, "
                    "and the proposal does not list them as a disagreement"
                )
                for number in responsible:
                    self.fail(number, "hidden_disagreement", why)


def check(given: Given) -> Supported:
    """What fails, and what the owner's ruling will need."""
    judging = _Judging(given)
    judging.named()
    judging.reaching()
    judging.outcomes()
    disagreements = [list(pair) for pair in sorted(judging.needed)]
    return Supported(judging.failures, disagreements, judging.disputed)
