"""Asking the agent has three outcomes, with the real backend and agent apps."""

import time

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from layer import CUTOFF, DAY, OPTION, address, choices, listen
from sqlalchemy import create_engine, text
from stub_model import call


def ask(layer):
    return layer.as_analyst("POST", "/api/triage-runs", json={"date": DAY, "cutoff": CUTOFF})


def records(layer) -> int:
    engine = create_engine(f"sqlite:///{layer.settings.database}")
    tables = ("runs", "reads", "exceptions", "notices", "notes", "steps")
    with engine.connect() as connection:
        found = sum(
            connection.execute(text(f"SELECT count(*) FROM triage_{table}")).scalar_one()
            for table in tables
        )
    engine.dispose()
    return found


def test_an_agent_that_is_down_undoes_the_run(make_layer):
    nowhere = listen()
    url = address(nowhere)
    nowhere.close()
    layer = make_layer(agent_url=url)

    response = ask(layer)

    assert response.status_code == 503
    assert response.json() == {"detail": "The triage agent cannot be reached. The run was undone."}
    assert records(layer) == 0


@pytest.mark.parametrize("setting", ["", "anthropic:claude-opus-5-5", "replay:/no/such/file.json"])
def test_with_no_key_and_no_replay_file_a_run_is_refused_before_any_work(make_layer, setting):
    layer = make_layer(model=setting)

    response = ask(layer)

    assert response.status_code == 503
    assert "has no model" in response.json()["detail"]
    assert records(layer) == 0


@pytest.mark.parametrize("status", [401, 409, 500, 503])
def test_an_agent_that_answers_an_error_undoes_the_run(make_layer, status):
    def refusing(_: str, __: str) -> FastAPI:
        app = FastAPI()
        app.add_api_route(
            "/invocations", lambda: JSONResponse({"no": 1}, status_code=status), methods=["POST"]
        )
        return app

    layer = make_layer(agent=refusing)

    assert ask(layer).status_code == 503
    assert records(layer) == 0


def test_a_timeout_while_asking_leaves_the_run(make_layer, monkeypatch):
    monkeypatch.setattr("backend.asking.ANSWER_SECONDS", 0.2)

    def slow(_: str, __: str) -> FastAPI:
        app = FastAPI()

        def answer() -> dict:
            time.sleep(0.6)
            return {"accepted": True}

        app.add_api_route("/invocations", answer, methods=["POST"])
        return app

    layer = make_layer(agent=slow)
    response = ask(layer)

    assert response.status_code == 202
    assert response.json()["asked"] == "unknown"
    assert layer.run(response.json()["id"])["state"] == "working"


@pytest.mark.parametrize("move", ["/choice", "/complete"])
def test_a_lost_response_is_sent_again_and_changes_nothing_twice(make_layer, monkeypatch, move):
    send = httpx2.Client.request
    lost = []

    def losing(self, method, url, **options):
        # Marked before it is sent: the run can end as soon as the backend has the move.
        first = str(url).endswith(move) and not lost
        if first:
            lost.append(url)
        response = send(self, method, url, **options)
        if first:
            raise httpx2.ReadError("the answer was lost")
        return response

    monkeypatch.setattr(httpx2.Client, "request", losing)
    run = make_layer(replies=lambda: [call("choose_step", option=OPTION)]).triage()

    assert lost
    assert run["state"] == "completed"
    assert [note["state"] for note in run["notes"]].count("chosen") == 1
    assert len(choices(run)) == 1


def test_wealthcontext_down_answers_502_and_writes_nothing(make_layer):
    layer = make_layer(replies=list)
    layer.wealthcontext.down = True

    response = ask(layer)

    assert response.status_code == 502
    assert response.json() == {
        "detail": "WealthContext refused get_data_quality_report (HTTP 503)."
    }
    assert records(layer) == 0


def test_wealthcontext_out_of_reach_answers_502(make_layer):
    nowhere = listen()
    url = address(nowhere)
    nowhere.close()
    layer = make_layer(overrides={"wealthcontext_url": url})

    response = ask(layer)

    assert response.status_code == 502
    assert response.json() == {"detail": "WealthContext cannot be reached."}
