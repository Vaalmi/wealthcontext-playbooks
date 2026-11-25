"""Each move of the triage run and its notes, and each refusal."""

import pytest
from layer import CUTOFF, DAY
from sqlalchemy import create_engine, exc, text
from stand_ins import idle_agent

USAGE = {"model": "stub:triage", "input_tokens": 1, "output_tokens": 1, "cost_usd": None}
RIGHT = {"kind": "step", "playbook": "feed-outage", "step": "portfolio-feed-failed"}
NONE = {"kind": "no_step"}


@pytest.fixture
def layer(make_layer):
    return make_layer(agent=lambda *_: idle_agent())


class Working:
    def __init__(self, layer, cutoff: str = CUTOFF):
        self.layer = layer
        self.run_id = layer.start(cutoff=cutoff)["id"]
        self.base = f"/api/agent/triage-runs/{self.run_id}/attempts/1"
        (self.note,) = layer.as_agent("GET", self.base).json()["notes"]

    def send(self, path: str, body: dict | None = None):
        return self.layer.as_agent("POST", f"{self.base}{path}", json=body)

    def choose(self, choice: dict, turn: int = 1, note: str | None = None):
        body = {"turn": turn, "choice": choice, "usage": USAGE}
        return self.send(f"/notes/{note or self.note['id']}/choice", body)

    def unusable(self, turn: int = 1):
        body = {"turn": turn, "reason": "no_tool_call", "calls": [], "usage": USAGE}
        return self.send(f"/notes/{self.note['id']}/unusable-reply", body)

    def fallback(self):
        return self.send(f"/notes/{self.note['id']}/fallback")

    def note_now(self) -> dict:
        return self.layer.run(self.run_id)["notes"][0]


@pytest.fixture
def working(layer):
    return Working(layer)


def test_the_agent_is_shown_the_pending_note_and_no_identifier(working):
    assert working.note == {
        "id": working.note["id"],
        "code": "SOURCE_UNAVAILABLE",
        "feed": {"source_family": "PAS", "custodian": "B", "feed_status": "FAILED"},
        "warning_count": 15,
        "options": [
            {
                "playbook": "feed-outage",
                "step": "portfolio-feed-failed",
                "when": {"source_family": "PAS", "custodian": "any", "feed_status": "FAILED"},
                "line": "Ask for the failed portfolio accounting fetch to be run again.",
            }
        ],
        "refusals": [],
    }


def test_a_choice_that_holds_settles_the_note(working):
    assert working.choose(RIGHT).json() == {"state": "chosen"}
    assert working.note_now()["state"] == "chosen"


def test_no_step_for_the_failed_feed_is_422_and_one_line(working):
    response = working.choose(NONE)

    assert response.status_code == 422
    assert response.json() == {
        "detail": "A step applies to this exception, so 'no step' is refused."
    }
    assert working.note_now()["state"] == "pending"


@pytest.mark.parametrize(
    "choice",
    [
        {"kind": "step", "playbook": "feed-outage", "step": "not-a-step"},
        {"kind": "step", "playbook": "missing-cross-check", "step": "custody-cross-check-missing"},
    ],
)
def test_a_step_not_in_use_for_the_code_is_422_and_one_line(working, choice):
    response = working.choose(choice)

    assert response.status_code == 422
    assert response.json() == {
        "detail": "That is not a step in use with this exception's warning code."
    }


def test_no_step_holds_when_no_step_fits(layer):
    def partial(operation, envelope):
        if operation == "feed_runs":
            for run in envelope["data"]["items"]:
                if run["status"] != "SUCCESS":
                    run["status"] = "PARTIAL"
        if operation == "feed_run_impact":
            envelope["data"]["status"] = "PARTIAL"
        return envelope

    layer.wealthcontext.edit = partial
    working = Working(layer)

    misfit = working.choose(RIGHT)
    assert misfit.status_code == 422
    assert misfit.json() == {
        "detail": "That step does not fit this exception: "
        "its feed_status is FAILED, and the exception's is PARTIAL."
    }
    assert working.choose(NONE, turn=2).json() == {"state": "no_step"}
    assert working.note_now()["statement"] == "No approved step applies."


def test_the_same_move_again_returns_what_it_did_and_changes_nothing(working):
    assert working.choose(RIGHT).json() == working.choose(RIGHT).json()
    assert len(working.layer.run(working.run_id)["steps"]) == 1


def test_the_same_refused_move_again_returns_the_same_refusal(working):
    first, again = working.choose(NONE), working.choose(NONE)

    assert (first.status_code, first.json()) == (again.status_code, again.json())
    assert len(working.layer.run(working.run_id)["steps"]) == 1


def test_the_same_unusable_reply_again_changes_nothing(working):
    assert working.unusable().json() == working.unusable().json()
    assert len(working.layer.run(working.run_id)["steps"]) == 1


def test_another_choice_on_a_settled_note_is_409_naming_the_state(working):
    working.choose(RIGHT)

    for response in (working.choose(NONE), working.choose(NONE, turn=2)):
        assert response.status_code == 409
        assert "chosen" in response.json()["detail"]


def test_a_note_the_backend_settled_takes_no_choice(working):
    settled = working.layer.run(working.run_id)["notes"][1]["id"]

    response = working.choose(NONE, note=settled)

    assert response.status_code == 409
    assert "no_step" in response.json()["detail"]


def test_a_turn_out_of_order_is_409(working):
    response = working.choose(RIGHT, turn=2)

    assert response.status_code == 409
    assert "pending" in response.json()["detail"]


def test_fallback_follows_a_second_refusal_only(working):
    assert working.fallback().status_code == 409
    working.choose(NONE)
    assert working.fallback().status_code == 409
    working.unusable(turn=2)

    assert working.fallback().json() == {"state": "unverified"}
    assert working.fallback().json() == {"state": "unverified"}
    assert working.note_now()["action"] is None


def test_after_two_refusals_there_is_no_third_choice(working):
    working.choose(NONE)
    working.choose(NONE, turn=2)

    assert working.choose(RIGHT, turn=2).status_code == 409
    assert working.note_now()["state"] == "pending"


def test_fallback_on_a_chosen_note_is_409(working):
    working.choose(RIGHT)

    response = working.fallback()

    assert response.status_code == 409
    assert "chosen" in response.json()["detail"]


def test_complete_needs_every_note_settled(working):
    refused = working.send("/complete")
    assert refused.status_code == 409
    assert "pending" in refused.json()["detail"]

    working.choose(RIGHT)

    assert working.send("/complete").json() == {"state": "completed"}
    assert working.send("/complete").json() == {"state": "completed"}


def test_fail_works_from_working_only(working):
    sent = {"reason": "model_failed"}
    assert working.send("/fail", sent).json() == {"state": "failed"}
    assert working.send("/fail", sent).json() == {"state": "failed"}

    for response in (
        working.send("/fail", {"reason": "cost_cap"}),
        working.send("/complete"),
        working.choose(RIGHT),
        working.unusable(),
        working.fallback(),
    ):
        assert response.status_code == 409
        assert "failed" in response.json()["detail"]


def test_a_completed_run_cannot_fail(working):
    working.choose(RIGHT)
    working.send("/complete")

    response = working.send("/fail", {"reason": "model_failed"})

    assert response.status_code == 409
    assert "completed" in response.json()["detail"]


def test_a_move_for_another_attempt_is_409_naming_the_state(working):
    body = {"turn": 1, "choice": RIGHT, "usage": USAGE}
    path = f"/api/agent/triage-runs/{working.run_id}/attempts/2/notes/{working.note['id']}/choice"

    response = working.layer.as_agent("POST", path, json=body)

    assert response.status_code == 409
    assert "working" in response.json()["detail"]


def test_a_note_of_another_run_is_404(layer):
    first, second = Working(layer), Working(layer, cutoff="2026-10-30T16:00:00Z")

    assert first.choose(RIGHT, note=second.note["id"]).status_code == 404
    assert second.note_now()["state"] == "pending"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE triage_notes SET state = 'unverified' WHERE state = 'chosen'",
        "UPDATE triage_runs SET state = 'working'",
        "UPDATE triage_runs SET set_hash = 'x'",
        "UPDATE triage_exceptions SET code = 'PRICE_STALE'",
        "UPDATE triage_reads SET trace_id = 'x'",
        "UPDATE triage_steps SET line = 'x'",
        "UPDATE triage_notices SET subject = 'x'",
        "DELETE FROM triage_runs",
        "DELETE FROM triage_notes",
        "DELETE FROM triage_exceptions",
        "DELETE FROM triage_reads",
        "DELETE FROM triage_steps",
        "DELETE FROM triage_notices",
    ],
)
def test_the_records_refuse_rewrites_and_deletes(working, statement):
    working.choose(RIGHT)
    working.send("/complete")
    engine = create_engine(f"sqlite:///{working.layer.settings.database}")

    with pytest.raises(exc.IntegrityError), engine.begin() as connection:
        connection.execute(text(statement))
    engine.dispose()


def test_the_cutoff_is_kept_as_the_instant_it_names(layer):
    run_id = layer.start(DAY, "2026-10-31T12:00:00-04:00")["id"]

    assert layer.run(run_id)["cutoff"] == CUTOFF
