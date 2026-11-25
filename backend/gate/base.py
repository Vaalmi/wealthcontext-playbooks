"""The base check: every change names the playbook version in use, and a step that is there.

A proposal made against a version that is no longer in use is out of date, and refused. At
approval the whole set in use must also be the one the gate ran against.
"""

from backend import models
from backend.gate.effect import Given, shown, where


def check(given: Given) -> list[models.Failure]:
    in_use = {each.id: each.version for each in given.playbooks.versions}
    steps = {(each.playbook, each.step.id) for each in given.playbooks.steps}
    failures = []
    for number, change in enumerate(given.proposal.changes, start=1):
        found = in_use.get(change.playbook)
        named = (change.playbook, change.step)
        if change.version is None:
            wrong = "version_not_in_use" if found is not None else None
            why = "it names no version, and a version of its playbook is in use"
        elif found != change.version:
            wrong, why = "version_not_in_use", "it names a version of a playbook that is not in use"
        elif isinstance(change, models.AddStep):
            wrong = "step_exists" if named in steps else None
            why = "it adds a step that the version in use has already"
        else:
            wrong = "no_such_step" if named not in steps else None
            why = "it names a step that the version in use does not have"
        if wrong:
            failures.append(
                models.Failure(
                    change=number,
                    step=shown(change),
                    rule=wrong,
                    line=f"{where(number, change)}: {why}.",
                )
            )
    return failures
