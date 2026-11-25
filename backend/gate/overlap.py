"""The overlap check: after the proposal, no two steps in use could fit one exception.

Across every playbook, wildcards included, as the store refuses whenever the set in use
changes.
"""

from itertools import combinations

from backend import matching, models
from backend.gate.effect import Given, Placed, after, in_use, shown, where
from backend.playbook_format import Step


def _as_step(placed: Placed) -> Step:
    # Only the code and the conditions decide an overlap.
    return Step.model_construct(code=placed.code, when=placed.when)


def _named(placed: Placed, given: Given) -> str:
    if placed.change is None:
        version = next(v.version for v in given.playbooks.versions if v.id == placed.playbook)
        return f"{placed.playbook} version {version} step {placed.step}, in use"
    return f"change {placed.change}"


def check(given: Given) -> list[models.Failure]:
    changes = given.proposal.changes
    failures = []
    for first, second in combinations(after(in_use(given.playbooks), given.proposal), 2):
        if first.change is None and second.change is None:
            continue  # the set in use was checked when it was put in use
        if not matching.overlap(_as_step(first), _as_step(second)):
            continue
        ours, other = (first, second) if first.change is not None else (second, first)
        assert ours.change is not None
        change = changes[ours.change - 1]
        failures.append(
            models.Failure(
                change=ours.change,
                step=shown(change),
                rule="overlap",
                line=f"{where(ours.change, change)}: it would overlap {_named(other, given)}.",
            )
        )
    return failures
