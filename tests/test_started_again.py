"""A run that stalls is started again: the same run, a new attempt, one note per exception."""

import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import firm
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from layer import (
    AGENT_KEY,
    ANALYST,
    CROSS_CHECK_OPTION,
    CUTOFF,
    DAY,
    GROUPING_DAY,
    OPTION,
    OWNER,
    address,
    choices,
    listen,
)
from sqlalchemy import create_engine, text
from stand_ins import idle_agent
from stub_model import call, stub

from backend import triage
from cli import main
from modelkit import models
from modelkit.prices import Price
from triage_agent import api, harness
from triage_agent.connection import Connection

USAGE = {"model": "stub:triage", "input_tokens": 1, "output_tokens": 1, "cost_usd": None}
RIGHT = {"kind": "step", "playbook": "feed-outage", "step": "portfolio-feed-failed"}
NONE = {"kind": "no_step"}
ASKED = {"date": DAY, "cutoff": CUTOFF}
NOT_STALLED = (
    "The run is working and has not stalled. "
    "It can be started again when nothing has been heard for five minutes."
)


class Killed(BaseException):
    """The agent's thread dies: nothing is caught and nothing more is sent."""


class Clock:
    def __init__(self):
        self.ahead = timedelta(0)
        self.stopped_at: datetime | None = None

    def __call__(self) -> datetime:
        return (self.stopped_at or datetime.now(UTC)) + self.ahead

    def pass_time(self, **of) -> None:
        self.ahead += timedelta(**of)


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(triage, "_clock", clock)
    return clock


@pytest.fixture
def died(monkeypatch):
    """Waits for the agent's thread to have died, its lock on the run let go."""
    deaths = threading.Semaphore(0)

    def hook(stopped) -> None:
        assert stopped.exc_type is Killed, stopped.exc_value
        deaths.release()

    monkeypatch.setattr(threading, "excepthook", hook)

    def wait() -> None:
        assert deaths.acquire(timeout=10), "the agent did not stop"

    return wait


def start(layer, day: str = DAY, cutoff: str = CUTOFF):
    return layer.as_analyst("POST", "/api/triage-runs", json={"date": day, "cutoff": cutoff})


def path(run_id: str, attempt: int, rest: str = "") -> str:
    return f"/api/agent/triage-runs/{run_id}/attempts/{attempt}{rest}"


def choose(layer, run_id: str, attempt: int, choice: dict, turn: int = 1, usage: dict = USAGE):
    note = layer.run(run_id)["notes"][0]["id"]
    body = {"turn": turn, "choice": choice, "usage": usage}
    return layer.as_agent("POST", path(run_id, attempt, f"/notes/{note}/choice"), json=body)


def rows(layer, table: str) -> list:
    engine = create_engine(f"sqlite:///{layer.settings.database}")
    with engine.connect() as connection:
        found = connection.execute(text(f"SELECT * FROM {table}")).all()
    engine.dispose()
    return sorted(found, key=str)


def scripts(*each_invocation):
    """The replies of each invocation of the agent in turn."""
    given = iter(each_invocation)

    def replies():
        script = next(given)
        if isinstance(script, Exception):
            raise script
        return script

    return replies


# Where a triage agent can stop


def right_for(asked: str):
    if '"feed_facts": null' in asked:
        return call("no_step_applies")
    failed_feed = '"source_family": "PAS"' in asked.split('"steps"')[0]
    return call("choose_step", option=OPTION if failed_feed else CROSS_CHECK_OPTION)


@pytest.mark.parametrize("settled_before", [0, 1, 2, 3])
def test_a_stalled_run_is_started_again_and_has_one_note_per_exception(
    make_layer, clock, died, settled_before
):
    given = []

    def replies():
        first_invocation = not given
        given.append(True)

        def right():
            return right_for(layer.stubs[-1].prompts[-1][-1].text)

        return [right] * settled_before + [Killed()] if first_invocation else [right] * 4

    layer = make_layer(replies=replies)
    started = layer.start(GROUPING_DAY)
    died()
    before = layer.run(started["id"])
    assert before["state"] == "working" and not before["stalled"]
    settled = {n["id"]: n for n in before["notes"] if n["state"] != "pending"}
    assert len(settled) == 1 + settled_before  # one was the backend's to settle

    clock.pass_time(minutes=5)
    assert layer.run(started["id"])["stalled"]
    again = layer.start(GROUPING_DAY)
    run = layer.ended(again["id"])

    assert (again["id"], again["attempt"]) == (started["id"], 2)
    assert run["state"] == "completed" and not run["stalled"]
    assert run["attempt"] == 2
    assert [note["id"] for note in run["notes"]] == [note["id"] for note in before["notes"]]
    in_notes = Counter((n["code"], s) for n in run["notes"] for s in n["subjects"])
    assert set(in_notes.values()) == {1} and len(run["notes"]) == 5
    assert "pending" not in {note["state"] for note in run["notes"]}
    assert all(settled[n["id"]] == n for n in run["notes"] if n["id"] in settled)
    # One side effect each: a note is chosen once, and the model is asked only what was left.
    accepted = Counter(s["note_id"] for s in choices(run) if s["outcome"] == "accepted")
    assert list(accepted.values()) == [1, 1, 1, 1]
    assert len(layer.stubs[1].prompts) == 4 - settled_before
    assert len(rows(layer, "triage_runs")) == 1
    assert len(rows(layer, "triage_notes")) == 5


def killed_once(monkeypatch, operation: str) -> None:
    real, killed = getattr(api, operation), []

    def once(*arguments):
        if not killed:
            killed.append(True)
            raise Killed
        return real(*arguments)

    monkeypatch.setattr(api, operation, once)


def test_an_agent_killed_before_it_read_its_work(make_layer, clock, died, monkeypatch):
    killed_once(monkeypatch, "read_triage_work")
    layer = make_layer(replies=lambda: [call("choose_step", option=OPTION)])
    layer.start()
    died()
    clock.pass_time(minutes=5)

    run = layer.triage()

    assert (run["state"], run["attempt"]) == ("completed", 2)
    assert len(run["notes"]) == 7


def test_an_agent_killed_before_completing_only_completes(make_layer, clock, died, monkeypatch):
    killed_once(monkeypatch, "complete_triage")
    layer = make_layer(replies=scripts([call("choose_step", option=OPTION)], []))
    layer.start()
    died()
    clock.pass_time(minutes=5)

    run = layer.triage()

    assert run["state"] == "completed"
    assert layer.stubs[1].prompts == []
    assert len(choices(run)) == 1


def test_a_refusal_before_the_stall_still_counts(make_layer, clock, died):
    layer = make_layer(
        replies=scripts([call("no_step_applies"), Killed()], [call("no_step_applies")])
    )
    layer.start()
    died()
    clock.pass_time(minutes=5)

    run = layer.triage()

    assert run["notes"][0]["state"] == "unverified" and run["notes"][0]["action"] is None
    (asked,) = layer.stubs[1].prompts
    assert asked[-1].text == (
        "An earlier answer for this exception was refused: "
        "A step applies to this exception, so 'no step' is refused."
    )
    assert [(s["attempt"], s["turn"]) for s in choices(run)] == [(1, 1), (2, 2)]


def test_two_refusals_before_the_stall_leave_only_the_fallback(
    make_layer, clock, died, monkeypatch
):
    killed_once(monkeypatch, "record_fallback")
    layer = make_layer(
        replies=scripts(
            [call("no_step_applies"), call("no_step_applies")],
            [call("choose_step", option=OPTION)],
        )
    )
    layer.start()
    died()
    clock.pass_time(minutes=5)

    run = layer.triage()

    assert run["state"] == "completed"
    assert run["notes"][0]["state"] == "unverified" and run["notes"][0]["action"] is None
    assert layer.stubs[1].prompts == [], "a third choice was asked for"


def test_one_cap_holds_over_every_attempt(make_layer, clock):
    layer = make_layer(agent=lambda *_: idle_agent())
    run_id = layer.start()["id"]
    choose(layer, run_id, 1, NONE, usage=USAGE | {"cost_usd": "1.990000"})
    clock.pass_time(minutes=5)
    layer.start()
    stubbed = stub(call("choose_step", option=OPTION))
    configured = models.Configured("stub:triage", stubbed, Price(Decimal(4), Decimal(20)))

    connection = Connection(layer.url, AGENT_KEY)
    harness.triage(connection, configured, run_id, 2, Decimal(2))
    connection.close()

    run = layer.run(run_id)
    assert run["state"] == "failed" and "cost cap" in run["failure"]
    assert stubbed.prompts == []


# Asking the agent


def test_a_timeout_while_asking_stalls_and_starting_again_finishes_it(
    make_layer, clock, monkeypatch
):
    monkeypatch.setattr("backend.asking.ANSWER_SECONDS", 0.2)
    given = []

    def replies():
        given.append(True)
        if len(given) == 1:
            # The first ask is answered too late, by an agent that never got to work.
            threading.Event().wait(0.6)
            raise models.Unavailable("no_model")
        return [call("choose_step", option=OPTION)]

    layer = make_layer(replies=replies)
    timed_out = start(layer)

    assert timed_out.status_code == 202
    assert timed_out.json()["asked"] == "unknown"
    run_id = timed_out.json()["id"]
    assert layer.run(run_id)["state"] == "working"
    clock.pass_time(minutes=5)
    stalled = layer.run(run_id)
    assert stalled["state"] == "working" and stalled["stalled"]
    page = layer.request("GET", "/").text
    assert "and stalled: nothing has been heard" in page
    assert "working, stalled" in page
    assert "It is stalled" in main.describe(stalled)

    run = layer.triage()

    assert run["id"] == run_id
    assert (run["state"], run["attempt"]) == ("completed", 2)
    assert len(run["notes"]) == 7


def test_an_agent_that_is_down_undoes_starting_again_but_not_the_attempts_number(make_layer, clock):
    first = make_layer(agent=lambda *_: idle_agent())
    run_id = first.start()["id"]
    nowhere = listen()
    url = address(nowhere)
    nowhere.close()
    second = make_layer(after=first, agent_url=url)
    clock.pass_time(minutes=5)
    before = second.run(run_id)

    response = start(second)

    assert response.status_code == 503
    assert response.json() == {
        "detail": "The triage agent cannot be reached. Starting it again was undone: "
        "the run is working and still stalled, so it can be started again."
    }
    after = second.run(run_id)
    assert after["stalled"]
    # The number is spent, so that no move of an attempt before it can come back to life.
    assert after == before | {"attempt": 2}
    third = make_layer(after=second, replies=lambda: [call("choose_step", option=OPTION)])
    finished = third.triage()
    assert (finished["id"], finished["state"], finished["attempt"]) == (run_id, "completed", 3)


def test_an_agent_still_working_the_run_refuses_to_be_asked_again(make_layer, clock):
    asked, release = threading.Event(), threading.Event()

    def slow():
        asked.set()
        assert release.wait(10)
        return call("choose_step", option=OPTION)

    layer = make_layer(replies=lambda: [slow])
    run_id = layer.start()["id"]
    assert asked.wait(10)
    clock.pass_time(minutes=5)

    response = start(layer)
    release.set()

    assert response.status_code == 503
    assert "already working this run" in response.json()["detail"]
    # The attempt before is retired all the same: what it answers late is refused.
    run = restarted_when_free(layer, run_id)
    assert (run["state"], run["attempt"] > 2) == ("completed", True)
    accepted = [step for step in choices(run) if step["outcome"] == "accepted"]
    assert [step["attempt"] for step in accepted] == [run["attempt"]]


def restarted_when_free(layer, run_id: str) -> dict:
    """Start the run again once the agent has let go of it."""
    for _ in range(200):
        again = start(layer)
        if again.status_code == 202:
            return layer.ended(run_id)
        assert again.status_code == 503, again.text
        threading.Event().wait(0.05)
    raise AssertionError("the agent kept the run")


def test_an_old_attempt_cannot_end_a_run_whose_restart_was_undone(make_layer, clock):
    """Its choice lands while the restart is being asked, and its failure after the undo."""
    seen = []

    def refusing_once(backend: str, _: str) -> FastAPI:
        app = FastAPI()

        def invoked(asked: dict) -> dict:
            if asked["attempt"] != 2:
                return {"accepted": True}
            seen.append(choose(layer, asked["run_id"], 1, RIGHT))
            return {"refused": "already_working"}

        app.add_api_route("/invocations", invoked, methods=["POST"])
        return app

    layer = make_layer(agent=refusing_once)
    run_id = layer.start()["id"]
    clock.pass_time(minutes=5)
    assert start(layer).status_code == 503
    assert [each.status_code for each in seen] == [409]

    late = layer.as_agent("POST", path(run_id, 1, "/fail"), json={"reason": "backend_refused"})

    assert late.status_code == 409
    run = layer.run(run_id)
    assert run["state"] == "working" and run["stalled"]
    assert start(layer).json()["attempt"] == 3


def test_failed_restarts_do_not_use_the_attempts_up(make_layer, clock):
    """Each failed restart spends a number, so there must be far more than a person would use."""
    idle = make_layer(agent=lambda *_: idle_agent())
    run_id = idle.start()["id"]
    engine = create_engine(f"sqlite:///{idle.settings.database}")
    with engine.begin() as connection:
        connection.execute(text("UPDATE triage_runs SET attempt = 5000"))
    engine.dispose()
    clock.pass_time(minutes=5)

    again = start(idle).json()

    assert again["attempt"] == 5001
    assert choose(idle, run_id, 5001, RIGHT).status_code == 200
    # The agent's app takes the number too.
    clock.pass_time(minutes=5)
    working = make_layer(after=idle, replies=list)
    finished = working.triage()
    assert (finished["state"], finished["attempt"]) == ("completed", 5002)


def test_an_agent_that_did_start_is_not_undone(make_layer, clock):
    """Its answer was an error, but it had been heard from."""
    heard = []

    def works_then_errs(backend: str, _: str) -> FastAPI:
        app = FastAPI()

        def invoked(asked: dict):
            if asked["attempt"] == 1:
                return {"accepted": True}
            # A tick later than the move that started it again.
            clock.pass_time(seconds=1)
            heard.append(choose(layer, asked["run_id"], 2, RIGHT))
            return JSONResponse({"no": 1}, status_code=500)

        app.add_api_route("/invocations", invoked, methods=["POST"])
        return app

    layer = make_layer(agent=works_then_errs)
    layer.start()
    clock.pass_time(minutes=5)

    again = start(layer)

    assert [each.status_code for each in heard] == [200]
    assert again.status_code == 202
    assert layer.run(again.json()["id"])["attempt"] == 2


# The move and its refusals


@pytest.fixture
def idle(make_layer):
    return make_layer(agent=lambda *_: idle_agent())


def test_it_stalls_at_five_minutes_and_not_before(idle, clock):
    clock.stopped_at = datetime(2026, 11, 22, 9, 0, tzinfo=UTC)
    run_id = idle.start()["id"]
    clock.pass_time(minutes=4, seconds=59, microseconds=999_999)

    early = start(idle)

    assert early.status_code == 409
    assert early.json() == {"detail": NOT_STALLED}
    assert not idle.run(run_id)["stalled"]
    clock.pass_time(microseconds=1)
    assert idle.run(run_id)["stalled"]
    assert start(idle).json()["attempt"] == 2


def test_a_step_heard_puts_the_stall_off(idle, clock):
    run_id = idle.start()["id"]
    clock.pass_time(minutes=4)
    assert choose(idle, run_id, 1, NONE).status_code == 422
    clock.pass_time(minutes=4)

    assert start(idle).status_code == 409
    assert not idle.run(run_id)["stalled"]


def test_starting_again_reads_wealthcontext_no_more(idle, clock):
    run_id = idle.start()["id"]
    receipt = idle.run(run_id)["notes"][0]["receipt"]
    asked = list(idle.wealthcontext.asked)
    idle.wealthcontext.down = True
    clock.pass_time(minutes=5)

    assert start(idle).status_code == 202
    assert idle.wealthcontext.asked == asked
    assert idle.run(run_id)["notes"][0]["receipt"] == receipt


@pytest.mark.parametrize("change", ["retired", "replaced"])
def test_a_run_started_again_after_the_playbooks_changed_gives_out_no_older_action(
    make_layer, clock, died, change
):
    replaced = [call("choose_step", option=OPTION)] if change == "replaced" else []
    layer = make_layer(replies=scripts([Killed()], [], replaced))
    began = layer.start()
    died()
    before = layer.run(began["id"])
    if change == "retired":
        assert layer.retire("feed-outage", 1).status_code == 200
    else:
        layer.put_in_use({"feed-outage": firm.steps("feed-outage", by="within 2 hours")})
    in_use = layer.as_analyst("GET", "/api/playbooks").json()
    clock.pass_time(minutes=5)

    again = layer.ended(layer.start()["id"])

    assert (again["id"], again["attempt"], again["state"]) == (began["id"], 2, "failed")
    assert again["notes"] == before["notes"], "a note was settled under the older playbooks"
    assert layer.stubs[1].prompts == [], "a model was asked to choose under the older playbooks"

    new = layer.triage()

    assert new["id"] != began["id"] and new["state"] == "completed"
    assert new["playbook_set"] == {key: in_use[key] for key in ("id", "version", "hash")}
    feed = new["notes"][0]
    if change == "retired":
        assert (feed["state"], feed["action"]) == ("no_step", None)
    else:
        assert (feed["step"]["version"], feed["action"]["by"]) == (2, "within 2 hours")


@pytest.mark.parametrize("ends", ["complete", "fail"])
def test_a_run_that_ended_is_not_started_again(idle, clock, ends):
    run_id = idle.start()["id"]
    if ends == "complete":
        choose(idle, run_id, 1, RIGHT)
    body = {"reason": "model_failed"} if ends == "fail" else None
    assert idle.as_agent("POST", path(run_id, 1, f"/{ends}"), json=body).status_code == 200
    clock.pass_time(minutes=5)

    new = start(idle)

    assert new.status_code == 202
    assert new.json()["id"] != run_id
    assert new.json()["attempt"] == 1
    assert not idle.run(run_id)["stalled"]


def test_only_the_analyst_starts_a_run_again(idle, clock):
    run_id = idle.start()["id"]
    clock.pass_time(minutes=5)

    assert idle.as_owner("POST", "/api/triage-runs", json=ASKED).status_code == 403
    assert idle.as_agent("POST", "/api/triage-runs", json=ASKED).status_code == 401
    assert idle.run(run_id)["attempt"] == 1


@pytest.mark.parametrize("stalled", [False, True])
def test_two_starts_at_once_make_one_move(idle, clock, stalled):
    if stalled:
        idle.start()
        clock.pass_time(minutes=5)
    together, found = threading.Barrier(2), []

    def starting() -> None:
        together.wait(5)
        found.append(start(idle))

    threads = [threading.Thread(target=starting) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)

    assert sorted(each.status_code for each in found) == [202, 409]
    ((_, attempt, *_),) = rows(idle, "triage_runs")
    assert attempt == (2 if stalled else 1)
    assert len(rows(idle, "triage_notes")) == 7


# An attempt that was superseded


@pytest.fixture
def superseded(idle, clock):
    run_id = idle.start()["id"]
    clock.pass_time(minutes=5)
    assert idle.start()["attempt"] == 2
    return run_id


@pytest.mark.parametrize(
    "rest, body",
    [
        ("", None),
        ("/notes/{note}/choice", {"turn": 1, "choice": RIGHT, "usage": USAGE}),
        (
            "/notes/{note}/unusable-reply",
            {"turn": 1, "reason": "no_tool_call", "calls": [], "usage": USAGE},
        ),
        ("/notes/{note}/fallback", None),
        ("/complete", None),
        ("/fail", {"reason": "model_failed"}),
    ],
)
def test_every_move_of_an_old_attempt_is_409_naming_the_state(idle, superseded, rest, body):
    before = idle.run(superseded)
    where = path(superseded, 1, rest.format(note=before["notes"][0]["id"]))

    response = idle.as_agent("POST" if rest else "GET", where, json=body)

    assert response.status_code == 409
    assert response.json() == {"detail": "That attempt is not the current one; the run is working."}
    after = idle.run(superseded)
    assert (after["state"], after["notes"]) == ("working", before["notes"])


def test_an_old_attempts_tool_call_is_a_refused_step_and_counts_for_nothing(
    idle, superseded, clock
):
    assert choose(idle, superseded, 1, RIGHT).status_code == 409
    assert choose(idle, superseded, 1, RIGHT).status_code == 409, "the same move again"
    assert choose(idle, superseded, 1, NONE, turn=2).status_code == 409

    run = idle.run(superseded)
    assert [(s["attempt"], s["turn"], s["tool"], s["outcome"]) for s in run["steps"]] == [
        (1, 1, "choose_step", "refused"),
        (1, 2, "no_step_applies", "refused"),
    ]
    assert run["notes"][0]["state"] == "pending"
    # Not one of the note's two refusals: the new attempt still has both its turns.
    (pending,) = idle.as_agent("GET", path(superseded, 2)).json()["notes"]
    assert pending["refusals"] == []
    assert choose(idle, superseded, 2, NONE).status_code == 422
    assert choose(idle, superseded, 2, RIGHT, turn=2).json() == {"state": "chosen"}


def test_an_old_attempt_does_not_keep_a_run_from_stalling(idle, superseded, clock):
    clock.pass_time(minutes=4)
    assert choose(idle, superseded, 1, RIGHT).status_code == 409
    clock.pass_time(minutes=1)

    assert idle.run(superseded)["stalled"]


def test_a_move_accepted_before_the_stall_is_refused_and_not_recorded_twice(idle, clock):
    run_id = idle.start()["id"]
    assert choose(idle, run_id, 1, RIGHT).status_code == 200
    clock.pass_time(minutes=5)
    idle.start()

    assert choose(idle, run_id, 1, RIGHT).status_code == 409
    assert len(idle.run(run_id)["steps"]) == 1


def test_a_call_out_of_its_turn_is_a_step_and_the_turn_is_still_free(idle):
    run_id = idle.start()["id"]
    assert choose(idle, run_id, 1, RIGHT, turn=2).status_code == 409
    assert choose(idle, run_id, 1, NONE).status_code == 422
    assert choose(idle, run_id, 1, RIGHT, turn=2).status_code == 409, "the same move again"
    other = RIGHT | {"step": "another-step"}
    assert choose(idle, run_id, 1, other, turn=2).status_code == 422

    steps = idle.run(run_id)["steps"]

    assert [(s["turn"], s["outcome"]) for s in steps] == [
        (2, "refused"),
        (1, "refused"),
        (2, "refused"),
    ]


@pytest.mark.parametrize(
    "answer, recorded",
    [
        (lambda: call("no_step_applies"), "no_step_applies"),
        (lambda: call("release_report"), "other"),
    ],
    ids=["a choice", "an unusable call"],
)
def test_a_superseded_agents_model_reads_the_refusal_and_settles_nothing(
    idle, clock, answer, recorded
):
    run_id = idle.start()["id"]

    def superseded_meanwhile():
        clock.pass_time(minutes=5)
        assert idle.start()["attempt"] == 2
        return answer()

    right = call("choose_step", option=OPTION)
    old = stub(superseded_meanwhile, right, call("choose_step", option=OPTION))
    connection = Connection(idle.url, AGENT_KEY)
    harness.triage(connection, models.Configured("stub:triage", old, None), run_id, 1, Decimal(2))
    connection.close()

    _, second = old.prompts  # the refusal back once, and no third call
    assert second[-1].type == "tool" and second[-1].status == "error"
    assert second[-1].text == "That attempt is not the current one; the run is working."
    run = idle.run(run_id)
    assert (run["state"], run["attempt"]) == ("working", 2)
    assert run["notes"][0]["state"] == "pending"
    assert [(s["attempt"], s["turn"], s["tool"], s["outcome"]) for s in run["steps"]] == [
        (1, 1, recorded, "refused"),
        (1, 2, "choose_step", "refused"),
    ]
    (pending,) = idle.as_agent("GET", path(run_id, 2)).json()["notes"]
    assert pending["refusals"] == [], "a refusal of attempt counted as a refused choice"


# The command


@pytest.fixture
def command(monkeypatch, tmp_path):
    def run(layer, token: str = ANALYST, *more: str) -> int:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PLAYBOOKS_URL", layer.url)
        monkeypatch.setenv("PLAYBOOKS_ANALYST_TOKEN", token)
        return main.main(["triage", "--date", DAY, "--cutoff", CUTOFF, *more])

    return run


def test_the_same_command_starts_a_stalled_run_again(make_layer, clock, died, command, capsys):
    layer = make_layer(replies=scripts([Killed()], [call("choose_step", option=OPTION)]))
    layer.start()
    died()

    assert command(layer) == 1
    assert capsys.readouterr().err == f"409: {NOT_STALLED}\n"
    clock.pass_time(minutes=5)
    assert command(layer, OWNER) == 1
    assert capsys.readouterr().err == "403: Only the analyst may do this.\n"

    assert command(layer) == 0

    said = capsys.readouterr()
    assert "Started the stalled run again: attempt 2." in said.err
    assert ": completed" in said.out
    assert said.out.count("No approved step applies.") == 6


def test_the_command_says_when_the_agent_did_not_answer(make_layer, monkeypatch, command, capsys):
    monkeypatch.setattr("backend.asking.ANSWER_SECONDS", 0.2)

    def replies():
        threading.Event().wait(0.6)
        raise models.Unavailable("no_model")

    layer = make_layer(replies=replies)

    assert command(layer, ANALYST, "--wait", "0") == 1

    said = capsys.readouterr()
    assert "did not say whether it started" in said.err
    assert "the same command starts the run again" in said.err
