"""The triage agent's app: it answers at once and works on a thread of its own."""

import logging
import os
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from modelkit import models
from triage_agent import harness
from triage_agent.connection import Connection

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    backend_url: str
    model: str
    cap: Decimal
    key: str = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] = os.environ) -> "Settings":
        cap = Decimal(environment.get("TRIAGE_MAX_DOLLARS", "2"))
        if not cap.is_finite() or cap <= 0:
            raise ValueError("TRIAGE_MAX_DOLLARS is an amount above zero")
        return cls(
            backend_url=environment.get("PLAYBOOKS_BACKEND_URL", "http://backend:8000"),
            model=environment.get("TRIAGE_MODEL", ""),
            cap=cap,
            key=environment.get("TRIAGE_AGENT_KEY", ""),
            environment=environment,
        )


class TriageAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["triage"]
    run_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    attempt: int = Field(ge=1, le=1_000_000)


class TrialAsk(BaseModel):
    """A gate's held-out trial, worked as a triage run. The gate names the model it will
    record and the most the trial may spend; the agent works under no other model."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["trial"]
    run_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    attempt: int = Field(ge=1, le=1_000_000)
    model: Annotated[str, StringConstraints(max_length=200)]
    cap_usd: Annotated[str, StringConstraints(pattern=r"^[0-9]{1,6}\.[0-9]{6}$")]


class ReviewAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["review"]
    correction_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    attempt: int = Field(ge=1, le=1_000_000)


def create_app(
    settings: Settings | None = None,
    configure: Callable[[str, Mapping[str, str]], models.Configured] = models.configured,
) -> FastAPI:
    given = settings or Settings.from_environment()
    working: set[str] = set()
    lock = threading.Lock()
    # A replay or a recording is one model for all the app's asks (`Configured.lasting`).
    kept: dict[str, models.Configured] = {}
    app = FastAPI(
        title="Triage agent",
        docs_url=None,
        redoc_url=None,
        # A slash redirect would send the request's query back in its Location.
        redirect_slashes=False,
    )

    @app.exception_handler(RequestValidationError)
    async def malformed(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "The request does not fit invocations."}, status_code=422)

    @app.exception_handler(Exception)
    async def broken(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse({"detail": "The triage agent could not start."}, status_code=500)

    def work(
        asked: TriageAsk | TrialAsk | ReviewAsk, configured: models.Configured, on: str
    ) -> None:
        connection = Connection(given.backend_url, given.key)
        try:
            if isinstance(asked, TriageAsk):
                harness.triage(connection, configured, asked.run_id, asked.attempt, given.cap)
            elif isinstance(asked, TrialAsk):
                # The trial's cap is what is left of the gate's, and never more than the agent's.
                cap = min(given.cap, Decimal(asked.cap_usd))
                harness.triage(connection, configured, asked.run_id, asked.attempt, cap)
            else:
                harness.review(
                    connection, configured, asked.correction_id, asked.attempt, given.cap
                )
        finally:
            connection.close()
            with lock:
                working.discard(on)

    @app.get("/ping", operation_id="ping", description="Say that the agent is up.")
    def ping() -> dict:
        return {"status": "Healthy"}

    @app.post(
        "/invocations",
        operation_id="invocations",
        description="Ask for a triage run, a held-out trial or a review.",
    )
    def invocations(
        asked: TriageAsk | TrialAsk | ReviewAsk,
        x_triage_agent_key: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        sent = (x_triage_agent_key or "").encode()
        if not given.key or not secrets.compare_digest(sent, given.key.encode()):
            return JSONResponse({"detail": "The triage agent's key is needed."}, status_code=401)
        # Refused before any work starts, so that the backend can undo the run.
        if isinstance(asked, TrialAsk) and asked.model != given.model:
            return JSONResponse({"refused": "other_model"})
        try:
            with lock:
                configured = kept.get(given.model) or configure(given.model, given.environment)
                if configured.lasting:
                    kept[given.model] = configured
        except models.Unavailable as unavailable:
            return JSONResponse({"refused": unavailable.reason})
        # One lock for each record being worked: a run, or a correction under review.
        on = asked.correction_id if isinstance(asked, ReviewAsk) else asked.run_id
        with lock:
            if on in working:
                return JSONResponse({"refused": "already_working"})
            working.add(on)
        try:
            threading.Thread(target=work, args=(asked, configured, on), daemon=True).start()
        except RuntimeError:
            with lock:
                working.discard(on)
            raise
        return JSONResponse({"accepted": True})

    return app
