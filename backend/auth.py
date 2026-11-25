"""Development tokens for the two people and the two agents' keys."""

import secrets
from typing import Literal

from backend.settings import Settings

Role = Literal["analyst", "playbook_owner"]


def _same(given: str | None, configured: str) -> bool:
    # With nothing configured, nothing is the same: the operations behind it refuse.
    if not given or not configured:
        return False
    return secrets.compare_digest(given.encode(), configured.encode())


def kept_apart(settings: Settings) -> None:
    """Each role's secret is its own: one shared would open another role's operations."""
    given = (
        settings.analyst_token,
        settings.playbook_owner_token,
        settings.triage_agent_key,
        settings.curator_key,
    )
    configured = [each for each in given if each]
    if len(set(configured)) != len(configured):
        raise ValueError("The two people's tokens and the two agents' keys must all differ")


def person(token: str | None, settings: Settings) -> Role | None:
    if _same(token, settings.analyst_token):
        return "analyst"
    if _same(token, settings.playbook_owner_token):
        return "playbook_owner"
    return None


def is_triage_agent(key: str | None, settings: Settings) -> bool:
    return _same(key, settings.triage_agent_key)


def is_curator(key: str | None, settings: Settings) -> bool:
    return _same(key, settings.curator_key)
