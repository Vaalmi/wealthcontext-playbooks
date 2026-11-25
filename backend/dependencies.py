"""The gates. Each operation that is not public stands behind exactly one."""

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from backend import auth
from backend.refusals import Refusal

_bearer = HTTPBearer(auto_error=False)
_agent_key = APIKeyHeader(name="X-Triage-Agent-Key", auto_error=False)
_curator_key = APIKeyHeader(name="X-Curator-Key", scheme_name="CuratorKey", auto_error=False)


def any_person(
    request: Request,
    sent: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> auth.Role:
    role = auth.person(sent.credentials if sent else None, request.app.state.settings)
    if role is None:
        raise Refusal(401, "This operation needs a person's token.")
    return role


AnyPerson = Annotated[auth.Role, Depends(any_person)]


def analyst(role: AnyPerson) -> None:
    if role != "analyst":
        raise Refusal(403, "Only the analyst may do this.")


def playbook_owner(role: AnyPerson) -> None:
    if role != "playbook_owner":
        raise Refusal(403, "Only the playbook owner may do this.")


def triage_agent(request: Request, sent: Annotated[str | None, Depends(_agent_key)]) -> None:
    if not auth.is_triage_agent(sent, request.app.state.settings):
        raise Refusal(401, "This operation needs the triage agent's key.")


def curator(request: Request, sent: Annotated[str | None, Depends(_curator_key)]) -> None:
    if not auth.is_curator(sent, request.app.state.settings):
        raise Refusal(401, "This operation needs the curator's key.")


Analyst = Depends(analyst)
PlaybookOwner = Depends(playbook_owner)
TriageAgentKey = Depends(triage_agent)
CuratorKey = Depends(curator)
