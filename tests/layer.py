"""The layer as the tests start it: real apps on real loopback ports, and databases of their own."""

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import firm
import httpx2
import uvicorn
from fastapi import FastAPI
from stand_ins import StandInWealthContext
from stub_model import StubModel, stub

from backend.main import create_app
from backend.settings import Settings
from curator import entrypoint as curator_app
from modelkit import models
from triage_agent import entrypoint

ROOT = Path(__file__).resolve().parent.parent
DAY, CUTOFF = "2026-10-15", "2026-10-31T16:00:00Z"
OPTION = "feed-outage/portfolio-feed-failed"
CROSS_CHECK_DAY = "2026-10-09"
CROSS_CHECK_OPTION = "missing-cross-check/custody-cross-check-missing"
GROUPING_DAY = "2026-10-20"
GAP_TOOLS = ("file_gap_report", "no_gap_report")
ANALYST = "analyst-token-for-tests"
OWNER = "owner-token-for-tests"
AGENT_KEY = "triage-agent-key-for-tests"
CURATOR_KEY = "curator-key-for-tests"
CURATOR_MODEL = "stub:curation"
# The name the stub triage model answers under, and so the gate's model for its trials.
TRIAGE_MODEL = "stub:triage"


def choices(run: dict) -> list[dict]:
    """A run's steps but those that answer whether to file a gap report."""
    return [step for step in run["steps"] if step["tool"] not in GAP_TOOLS]


def listen() -> socket.socket:
    bound = socket.socket()
    bound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    bound.bind(("127.0.0.1", 0))
    bound.listen(128)
    return bound


def address(bound: socket.socket) -> str:
    return f"http://127.0.0.1:{bound.getsockname()[1]}"


class Server(uvicorn.Server):
    async def main_loop(self) -> None:
        # As uvicorn's, with a shorter tick: a test starts and stops several servers.
        counter = 0
        while not await self.on_tick(counter):
            counter = (counter + 1) % 864000
            await asyncio.sleep(0.01)


@contextmanager
def serve(*apps: tuple[FastAPI, socket.socket]) -> Iterator[None]:
    running = []
    for app, bound in apps:
        server = Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [bound]}, daemon=True)
        thread.start()
        running.append((server, thread, bound))
    until = time.monotonic() + 10
    for server, thread, _ in running:
        while not server.started:
            assert thread.is_alive() and time.monotonic() < until, "a server did not start"
            time.sleep(0.005)
    try:
        yield
    finally:
        # All told at once: each takes a tick of uvicorn's to notice.
        for server, _, _ in running:
            server.should_exit = True
        for _, thread, bound in running:
            thread.join(10)
            bound.close()


@dataclass
class Layer:
    url: str
    wealthcontext: StandInWealthContext
    settings: Settings
    backend: FastAPI
    stubs: list[StubModel] = field(default_factory=list)
    curator_stubs: list[StubModel] = field(default_factory=list)

    def request(self, method: str, path: str, headers: dict | None = None, **options):
        with httpx2.Client(base_url=self.url, timeout=30, trust_env=False) as http:
            return http.request(method, path, headers=headers or {}, **options)

    def as_analyst(self, method: str, path: str, **options):
        return self.request(method, path, {"Authorization": f"Bearer {ANALYST}"}, **options)

    def as_owner(self, method: str, path: str, **options):
        return self.request(method, path, {"Authorization": f"Bearer {OWNER}"}, **options)

    def as_agent(self, method: str, path: str, **options):
        return self.request(method, path, {"X-Triage-Agent-Key": AGENT_KEY}, **options)

    def as_curator(self, method: str, path: str, **options):
        return self.request(method, path, {"X-Curator-Key": CURATOR_KEY}, **options)

    def put_in_use(self, changed: dict) -> None:
        """New versions, as an approval puts them in use."""
        firm.put_in_use(self.backend.state.journal, self.backend.state.store, changed)

    def retire(self, playbook: str, version: int, reason: str = "wrong_action"):
        path = f"/api/playbooks/{playbook}/versions/{version}/retire"
        return self.as_owner("POST", path, json={"reason": reason})

    def start(self, day: str = DAY, cutoff: str = CUTOFF) -> dict:
        response = self.as_analyst("POST", "/api/triage-runs", json={"date": day, "cutoff": cutoff})
        assert response.status_code == 202, response.text
        return response.json()

    def run(self, run_id: str) -> dict:
        response = self.as_analyst("GET", f"/api/triage-runs/{run_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def triage(self, day: str = DAY, cutoff: str = CUTOFF) -> dict:
        return self.ended(self.start(day, cutoff)["id"])

    def ended(self, run_id: str) -> dict:
        until = time.monotonic() + 15
        while (found := self.run(run_id))["state"] == "working":
            assert time.monotonic() < until, "the triage run did not end"
            time.sleep(0.02)
        return found


@contextmanager
def running_layer(
    folder: Path,
    *,
    replies: Callable[[], Sequence] | None = None,
    gaps: Callable[[list], object] | None = None,
    model: str = TRIAGE_MODEL,
    price=None,
    cap: str = "2",
    agent: Callable[[str, str], FastAPI] | None = None,
    agent_url: str | None = None,
    playbooks: Path | None = None,
    overrides: dict | None = None,
    curates: Callable[[], Sequence] | None = None,
    curation_model: str = CURATOR_MODEL,
    curation_price=None,
    curation_cap: str = "2",
    curator: Callable[[str, str], FastAPI] | None = None,
    curator_url: str | None = None,
    choosing: Callable[[], StubModel] | None = None,
) -> Iterator[Layer]:
    """`replies` scripts a stub model's choices for each run, and `curates` its answers for
    each curator run. `gaps` gives its answer to each
    question of whether to file a gap report, a run's or a review's, from what it was asked.
    `model` is a real setting, read as it is. `choosing` makes the stub for each run, one
    that reads what it is asked.
    """
    if playbooks is None:
        playbooks = folder / "playbooks"
        # A layer started again on its folder finds the playbooks it left. A new one has the
        # committed folder, whose founding versions the playbook owner has approved.
        if not playbooks.exists():
            firm.copy(playbooks)
    theirs, ours, agents, curators = listen(), listen(), listen(), listen()
    wealthcontext = StandInWealthContext()
    values = {
        "database": folder / "playbooks.sqlite",
        "playbooks": playbooks,
        "triage_log": folder / "triage-log.jsonl",
        "wealthcontext_url": address(theirs),
        "agent_url": agent_url or address(agents),
        "wealthcontext_token": wealthcontext.token,
        "analyst_token": ANALYST,
        "playbook_owner_token": OWNER,
        "triage_agent_key": AGENT_KEY,
        "curator_key": CURATOR_KEY,
        "curator_url": curator_url or address(curators),
        "curator_model": curation_model,
        # Never the repository's own: a test's cases are fixtures it writes itself.
        "cases": folder / "cases",
        # The repository's firm sources, wherever the test runs from.
        "sources": ROOT / "sources",
        "triage_model": TRIAGE_MODEL,
    }
    settings = Settings(**(values | (overrides or {})))
    settings.cases.mkdir(parents=True, exist_ok=True)
    backend = create_app(settings)
    layer = Layer(
        url=address(ours), wealthcontext=wealthcontext, settings=settings, backend=backend
    )

    def configure(setting, environment) -> models.Configured:
        if choosing is not None:
            layer.stubs.append(choosing())
            return models.Configured(name=TRIAGE_MODEL, model=layer.stubs[-1], price=price)
        if replies is None:
            return models.configured(setting, environment)
        layer.stubs.append(stub(*replies(), gaps=gaps))
        return models.Configured(name=TRIAGE_MODEL, model=layer.stubs[-1], price=price)

    agent_app = (
        agent(address(ours), AGENT_KEY)
        if agent
        else entrypoint.create_app(
            entrypoint.Settings(
                backend_url=address(ours),
                model=model,
                cap=Decimal(cap),
                key=AGENT_KEY,
                environment={},
            ),
            configure,
        )
    )

    def configure_curator(setting, environment) -> models.Configured:
        if curates is None:
            return curator_app.configured(setting, environment)
        layer.curator_stubs.append(stub(*curates()))
        return models.Configured(setting, layer.curator_stubs[-1], curation_price)

    curating = (
        curator(address(ours), CURATOR_KEY)
        if curator
        else curator_app.create_app(
            curator_app.Settings(
                backend_url=address(ours),
                model=curation_model,
                cap=Decimal(curation_cap),
                key=CURATOR_KEY,
                environment={},
            ),
            configure_curator,
        )
    )
    with serve(
        (wealthcontext.app, theirs), (backend, ours), (agent_app, agents), (curating, curators)
    ):
        yield layer
