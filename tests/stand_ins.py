"""Stand-ins for WealthContext and for the agents."""

import base64
import copy
import json
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx2
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

FIXTURES = Path(__file__).parent / "fixtures" / "wealthcontext"
# A day made for the grouping rules, in the shapes of WealthContext's published schemas.
# WealthContext never served it.
GROUPING = Path(__file__).parent / "fixtures" / "grouping"


def sign_in(persona: str = "OPS-01", role: str = "ops") -> str:
    """A token in WealthContext's shape. Its signature is the stand-in's, which checks none."""

    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part({'sub': persona, 'role': role})}.stand-in"


def recorded(day: str, operation: str) -> dict:
    return _envelope(FIXTURES / day / f"{operation}.json")


def _envelope(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class StandInWealthContext:
    """Serves the fixtures' envelopes, with a new result and trace ID for each read."""

    def __init__(self):
        self.token = sign_in()
        self.asked: list[tuple[str, dict, str | None]] = []
        self.methods: list[str] = []
        self.served: list[tuple[str, dict]] = []
        self.edit: Callable[[str, dict], dict] = lambda _, envelope: envelope
        self.down = False
        self.down_for: set[str] = set()
        self.app = FastAPI()
        self.app.add_api_route("/{path:path}", self._answer, methods=["GET", "POST", "PUT"])

    async def _answer(self, path: str, request: Request) -> JSONResponse:
        self.methods.append(request.method)
        body = await request.body()
        sent = json.loads(body) if body else {}
        self.asked.append((f"/{path}", sent, request.headers.get("authorization")))
        if self.down or path.removeprefix("api/") in self.down_for:
            return JSONResponse({"nothing": "here"}, status_code=503)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return JSONResponse({"error": {"code": "UNAUTHENTICATED"}}, status_code=401)
        operation = path.removeprefix("api/")
        found = None
        for day in sorted([*FIXTURES.iterdir(), *GROUPING.iterdir()]):
            # A day has one impact for each run that failed.
            for file in sorted(day.glob(f"{operation}*.json")):
                envelope = _envelope(file)
                if operation == "feed_run_impact":
                    matches = envelope["data"]["run_id"] == sent.get("run_id")
                else:
                    matches = day.name == sent.get("as_of")
                if matches:
                    found = copy.deepcopy(envelope)
        if found is None:
            return JSONResponse({"error": {"code": "AS_OF_OUT_OF_RANGE"}}, status_code=422)
        found["result_id"] = f"r_{uuid.uuid4().hex}"
        found["trace_id"] = uuid.uuid4().hex
        found["known_as_of"] = sent["known_as_of"]
        found = self.edit(operation, found)
        self.served.append((operation, found))
        return JSONResponse(found)


def idle_agent() -> FastAPI:
    """An agent that says it started and does nothing, so that a test can make the moves."""
    app = FastAPI()
    app.add_api_route("/invocations", lambda: {"accepted": True}, methods=["POST"])
    return app


def scripted_agent(backend_url: str, key: str) -> FastAPI:
    """An agent that plays a scripted decision through the backend's API, with the agent's key."""
    app = FastAPI()

    def play(asked: dict) -> None:
        base = f"/api/agent/triage-runs/{asked['run_id']}/attempts/{asked['attempt']}"
        usage = {
            "model": "replay:stand-in (scripted)",
            "input_tokens": None,
            "output_tokens": None,
            "cost_usd": None,
        }
        with httpx2.Client(base_url=backend_url, headers={"X-Triage-Agent-Key": key}) as http:
            for note in http.get(base).json()["notes"]:
                only = note["options"][0]
                choice = {"kind": "step", "playbook": only["playbook"], "step": only["step"]}
                body = {"turn": 1, "choice": choice, "usage": usage}
                http.post(f"{base}/notes/{note['id']}/choice", json=body).raise_for_status()
            http.post(f"{base}/complete").raise_for_status()

    @app.post("/invocations")
    def invocations(asked: dict) -> dict:
        threading.Thread(target=play, args=(asked,), daemon=True).start()
        return {"accepted": True}

    return app


def fitting(note: dict) -> dict:
    """The choice a right model makes: the one step whose conditions the exception meets."""
    facts = note["feed"]
    for option in note["options"]:
        if all(v == "any" or (facts and facts[k] == v) for k, v in option["when"].items()):
            return {"kind": "step", "playbook": option["playbook"], "step": option["step"]}
    return {"kind": "no_step"}


def trial_agent(
    backend_url: str,
    key: str,
    choose=fitting,
    *,
    model: str = "stub:triage",
    answers_as: str | None = None,
    cost: str | None = None,
    before=None,
    after=None,
    fails: bool = False,
) -> FastAPI:
    """An agent that plays each held-out trial it is asked, with `choose`, and leaves a triage
    run or a review to the test, as `idle_agent` does. Configured with `model`, it refuses a
    trial the gate asks under another, as the triage agent does; its calls name `answers_as`.
    `before(http, note_path, note)` is called before each choice, and `after(asked)` once the
    trial's end was answered. With `fails`, it fails each trial at once."""
    app = FastAPI()
    usage = {
        "model": answers_as or model,
        "input_tokens": 10,
        "output_tokens": 2,
        "cost_usd": cost,
    }

    def play(asked: dict) -> None:
        base = f"/api/agent/triage-runs/{asked['run_id']}/attempts/{asked['attempt']}"
        with httpx2.Client(base_url=backend_url, headers={"X-Triage-Agent-Key": key}) as http:
            if fails:
                http.post(f"{base}/fail", json={"reason": "model_failed"}).raise_for_status()
                if after is not None:
                    after(asked)
                return
            for note in http.get(base).json()["notes"]:
                if before is not None:
                    before(http, f"{base}/notes/{note['id']}", note)
                body = {"turn": 1, "choice": choose(note), "usage": usage}
                http.post(f"{base}/notes/{note['id']}/choice", json=body).raise_for_status()
            http.post(f"{base}/complete").raise_for_status()
            if after is not None:
                after(asked)

    @app.post("/invocations")
    def invocations(asked: dict) -> dict:
        if asked.get("kind") == "trial":
            if asked["model"] != model:
                return {"refused": "other_model"}
            threading.Thread(target=play, args=(asked,), daemon=True).start()
        return {"accepted": True}

    return app
