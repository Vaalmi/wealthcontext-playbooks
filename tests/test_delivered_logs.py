"""Nothing a caller sends reaches a container's log: each app, started as its image starts it."""

import json
import os
import re
import socket
import subprocess
import sys
import time

import firm
import httpx2
import pytest
from layer import AGENT_KEY, ANALYST, CURATOR_KEY, OWNER, ROOT
from stand_ins import sign_in

NAME, ACCOUNT, NUMBER = "Quillon", "WC-AC-0002", "20998877"
SERVED = {
    "backend.main:create_app": "/health",
    "curator.entrypoint:create_app": "/ping",
    "triage_agent.entrypoint:create_app": "/ping",
}


def delivered() -> dict[str, list[str]]:
    """Each image's own command, by the app it serves."""
    found = {}
    for line in re.findall(r"^CMD (.+)$", (ROOT / "Dockerfile").read_text(), re.M):
        command = json.loads(line)
        (app,) = [each for each in command if each.endswith(":create_app")]
        found[app] = command
    return found


def test_every_image_that_is_served_is_looked_at():
    assert delivered().keys() == SERVED.keys()


def free_port() -> int:
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        return bound.getsockname()[1]


@pytest.mark.parametrize("app", sorted(SERVED))
def test_nothing_that_was_sent_is_in_the_delivered_servers_log(tmp_path, app):
    port = free_port()
    command = [sys.executable, *delivered()[app][1:]]
    command[command.index("--host") + 1] = "127.0.0.1"
    command[command.index("--port") + 1] = str(port)
    environment = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(ROOT),
        "PYTHONUNBUFFERED": "1",
        "LANGSMITH_TRACING": "false",
        "PLAYBOOKS_DATABASE": str(tmp_path / "logs.sqlite"),
        "PLAYBOOKS_FOLDER": str(firm.copy(tmp_path / "playbooks")),
        "PLAYBOOKS_TRIAGE_LOG": str(tmp_path / "triage-log.jsonl"),
        "PLAYBOOKS_CASES": str(tmp_path / "cases"),
        "PLAYBOOKS_SOURCES": str(ROOT / "sources"),
        "PLAYBOOKS_ANALYST_TOKEN": ANALYST,
        "PLAYBOOKS_OWNER_TOKEN": OWNER,
        "TRIAGE_AGENT_KEY": AGENT_KEY,
        "CURATOR_KEY": CURATOR_KEY,
        "WEALTHCONTEXT_TOKEN": sign_in(),
        "WEALTHCONTEXT_URL": "http://127.0.0.1:9",
        "PLAYBOOKS_BACKEND_URL": "http://127.0.0.1:9",
        # The recorded cycle's button is offered, and refused for want of its files here.
        "PLAYBOOKS_CYCLE": "recorded",
    }
    log = tmp_path / "server.log"
    with log.open("w") as written:
        process = subprocess.Popen(
            command, cwd=tmp_path, env=environment, stdout=written, stderr=subprocess.STDOUT
        )
        try:
            base = f"http://127.0.0.1:{port}"
            with httpx2.Client(base_url=base, timeout=5, trust_env=False) as http:
                until = time.monotonic() + 20
                while True:
                    try:
                        assert http.get(SERVED[app]).status_code == 200
                        break
                    except httpx2.TransportError:
                        assert process.poll() is None and time.monotonic() < until, log.read_text()
                        time.sleep(0.05)
                send_the_plants(http, SERVED[app])
            malformed(port)
        finally:
            process.terminate()
            process.wait(10)

    said = log.read_text()
    assert "Application startup complete" in said, "the server's log was not captured"
    for plant in (NAME, ACCOUNT, NUMBER):
        assert plant not in said, f"{app} logged what was sent: start it with --no-access-log"


def send_the_plants(http: httpx2.Client, public: str) -> None:
    query = {"person": f"{NAME} Vantree", "account": ACCOUNT}
    http.get(public, params=query)
    http.get(f"/{NAME}/{ACCOUNT}/{NUMBER}")
    # Refused, and refused as a person: a refusal is logged like any other answer.
    for headers in ({}, {"Authorization": f"Bearer {OWNER}"}, {"X-Curator-Key": CURATOR_KEY}):
        http.get(f"/api/curator-runs/{ACCOUNT}", params=query, headers=headers)
        http.get(f"/api/agent/curator-runs/{ACCOUNT}/attempts/1", headers=headers)
        http.post("/invocations", json={ACCOUNT: NAME}, params=query, headers=headers)
    # A proposal handed in, carrying each: checked, refused, and kept by its hash alone.
    change = {
        "change": "remove",
        "playbook": "feed-outage",
        "version": 1,
        "step": f"{NAME} {ACCOUNT}",
        "rests_on": [NUMBER],
    }
    proposal = {"changes": [change], "disagreements": []}
    person = {"Authorization": f"Bearer {ANALYST}"}
    assert http.post("/api/checked-proposals", json=proposal, headers=person).status_code in (
        200,
        404,
    )
    http.post(f"/api/proposals/{ACCOUNT}/gate-runs", params=query, headers=person)
    http.post(f"/api/proposals/{ACCOUNT}/approve", json={ACCOUNT: NAME}, headers=person)
    owner = {"Authorization": f"Bearer {OWNER}"}
    http.get(f"/api/proposals/{ACCOUNT}", params=query, headers=owner)
    http.post(f"/api/proposals/{ACCOUNT}/refuse", json={"reason": NAME}, headers=owner)
    ruling = {"change": 1, "between": [NAME.lower(), ACCOUNT], "ruled_for": NAME.lower()}
    ruled = {"sha256": "0" * 64, "gate_run": "0" * 32, "rulings": [ruling]}
    http.post(f"/api/proposals/{ACCOUNT}/approve", json=ruled, headers=owner)
    for headers in ({}, person, owner):
        http.get("/api/founding", params=query, headers=headers)
        http.post("/api/founding/approve", json={"sha256": NAME, ACCOUNT: NUMBER}, headers=headers)
    # The recorded cycle's button and its state, the lists of runs, as the page's register reads.
    for headers in ({}, {"Origin": f"http://{NAME}.example"}, {"Sec-Fetch-Site": NAME}):
        http.post("/cycle", params=query, headers=headers)
    http.get("/api/cycle", params=query)
    http.get("/api/triage-runs", params=query, headers=owner)
    http.get("/api/curator-runs", params=query, headers=owner)
    http.get(public, headers={"User-Agent": f"{NAME} {NUMBER}"})
    http.get(public, headers={"Host": f"{NAME}.example"})


def malformed(port: int) -> None:
    for sent in (
        f"{NAME} {ACCOUNT} {NUMBER}\r\n\r\n",
        f"GET /{ACCOUNT} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: upgrade\r\n"
        f"Upgrade: {NAME}\r\n\r\n",
    ):
        with socket.create_connection(("127.0.0.1", port), 5) as connection:
            connection.sendall(sent.encode())
            connection.settimeout(1)
            try:
                connection.recv(4096)
            except OSError:
                pass
