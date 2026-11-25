"""Which steps fit an exception, and whether a choice holds, by fixed fields only."""

from collections.abc import Iterable

from backend.playbook_format import Step, When
from backend.vocabulary import ANY, FeedFacts


def misfit(step: Step, code: str, facts: FeedFacts | None) -> str | None:
    """The condition on which a step does not fit an exception, in one line, or nothing."""
    return misfit_of(step.code, step.when, code, facts)


def misfit_of(step_code: str, when: When, code: str, facts: FeedFacts | None) -> str | None:
    """As `misfit`, for a step known by its code and conditions alone, as a proposal's is."""
    if step_code != code:
        return "its warning code is not the exception's."
    for field, wanted in when.fixed():
        if facts is None:
            return f"it needs {field} {wanted}, and the exception has no feed facts."
        actual = getattr(facts, field)
        if actual != wanted:
            return f"its {field} is {wanted}, and the exception's is {actual}."
    return None


def covers(when: When, facts: FeedFacts | None) -> bool:
    """Whether a condition holds for an exception with these feed facts, or with none."""
    return all(facts is not None and getattr(facts, f) == v for f, v in when.fixed())


def some_step_fits(steps: Iterable[Step], code: str, facts: FeedFacts | None) -> bool:
    return any(misfit(step, code, facts) is None for step in steps)


def overlap(first: Step, second: Step) -> bool:
    """Whether some exception could fit both steps."""
    if first.code != second.code:
        return False
    theirs = second.when.model_dump()
    return all(
        value == theirs[field] or ANY in (value, theirs[field])
        for field, value in first.when.model_dump().items()
    )
