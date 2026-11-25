"""The curator run's state machine: each move, each refusal, and who may make it."""

import threading
import time

import curation
import knowledge
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from layer import CURATOR_KEY, address, listen
from stand_ins import idle_agent

from curator import entrypoint
from modelkit import models

NOT_STALLED = (
    "The curator run is working and has not stalled. "
    "It can be started again when nothing has been heard for five minutes."
)


@pytest.fixture
def later(monkeypatch):
    return curation.later(monkeypatch)


@pytest.fixture
def idle(make_layer):
    return make_layer(agent=lambda *_: idle_agent(), curator=curation.idle)


def nowhere() -> str:
    bound = listen()
    url = address(bound)
    bound.close()
    return url


def asked_for(answer):
    """A curator that keeps what it was asked, and answers so."""
    asked: list[dict] = []

    def make(*_):
        app = FastAPI()

        @app.post("/invocations")
        def invocations(sent: dict):
            asked.append(sent)
            return answer()

        return app

    return make, asked


def test_a_run_records_the_curators_configured_model_before_it_asks(make_layer):
    make, asked = asked_for(lambda: {"accepted": True})
    layer = make_layer(curator=make, curation_model="anthropic:claude-opus-5-5")

    response = curation.curate(layer)

    assert response.status_code == 202
    run_id = response.json()["id"]
    assert asked == [
        {"kind": "curation", "run_id": run_id, "attempt": 1, "model": "anthropic:claude-opus-5-5"}
    ]
    assert curation.read(layer, run_id)["model"] == "anthropic:claude-opus-5-5"
    line = next(x for x in knowledge.ledger(layer) if x["event"] == "curator_run_started")
    assert line["data"]["model"] == "anthropic:claude-opus-5-5"


def test_only_the_playbook_owner_starts_a_run(idle):
    path = "/api/curator-runs"

    assert idle.as_analyst("POST", path).status_code == 403
    assert idle.as_agent("POST", path).status_code == 401
    assert idle.as_curator("POST", path).status_code == 401
    assert idle.request("POST", path).status_code == 401
    assert "curator_run_started" not in knowledge.events(idle)


def test_neither_agents_key_opens_the_others_operation(idle):
    run_id = curation.started(idle)
    triage_run = idle.start()["id"]

    assert idle.as_agent("GET", curation.work_path(run_id)).status_code == 401
    body = {"turn": 1, "usage": curation.USAGE}
    path = curation.work_path(run_id, "/no-change")
    assert idle.as_agent("POST", path, json=body).status_code == 401
    of_triage = f"/api/agent/triage-runs/{triage_run}/attempts/1"
    assert idle.as_curator("GET", of_triage).status_code == 401
    assert curation.read(idle, run_id)["state"] == "working"


@pytest.mark.parametrize("setting", ["", "not a model", "anthropic:claude-20998877"])
def test_with_no_model_that_can_be_recorded_nothing_is_written(make_layer, setting):
    make, asked = asked_for(lambda: {"accepted": True})
    layer = make_layer(curator=make, curation_model=setting)
    before = knowledge.events(layer)

    response = curation.curate(layer)

    assert response.status_code == 503
    assert "Nothing was started" in response.json()["detail"]
    assert asked == [] and knowledge.events(layer) == before


def test_with_the_curator_down_the_answer_is_503_and_the_run_is_undone(make_layer):
    layer = make_layer(curator_url=nowhere())

    response = curation.curate(layer)

    assert response.status_code == 503
    assert response.json() == {"detail": "The curator cannot be reached. The run was undone."}
    assert knowledge.events(layer)[-2:] == ["curator_run_started", "curator_run_undone"]
    run_id = knowledge.ledger(layer)[-1]["data"]["run_id"]
    assert layer.as_owner("GET", f"/api/curator-runs/{run_id}").status_code == 404
    assert curation.curate(layer).status_code == 503  # not a 409: no run is working


@pytest.mark.parametrize("setting", ["anthropic:claude-opus-5-5", "replay:no-such-file.json"])
def test_with_no_key_and_no_replay_file_the_run_is_undone(make_layer, setting):
    layer = make_layer(curation_model=setting)

    response = curation.curate(layer)

    assert response.status_code == 503
    assert "The curator has no model" in response.json()["detail"]
    assert "The run was undone." in response.json()["detail"]
    assert knowledge.events(layer)[-1] == "curator_run_undone"


def test_a_model_with_no_price_undoes_the_run(make_layer):
    def curator(url: str, key: str):
        settings = entrypoint.Settings(
            backend_url=url,
            model="anthropic:unpriced",
            cap=2,  # type: ignore[arg-type]
            key=key,
            environment={"ANTHROPIC_API_KEY": "not-a-key"},
        )
        return entrypoint.create_app(settings)

    layer = make_layer(curator=curator, curation_model="anthropic:unpriced")

    response = curation.curate(layer)

    assert response.status_code == 503 and "no price" in response.json()["detail"]


@pytest.mark.parametrize(
    ("model", "said"),
    [
        ("openai:gpt-6.2-sol:none", "takes no such reasoning effort as CURATOR_MODEL names"),
        ("anthropic:claude-opus-5-5:high", "takes no such reasoning effort as CURATOR_MODEL names"),
    ],
)
def test_an_effort_the_curators_model_does_not_take_undoes_the_run(make_layer, model, said):
    def curator(url: str, key: str):
        settings = entrypoint.Settings(
            backend_url=url,
            model=model,
            cap=2,  # type: ignore[arg-type]
            key=key,
            environment={"ANTHROPIC_API_KEY": "not-a-key", "OPENAI_API_KEY": "not-a-key"},
        )
        return entrypoint.create_app(settings)

    layer = make_layer(curator=curator, curation_model=model)

    response = curation.curate(layer)

    assert response.status_code == 503 and said in response.json()["detail"]
    assert knowledge.events(layer)[-1] == "curator_run_undone"


def test_a_curator_on_another_model_than_the_one_recorded_does_not_start(make_layer):
    def curator(url: str, key: str):
        settings = entrypoint.Settings(url, "stub:another", 2, key, {})  # type: ignore[arg-type]
        return entrypoint.create_app(settings, lambda *_: pytest.fail("a model was built"))

    layer = make_layer(curator=curator)

    response = curation.curate(layer)

    assert response.status_code == 503
    assert "configured with another model" in response.json()["detail"]
    assert knowledge.events(layer)[-1] == "curator_run_undone"


@pytest.mark.parametrize(
    "answer",
    [
        lambda: JSONResponse({"detail": "no"}, status_code=500),
        lambda: {"refused": "something else entirely WC-AC-0002"},
    ],
)
def test_a_refusal_to_start_undoes_the_run_and_repeats_nothing(make_layer, answer):
    make, _ = asked_for(answer)
    layer = make_layer(curator=make)

    response = curation.curate(layer)

    assert response.status_code == 503
    assert response.json() == {"detail": "The curator refused to start. The run was undone."}


def test_a_timeout_while_asking_keeps_the_run_until_it_stalls(make_layer, monkeypatch, later):
    monkeypatch.setattr("backend.asking.ANSWER_SECONDS", 0.2)

    def slow():
        time.sleep(0.6)
        return {"accepted": True}

    make, _ = asked_for(slow)
    layer = make_layer(curator=make)

    response = curation.curate(layer)

    assert response.status_code == 202 and response.json()["asked"] == "unknown"
    run_id = response.json()["id"]
    assert curation.read(layer, run_id)["state"] == "working"
    later(minutes=5)
    assert curation.read(layer, run_id)["stalled"]


def test_one_run_works_at_a_time(idle):
    curation.started(idle)

    response = curation.curate(idle)

    assert response.status_code == 409
    assert response.json() == {"detail": NOT_STALLED}
    assert knowledge.events(idle).count("curator_run_started") == 1


def test_two_starts_at_once_make_one_run(idle):
    found = []
    threads = [
        threading.Thread(target=lambda: found.append(curation.curate(idle).status_code))
        for _ in range(2)
    ]
    for each in threads:
        each.start()
    for each in threads:
        each.join()

    assert sorted(found) == [202, 409]


def test_no_change_ends_the_run_with_no_proposal(idle):
    run_id = curation.started(idle)

    response = curation.no_change(idle, run_id)

    assert response.status_code == 200
    assert response.json() == {"state": "completed_no_change"}
    found = curation.read(idle, run_id)
    assert (found["state"], found["proposal"]) == ("completed_no_change", None)
    assert found["finished_at"]
    assert [(each["tool"], each["outcome"]) for each in found["steps"]] == [
        ("no_change", "accepted")
    ]


def test_a_second_proposal_for_one_run_is_refused(idle):
    curation.first_cycle(idle)
    run_id = curation.started(idle)
    first = curation.propose(idle, run_id, curation.proposed())
    assert first.status_code == 200

    second = curation.propose(idle, run_id, curation.one())

    assert second.status_code == 409
    assert second.json() == {"detail": "The curator run is completed: it takes one answer."}
    found = curation.read(idle, run_id)
    assert found["proposal"]["id"] == first.json()["id"]
    assert [each["outcome"] for each in found["steps"]] == ["accepted", "refused"]
    assert knowledge.events(idle).count("proposal_recorded") == 1


@pytest.mark.parametrize("first", ["no change", "failed"])
def test_a_run_that_ended_takes_no_proposal(idle, first):
    curation.first_cycle(idle)
    run_id = curation.started(idle)
    if first == "failed":
        assert curation.fail(idle, run_id).status_code == 200
        state = "failed"
    else:
        assert curation.no_change(idle, run_id).status_code == 200
        state = "completed_no_change"

    for response in (
        curation.propose(idle, run_id, curation.proposed()),
        curation.no_change(idle, run_id, turn=2),
        curation.unusable(idle, run_id),
        curation.fail(idle, run_id, "model_failed"),
    ):
        assert response.status_code == 409
        assert state in response.json()["detail"]
    assert curation.read(idle, run_id)["proposal"] is None


def test_the_same_move_sent_again_changes_nothing(idle):
    curation.first_cycle(idle)
    run_id = curation.started(idle)
    first = curation.propose(idle, run_id, curation.proposed())
    count = len(knowledge.ledger(idle))

    again = curation.propose(idle, run_id, curation.proposed())

    assert again.status_code == 200 and again.json() == first.json()
    assert len(knowledge.ledger(idle)) == count


@pytest.mark.parametrize("move", ["refused", "unusable", "no change", "fail"])
def test_each_other_move_sent_again_changes_nothing(idle, move):
    run_id = curation.started(idle)
    moves = {
        "refused": lambda: curation.propose(idle, run_id, curation.one()),
        "unusable": lambda: curation.unusable(idle, run_id),
        "no change": lambda: curation.no_change(idle, run_id),
        "fail": lambda: curation.fail(idle, run_id),
    }
    first = moves[move]()
    count = len(knowledge.ledger(idle))

    again = moves[move]()

    assert (again.status_code, again.json()) == (first.status_code, first.json())
    assert len(knowledge.ledger(idle)) == count


def test_the_second_answer_is_the_last(idle):
    run_id = curation.started(idle)
    assert curation.unusable(idle, run_id).status_code == 200
    assert curation.propose(idle, run_id, curation.one(), turn=2).status_code == 422

    third = curation.no_change(idle, run_id, turn=2)

    assert third.status_code == 409
    assert curation.fail(idle, run_id, "refused_twice").status_code == 200
    found = curation.read(idle, run_id)
    assert found["state"] == "failed"
    assert found["failure"].startswith("The curator's answer was refused twice.")
    assert "not in the bundle" in found["failure"]


def test_refused_twice_follows_a_second_refusal_only(idle):
    run_id = curation.started(idle)
    assert curation.unusable(idle, run_id).status_code == 200

    response = curation.fail(idle, run_id, "refused_twice")

    assert response.status_code == 409
    assert curation.read(idle, run_id)["state"] == "working"


def test_a_turn_out_of_order_is_refused_and_counts_for_nothing(idle):
    run_id = curation.started(idle)

    assert curation.no_change(idle, run_id, turn=2).status_code == 409

    assert curation.work(idle, run_id)["refusals"] == []
    assert len(curation.read(idle, run_id)["steps"]) == 1
    assert curation.no_change(idle, run_id).status_code == 200


def test_an_unknown_run_is_404_and_an_attempt_never_made_records_nothing(idle):
    run_id = curation.started(idle)
    count = len(knowledge.ledger(idle))

    assert curation.no_change(idle, "0" * 32).status_code == 404
    assert curation.no_change(idle, run_id, attempt=7).status_code == 409
    assert len(knowledge.ledger(idle)) == count


# Started again


def test_nothing_heard_for_five_minutes_starts_the_same_run_again_on_the_same_bundle(idle, later):
    curation.first_cycle(idle)
    run_id = curation.started(idle)
    given = curation.work(idle, run_id)["items"]
    bundle = curation.read(idle, run_id)["bundle"]
    # What has happened since is in no bundle of this run.
    assert knowledge.intake(idle, knowledge.committed_memo("custody-meeting-notice")).is_success
    knowledge.settled_run(idle)

    later(minutes=4, seconds=50)
    assert curation.curate(idle).status_code == 409
    later(seconds=10)
    assert curation.read(idle, run_id)["stalled"]
    again = curation.curate(idle)

    assert again.status_code == 202
    assert (again.json()["id"], again.json()["attempt"]) == (run_id, 2)
    assert curation.work(idle, run_id, attempt=2)["items"] == given
    found = curation.read(idle, run_id)
    assert found["bundle"] == bundle and not found["stalled"]
    assert curation.curate(idle).status_code == 409


def test_a_refused_call_is_hearing_from_the_curator(idle, later):
    run_id = curation.started(idle)
    later(minutes=4)
    assert curation.propose(idle, run_id, curation.one()).status_code == 422
    later(minutes=2)

    assert not curation.read(idle, run_id)["stalled"]
    assert curation.curate(idle).status_code == 409


def test_a_call_out_of_place_is_not_hearing_from_it(idle, later):
    run_id = curation.started(idle)
    later(minutes=4)
    assert curation.no_change(idle, run_id, turn=2).status_code == 409
    later(minutes=1)

    assert curation.read(idle, run_id)["stalled"]


def test_an_old_attempt_cannot_end_a_run_that_was_started_again(idle, later):
    curation.first_cycle(idle)
    run_id = curation.started(idle)
    later(minutes=5)
    assert curation.curate(idle).status_code == 202

    for response in (
        curation.propose(idle, run_id, curation.proposed()),
        curation.no_change(idle, run_id),
        curation.unusable(idle, run_id),
        curation.fail(idle, run_id),
        idle.as_curator("GET", curation.work_path(run_id)),
    ):
        assert response.status_code == 409
        assert "not the current one" in response.json()["detail"]
    assert curation.no_change(idle, run_id, attempt=2).status_code == 200


def test_an_old_attempt_cannot_end_a_run_whose_restart_was_undone(make_layer, later):
    first = make_layer(agent=lambda *_: idle_agent(), curator=curation.idle)
    curation.first_cycle(first)
    run_id = curation.started(first)
    later(minutes=5)
    down = make_layer(after=first, agent=lambda *_: idle_agent(), curator_url=nowhere())

    undone = curation.curate(down)

    assert undone.status_code == 503
    assert "Starting it again was undone" in undone.json()["detail"]
    for response in (
        curation.propose(down, run_id, curation.proposed()),
        curation.no_change(down, run_id),
        curation.unusable(down, run_id),
        curation.fail(down, run_id),
    ):
        assert response.status_code == 409, response.text
        assert "not the current one" in response.json()["detail"]
    found = curation.read(down, run_id)
    assert (found["state"], found["attempt"], found["stalled"]) == ("working", 2, True)
    assert found["proposal"] is None
    up = make_layer(after=down, agent=lambda *_: idle_agent(), curator=curation.idle)
    assert curation.curate(up).json()["attempt"] == 3


def test_starting_again_records_the_model_then_configured(make_layer, later):
    first = make_layer(curator=curation.idle)
    run_id = curation.started(first)
    later(minutes=5)
    down = make_layer(after=first, curator_url=nowhere(), curation_model="stub:second")

    assert curation.curate(down).status_code == 503
    assert curation.read(down, run_id)["model"] == "stub:curation"
    up = make_layer(after=down, curator=curation.idle, curation_model="stub:second")
    assert curation.curate(up).status_code == 202
    assert curation.read(up, run_id)["model"] == "stub:second"


def test_two_refusals_count_over_attempts(idle, later):
    run_id = curation.started(idle)
    assert curation.unusable(idle, run_id).status_code == 200
    later(minutes=5)
    assert curation.curate(idle).status_code == 202

    assert len(curation.work(idle, run_id, attempt=2)["refusals"]) == 1
    assert curation.no_change(idle, run_id, attempt=2).status_code == 409
    assert curation.unusable(idle, run_id, turn=2, attempt=2).status_code == 200
    assert curation.fail(idle, run_id, "refused_twice", attempt=2).status_code == 200


def test_a_run_that_ended_gives_out_its_bundle_no_more(idle):
    run_id = curation.started(idle)
    assert curation.work(idle, run_id)["items"]
    assert curation.no_change(idle, run_id).status_code == 200

    assert curation.work(idle, run_id)["items"] == []


def test_the_curator_cannot_claim_a_failure_that_is_the_backends(idle):
    run_id = curation.started(idle)

    assert curation.fail(idle, run_id, "held_out").status_code == 422
    assert curation.read(idle, run_id)["state"] == "working"


def test_a_new_run_can_follow_one_that_ended(idle):
    first = curation.started(idle)
    assert curation.no_change(idle, first).status_code == 200

    assert curation.started(idle) != first


def test_the_curators_app_takes_only_the_curators_key():
    from decimal import Decimal

    from fastapi.testclient import TestClient

    given = entrypoint.Settings("http://127.0.0.1:9", "", Decimal(2), CURATOR_KEY, {})
    app = TestClient(entrypoint.create_app(given))
    asked = {"kind": "curation", "run_id": "0" * 32, "attempt": 1, "model": ""}

    assert app.post("/invocations", json=asked).status_code == 401
    other = {"X-Triage-Agent-Key": CURATOR_KEY}
    assert app.post("/invocations", json=asked, headers=other).status_code == 401
    malformed = app.post("/invocations", json=asked | {"run_id": "WC-AC-0002"})
    assert malformed.status_code == 422 and "WC-AC-0002" not in malformed.text
    ours = app.post("/invocations", json=asked, headers={"X-Curator-Key": CURATOR_KEY})
    assert ours.json() == {"refused": "no_model"}
    assert app.get("/ping").json() == {"status": "Healthy"}


def test_the_curators_cap_is_an_amount_above_zero():
    for wrong in ("0", "-1", "nan"):
        with pytest.raises(ValueError):
            entrypoint.Settings.from_environment({"CURATOR_MAX_DOLLARS": wrong})


def test_the_curators_settings_come_from_the_environment():
    found = entrypoint.Settings.from_environment(
        {"CURATOR_MODEL": "replay:x.json", "CURATOR_KEY": "key"}
    )

    assert (found.model, found.key, str(found.cap)) == ("replay:x.json", "key", "2")
    assert isinstance(models.MAX_OUTPUT_TOKENS, int) and entrypoint.MAX_OUTPUT_TOKENS > 4096
