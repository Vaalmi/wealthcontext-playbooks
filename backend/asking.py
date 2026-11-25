"""Commit a move, ask an agent, and undo the move only when the agent definitely did not start."""

from collections.abc import Callable, Mapping
from typing import Literal

import httpx2

from backend.refusals import Refusal
from backend.settings import Settings

# How long to wait for the agent to say that it started.
ANSWER_SECONDS = 5
Asked = Literal["accepted", "not_started", "unknown"]

# An agent's refusal is a name from its list, so its line is the backend's own.
REFUSED = {
    "no_model": "The triage agent has no model: set TRIAGE_MODEL and its provider's key, "
    "or a replay file.",
    "no_price": "The triage agent has no price for that model, so it cannot hold the cost cap.",
    "no_effort": "The triage agent's model takes no such reasoning effort as TRIAGE_MODEL names.",
    "stray_setting": "The triage agent's environment holds an OPENAI_ setting other than its key, "
    "which the provider's client would read; it was not started.",
    "already_working": "The triage agent is already working this run or review.",
    "other_model": "The triage agent is configured with another model than the gate records "
    "for its trials (TRIAGE_MODEL), so it ran no trial.",
}
CURATOR_REFUSED = {
    "no_model": "The curator has no model: set CURATOR_MODEL and its provider's key, "
    "or a replay file.",
    "no_price": "The curator has no price for that model, so it cannot hold the cost cap.",
    "no_effort": "The curator's model takes no such reasoning effort as CURATOR_MODEL names.",
    "stray_setting": "The curator's environment holds an OPENAI_ setting other than its key, "
    "which the provider's client would read; it was not started.",
    "other_model": "The curator is configured with another model than the backend would record.",
    "already_working": "The curator is already working this run.",
}


class Agent:
    """An agent's app, asked at one address with that agent's own key."""

    def __init__(self, name: str, url: str, header: dict[str, str], refused: Mapping[str, str]):
        self._name = name
        self._refused = refused
        self._http = httpx2.Client(
            base_url=url, headers=header, timeout=ANSWER_SECONDS, trust_env=False
        )

    def close(self) -> None:
        self._http.close()

    def ask(self, payload: dict) -> tuple[Asked, str | None]:
        asked, line, _ = self.ask_why(payload)
        return asked, line

    def ask_why(self, payload: dict) -> tuple[Asked, str | None, str | None]:
        """As `ask`, with the agent's own name for its refusal, when it gave one."""
        try:
            answer = self._http.post("/invocations", json=payload)
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout):
            return "not_started", f"The {self._name} cannot be reached.", None
        except httpx2.HTTPError:
            return "unknown", None, None
        refused_to_start = f"The {self._name} refused to start."
        if answer.status_code >= 400:
            return "not_started", refused_to_start, None
        try:
            body = answer.json()
        except ValueError:
            return "unknown", None, None
        if isinstance(body, dict) and body.get("accepted") is True:
            return "accepted", None, None
        if isinstance(body, dict) and "refused" in body:
            named = str(body["refused"])
            return "not_started", self._refused.get(named, refused_to_start), named
        return "unknown", None, None


class TriageAgent(Agent):
    def __init__(self, settings: Settings):
        key = {"X-Triage-Agent-Key": settings.triage_agent_key}
        super().__init__("triage agent", settings.agent_url, key, REFUSED)


class Curator(Agent):
    def __init__(self, settings: Settings):
        key = {"X-Curator-Key": settings.curator_key}
        super().__init__("curator", settings.curator_url, key, CURATOR_REFUSED)


def commit_then_ask(
    agent: Agent,
    commit: Callable[[], dict],
    undo: Callable[[], bool],
    undone: str,
) -> Literal["accepted", "unknown"]:
    """`commit` returns what to send. `undo` says whether there was still a move to undo.
    `undone` is how a refusal says that it was.
    """
    payload = commit()
    asked, line = agent.ask(payload)
    if asked == "not_started":
        if undo():
            raise Refusal(503, f"{line} {undone}")
        return "accepted"  # it has been heard from, so it did start
    return asked
