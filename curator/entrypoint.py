"""The curator's app: it answers at once and works on a thread of its own."""

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

from curator import harness
from curator.connection import Connection
from modelkit import models

log = logging.getLogger(__name__)
# A proposal is far longer than a choice of step.
MAX_OUTPUT_TOKENS = 8192
# With its retries, a call still ends inside the five minutes after which a run reads stalled.
TIMEOUT_SECONDS = 90


def configured(setting: str, environment: Mapping[str, str]) -> models.Configured:
    return models.configured(
        setting, environment, max_output_tokens=MAX_OUTPUT_TOKENS, timeout=TIMEOUT_SECONDS
    )


@dataclass(frozen=True)
class Settings:
    backend_url: str
    model: str
    cap: Decimal
    key: str = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] = os.environ) -> "Settings":
        cap = Decimal(environment.get("CURATOR_MAX_DOLLARS", "2"))
        if not cap.is_finite() or cap <= 0:
            raise ValueError("CURATOR_MAX_DOLLARS is an amount above zero")
        return cls(
            backend_url=environment.get("PLAYBOOKS_BACKEND_URL", "http://backend:8000"),
            model=environment.get("CURATOR_MODEL", ""),
            cap=cap,
            key=environment.get("CURATOR_KEY", ""),
            environment=environment,
        )


class CurationAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["curation"]
    run_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    attempt: int = Field(ge=1, le=1_000_000)
    # The model the backend recorded on the run. Another is never worked under that name.
    model: Annotated[str, StringConstraints(max_length=200)]


def create_app(
    settings: Settings | None = None,
    configure: Callable[[str, Mapping[str, str]], models.Configured] = configured,
) -> FastAPI:
    given = settings or Settings.from_environment()
    working: set[str] = set()
    lock = threading.Lock()
    # A replay or a recording is one model for all the app's asks (`Configured.lasting`).
    kept: dict[str, models.Configured] = {}
    app = FastAPI(
        title="Curator",
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
        return JSONResponse({"detail": "The curator could not start."}, status_code=500)

    def work(asked: CurationAsk, chosen: models.Configured) -> None:
        connection = Connection(given.backend_url, given.key)
        try:
            harness.curate(connection, chosen, asked.run_id, asked.attempt, given.cap)
        finally:
            connection.close()
            with lock:
                working.discard(asked.run_id)

    @app.get("/ping", operation_id="ping", description="Say that the curator is up.")
    def ping() -> dict:
        return {"status": "Healthy"}

    @app.post("/invocations", operation_id="invocations", description="Ask for a curator run.")
    def invocations(
        asked: CurationAsk, x_curator_key: Annotated[str | None, Header()] = None
    ) -> JSONResponse:
        sent = (x_curator_key or "").encode()
        if not given.key or not secrets.compare_digest(sent, given.key.encode()):
            return JSONResponse({"detail": "The curator's key is needed."}, status_code=401)
        # Refused before any work starts, so that the backend can undo the run.
        if asked.model != given.model:
            return JSONResponse({"refused": "other_model"})
        try:
            with lock:
                chosen = kept.get(given.model) or configure(given.model, given.environment)
                if chosen.lasting:
                    kept[given.model] = chosen
        except models.Unavailable as unavailable:
            return JSONResponse({"refused": unavailable.reason})
        with lock:
            if asked.run_id in working:
                return JSONResponse({"refused": "already_working"})
            working.add(asked.run_id)
        try:
            threading.Thread(target=work, args=(asked, chosen), daemon=True).start()
        except RuntimeError:
            with lock:
                working.discard(asked.run_id)
            raise
        return JSONResponse({"accepted": True})

    return app
