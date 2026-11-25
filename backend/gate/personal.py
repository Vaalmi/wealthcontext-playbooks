"""The personal-data check: no personal data and no client identifier anywhere in a proposal.

Every text of it, keys too, is read by the same rules as a memo or a gap report, but for
words addressed to an agent, which the guardrails check. A playbook's or a step's name must
be lower-case words, which hold no digit, so no identifier or number.
"""

from backend import models, personal_data
from backend.gate.effect import TEXT_LENGTH, Given, is_name, shown, where
from backend.proposals import names

NOT_A_NAME = "names a playbook or a step in other than lower-case words joined by hyphens"


def _broken(value: object) -> personal_data.Rule | None:
    return personal_data.broken_rule(value, TEXT_LENGTH, agent_words=False)


def check(given: Given) -> list[models.Failure]:
    failures = []
    for number, change in enumerate(given.proposal.changes, start=1):
        if broken := _broken([change.model_dump(mode="json"), names(change)]):
            rule, why = broken, personal_data.WHY[broken]
        elif not (is_name(change.playbook) and is_name(change.step)):
            rule, why = "not_a_name", NOT_A_NAME
        else:
            continue
        failures.append(
            models.Failure(
                change=number, step=shown(change), rule=rule, line=f"{where(number, change)} {why}."
            )
        )
    for number, told in enumerate(given.proposal.disagreements, start=1):
        if rule := _broken(told.model_dump(mode="json")):
            failures.append(
                models.Failure(
                    change=None,
                    step=None,
                    rule=rule,
                    line=f"Disagreement {number} {personal_data.WHY[rule]}.",
                )
            )
    return failures
