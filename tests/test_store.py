"""The playbook store: versions never edited, none overlapping, retired by the owner alone."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import firm
import pytest
import yaml
from layer import ANALYST, CROSS_CHECK_DAY, CROSS_CHECK_OPTION, DAY, OPTION, OWNER
from stand_ins import idle_agent
from stub_model import call

from backend import matching
from backend.chainlog import LogInvalid
from backend.playbook_format import Step
from backend.store import Overlap, StoreInvalid
from cli import main

PAS_FAILED = {"source_family": "PAS", "custodian": "any", "feed_status": "FAILED"}
ONLY_B = PAS_FAILED | {"custodian": "B"}
ONLY_A = PAS_FAILED | {"custodian": "A"}
REASONS = ("source_withdrawn", "wrong_action", "no_longer_applies", "put_in_use_in_error")


def a_step(name: str, when: dict, code: str = "SOURCE_UNAVAILABLE") -> Step:
    (founded,) = firm.steps("feed-outage")
    return Step.model_validate(founded.model_dump() | {"id": name, "when": when, "code": code})


# Overlap


@pytest.mark.parametrize(
    "first, second, overlaps",
    [
        (PAS_FAILED, ONLY_B, True),
        (ONLY_A, ONLY_B, False),
        (ONLY_B, ONLY_B, True),
        (firm.ANY_WHEN, ONLY_B, True),
        (firm.ANY_WHEN, firm.ANY_WHEN, True),
        ({**firm.ANY_WHEN, "custodian": "B"}, {**firm.ANY_WHEN, "feed_status": "FAILED"}, True),
        ({**firm.ANY_WHEN, "source_family": "PAS"}, {**ONLY_B, "source_family": "CUSTODY"}, False),
        (PAS_FAILED, {**PAS_FAILED, "feed_status": "PARTIAL"}, False),
    ],
)
def test_two_steps_overlap_when_some_exception_could_fit_both(first, second, overlaps):
    one, two = a_step("one", first), a_step("two", second)

    assert matching.overlap(one, two) is overlaps
    assert matching.overlap(two, one) is overlaps


def test_steps_for_different_codes_never_overlap():
    one = a_step("one", firm.ANY_WHEN)
    two = a_step("two", firm.ANY_WHEN, code="PRICE_STALE")

    assert not matching.overlap(one, two)


def founded_with(tmp_path, where: str, *whens: dict):
    """A folder whose founding set holds a feed step for each of these conditions."""
    folder = firm.copy(tmp_path / "playbooks")
    file = folder / "feed-outage" / "1.yaml"
    original = yaml.safe_load(file.read_text())
    made = [
        copy.deepcopy(original["steps"][0]) | {"id": f"step-{chr(97 + number)}", "when": when}
        for number, when in enumerate(whens)
    ]
    if where == "one playbook":
        file.write_text(yaml.safe_dump(original | {"steps": made}))
    else:
        file.write_text(yaml.safe_dump(original | {"steps": made[:1]}))
        (folder / "another-feed").mkdir()
        other = original | {"id": "another-feed", "steps": made[1:]}
        (folder / "another-feed" / "1.yaml").write_text(yaml.safe_dump(other))
    return firm.found(folder)


@pytest.mark.parametrize("where", ["one playbook", "two playbooks"])
def test_the_backend_will_not_start_on_a_set_that_overlaps(tmp_path, where):
    folder = founded_with(tmp_path, where, PAS_FAILED, ONLY_B)

    with pytest.raises(LogInvalid, match="ledger.jsonl line 1 cannot be applied: .* overlap"):
        with firm.opened(folder, tmp_path):
            pass


@pytest.mark.parametrize("where", ["one playbook", "two playbooks"])
def test_a_set_with_one_step_for_each_custodian_is_accepted(tmp_path, where):
    folder = founded_with(tmp_path, where, ONLY_A, ONLY_B)

    with firm.opened(folder, tmp_path) as (journal, store), journal.transaction() as connection:
        assert len(store.current(connection).with_code("SOURCE_UNAVAILABLE")) == 2


@pytest.fixture
def stored(tmp_path):
    folder = firm.copy(tmp_path / "playbooks")
    with firm.opened(folder, tmp_path) as (journal, store):
        yield folder, journal, store


@pytest.mark.parametrize("playbook", ["feed-outage", "custodian-b-feed"])
def test_a_change_that_would_overlap_is_refused_and_writes_nothing(stored, playbook):
    folder, journal, store = stored
    added = [a_step("only-b", ONLY_B)]
    if playbook == "feed-outage":
        added += firm.steps("feed-outage")
    before = firm.files(folder)

    with pytest.raises(Overlap):
        firm.put_in_use(journal, store, {playbook: added})

    assert firm.files(folder) == before
    assert firm.in_use(journal, store) == [("feed-outage", 1), ("missing-cross-check", 1)]


def test_a_step_for_custodian_a_beside_one_for_custodian_b_is_accepted(stored):
    _, journal, store = stored

    firm.put_in_use(journal, store, {"feed-outage": [a_step("only-b", ONLY_B)]})
    firm.put_in_use(journal, store, {"custodian-a-feed": [a_step("only-a", ONLY_A)]})

    assert firm.in_use(journal, store) == [
        ("custodian-a-feed", 1),
        ("feed-outage", 2),
        ("missing-cross-check", 1),
    ]


# Versions are written once


def test_a_version_after_a_retirement_takes_a_number_none_has_had(make_layer):
    layer = make_layer(agent=lambda *_: idle_agent())
    folder = layer.settings.playbooks
    seen = {}

    def unchanged() -> None:
        now = {name: data for name, data in firm.files(folder).items() if name.endswith(".yaml")}
        assert seen.items() <= now.items(), "a version file was written over"
        seen.update(now)

    unchanged()
    for hours, retire, expected in [(2, 2, 1), (3, 3, 1), (4, 4, 1), (5, None, 5)]:
        layer.put_in_use({"feed-outage": firm.steps("feed-outage", by=f"within {hours} hours")})
        unchanged()
        if retire:
            assert layer.retire("feed-outage", retire).json()["now_in_use"] == expected
            unchanged()

    written = yaml.safe_load((folder / "feed-outage" / "5.yaml").read_text())
    assert (written["id"], written["version"], written["replaces"]) == ("feed-outage", 5, 1)
    assert written["steps"][0]["by"] == "within 5 hours"
    assert sorted(file.name for file in (folder / "feed-outage").iterdir()) == [
        f"{number}.yaml" for number in range(1, 6)
    ]


def test_a_playbook_with_none_in_use_still_takes_a_new_number(make_layer):
    layer = make_layer(agent=lambda *_: idle_agent())
    folder = layer.settings.playbooks
    first = (folder / "feed-outage" / "1.yaml").read_bytes()
    assert layer.retire("feed-outage", 1).status_code == 200

    layer.put_in_use({"feed-outage": firm.steps("feed-outage")})

    written = yaml.safe_load((folder / "feed-outage" / "2.yaml").read_text())
    assert (written["version"], written["replaces"]) == (2, None)
    assert (folder / "feed-outage" / "1.yaml").read_bytes() == first


def test_a_file_that_is_there_is_never_written_over(stored, monkeypatch):
    folder, journal, store = stored
    first = (folder / "feed-outage" / "1.yaml").read_bytes()
    with journal.transaction() as connection:
        store.retire(connection, "feed-outage", 1, "wrong_action", who="playbook_owner", at=firm.AT)
    # Even were the number wrong, the file is not opened over another.
    monkeypatch.setattr(store, "_highest", lambda *_: 0)

    with pytest.raises(FileExistsError):
        firm.put_in_use(
            journal, store, {"feed-outage": firm.steps("feed-outage", hold_report=False)}
        )

    assert (folder / "feed-outage" / "1.yaml").read_bytes() == first


def test_a_version_left_by_a_move_that_failed_gives_way_to_the_next(stored, monkeypatch):
    folder, journal, store = stored

    def full(_):
        raise OSError("the disk is full")

    with monkeypatch.context() as patched:
        patched.setattr(store.kept.log, "write", full)
        with pytest.raises(OSError):
            firm.put_in_use(journal, store, {"feed-outage": firm.steps("feed-outage")})
    assert (folder / "feed-outage" / "2.yaml").exists()

    changed = firm.steps("feed-outage", by="within 2 hours")
    firm.put_in_use(journal, store, {"feed-outage": changed})

    assert "within 2 hours" in (folder / "feed-outage" / "2.yaml").read_text()


def test_tidying_removes_only_version_files_no_line_names(tmp_path):
    folder = firm.copy(tmp_path / "playbooks")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "3.yaml").write_text("not the store's")
    (folder / "linked").symlink_to(outside)
    (folder / "feed-outage" / "notes.txt").write_text("a person's")
    (folder / "feed-outage" / "2.yaml").write_text("half of a vers")

    with firm.opened(folder, tmp_path):
        pass

    assert sorted(file.name for file in (folder / "feed-outage").iterdir()) == [
        "1.yaml",
        "notes.txt",
    ]
    assert (outside / "3.yaml").exists()


def test_a_desk_that_is_not_on_the_list_is_refused_before_a_file_is_written(stored):
    folder, journal, store = stored
    before = firm.files(folder)

    with pytest.raises(StoreInvalid, match="tells a desk"):
        changed = firm.steps("feed-outage", tell="compliance")
        firm.put_in_use(journal, store, {"feed-outage": changed})

    assert firm.files(folder) == before


# Retirement


@pytest.fixture
def layer(make_layer):
    return make_layer(agent=lambda *_: idle_agent())


def versions_in_use(layer) -> list[tuple[str, int]]:
    found = layer.as_analyst("GET", "/api/playbooks").json()["in_use"]
    return [(each["id"], each["version"]) for each in found]


def ledger(layer) -> list[dict]:
    lines = (layer.settings.playbooks / "ledger.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_the_owner_retires_the_version_in_use_and_the_one_before_is_back(layer):
    layer.put_in_use({"feed-outage": firm.steps("feed-outage", by="within 2 hours")})
    assert versions_in_use(layer) == [("feed-outage", 2), ("missing-cross-check", 1)]
    before = ledger(layer)

    response = layer.retire("feed-outage", 2, "source_withdrawn")

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "retired"
    assert response.json()["now_in_use"] == 1
    assert response.json()["playbook_set"]["version"] == firm.APPROVED_SET + 2
    assert versions_in_use(layer) == [("feed-outage", 1), ("missing-cross-check", 1)]
    after = ledger(layer)
    assert after[:-1] == before
    added = after[-1]
    assert (added["event"], added["who"]) == ("version_retired", "playbook_owner")
    assert added["data"] == {
        "playbook": "feed-outage",
        "version": 2,
        "reason": "source_withdrawn",
        "restored": 1,
    }


def test_the_analyst_and_the_agent_retire_nothing(layer):
    path = "/api/playbooks/feed-outage/versions/1/retire"
    before = ledger(layer)

    refused = layer.as_analyst("POST", path, json={"reason": "wrong_action"})

    assert refused.status_code == 403
    assert refused.json() == {"detail": "Only the playbook owner may do this."}
    assert layer.as_agent("POST", path, json={"reason": "wrong_action"}).status_code == 401
    assert ledger(layer) == before
    assert versions_in_use(layer) == [("feed-outage", 1), ("missing-cross-check", 1)]


def test_the_same_retirement_again_returns_what_it_did_and_writes_nothing(layer):
    layer.put_in_use({"feed-outage": firm.steps("feed-outage")})
    first = layer.retire("feed-outage", 2, "wrong_action").json()
    assert layer.retire("missing-cross-check", 1).status_code == 200  # the set moves on
    before = ledger(layer)

    again = layer.retire("feed-outage", 2, "wrong_action")

    assert (again.status_code, again.json()) == (200, first)
    assert ledger(layer) == before


@pytest.mark.parametrize("state", ["retired", "superseded"])
def test_a_version_that_is_not_in_use_is_409_naming_its_state(layer, state):
    layer.put_in_use({"feed-outage": firm.steps("feed-outage")})
    version = 1
    if state == "retired":
        version = 2
        assert layer.retire("feed-outage", 2, "wrong_action").status_code == 200
    before = ledger(layer)

    response = layer.retire("feed-outage", version, "put_in_use_in_error")

    assert (response.status_code, response.json()) == (409, {"detail": f"The version is {state}."})
    assert ledger(layer) == before


@pytest.mark.parametrize("playbook, version", [("feed-outage", 2), ("no-such-playbook", 1)])
def test_a_version_that_is_not_there_is_404(layer, playbook, version):
    assert layer.retire(playbook, version).status_code == 404


def test_a_retirement_that_would_bring_back_an_overlap_is_409(layer):
    layer.put_in_use({"feed-outage": [a_step("only-a", ONLY_A)]})
    layer.put_in_use({"custodian-b-feed": [a_step("only-b", ONLY_B)]})
    before, in_use = ledger(layer), versions_in_use(layer)

    response = layer.retire("feed-outage", 2)

    assert response.status_code == 409
    assert "in_use" in response.json()["detail"] and "overlap" in response.json()["detail"]
    assert ledger(layer) == before
    assert versions_in_use(layer) == in_use


@pytest.mark.parametrize(
    "reason",
    [
        "Jordan Example Family Trust: client needs money for medical treatment.",
        "It was wrong.",
        "It broke WC-AC-0002.",
        "wrong_action for WC-AC-0002",
        "wrong_action\n",
        "Wrong_Action",
        "",
        None,
        7,
    ],
)
def test_a_reason_that_is_not_on_the_list_is_refused_and_kept_nowhere(layer, reason):
    before = firm.files(layer.settings.playbooks)

    response = layer.retire("feed-outage", 1, reason)

    assert response.status_code == 422
    assert response.json() == {
        "detail": "The request does not fit retire: a field is missing or malformed."
    }
    assert firm.files(layer.settings.playbooks) == before
    assert len(layer.as_analyst("GET", "/api/record").json()["entries"]) == len(
        firm.FOUNDING_EVENTS
    )
    assert versions_in_use(layer) == [("feed-outage", 1), ("missing-cross-check", 1)]


@pytest.mark.parametrize("reason", REASONS)
def test_each_reason_on_the_list_is_taken_and_recorded(layer, reason):
    assert layer.retire("feed-outage", 1, reason).status_code == 200

    assert ledger(layer)[-1]["data"]["reason"] == reason


def test_the_command_offers_the_reasons_the_backend_takes():
    assert main.RETIRE_REASONS == REASONS


@pytest.mark.parametrize(
    "day, playbook, option",
    [(DAY, "feed-outage", OPTION), (CROSS_CHECK_DAY, "missing-cross-check", CROSS_CHECK_OPTION)],
)
def test_with_no_earlier_version_triage_says_no_approved_step_applies(
    make_layer, day, playbook, option
):
    layer = make_layer(replies=lambda: [call("choose_step", option=option)])
    assert layer.triage(day)["notes"][0]["state"] == "chosen"

    assert layer.retire(playbook, 1).json()["now_in_use"] is None
    run = layer.triage(day)

    assert run["state"] == "completed"
    assert run["notes"][0]["statement"] == "No approved step applies."
    assert run["notes"][0]["action"] is None
    assert {note["state"] for note in run["notes"]} == {"no_step"}
    assert layer.stubs[-1].prompts == [], "a model was asked with nothing to choose"


def test_with_every_playbook_retired_the_page_and_triage_still_work(make_layer):
    layer = make_layer(replies=list)
    for playbook in ("feed-outage", "missing-cross-check"):
        assert layer.retire(playbook, 1).status_code == 200

    assert layer.triage()["state"] == "completed"
    assert "None is in use" in layer.request("GET", "/").text
    assert versions_in_use(layer) == []


def test_a_receipt_after_a_retirement_names_the_new_set(make_layer):
    layer = make_layer(replies=lambda: [call("choose_step", option=OPTION)])
    layer.put_in_use({"feed-outage": firm.steps("feed-outage", by="within 2 hours")})
    earlier = layer.triage()
    assert earlier["notes"][0]["action"]["by"] == "within 2 hours"

    retired = layer.retire("feed-outage", 2).json()["playbook_set"]
    later = layer.triage()

    assert earlier["playbook_set"]["version"] == firm.APPROVED_SET + 1
    assert (
        retired["version"] == firm.APPROVED_SET + 2
        and retired["hash"] != earlier["playbook_set"]["hash"]
    )
    for note in later["notes"]:
        assert note["receipt"]["playbook_set"] == retired
    assert later["notes"][0]["step"]["version"] == 1
    assert later["notes"][0]["action"]["by"] == "same_day 20:00 ET"
    assert retired["hash"] in layer.request("GET", "/").text
    assert layer.run(earlier["id"]) == earlier, "a past run was changed"


def test_a_retirement_while_wealthcontext_is_read_is_on_the_receipt(make_layer):
    layer = make_layer(replies=list)

    def retired_meanwhile(operation, envelope):
        if operation == "feed_runs":
            assert layer.retire("feed-outage", 1).status_code == 200
        return envelope

    layer.wealthcontext.edit = retired_meanwhile
    run = layer.triage()

    assert run["playbook_set"]["version"] == firm.APPROVED_SET + 1
    assert run["notes"][0]["state"] == "no_step"


def test_a_set_that_comes_back_still_has_a_new_version_and_hash(layer):
    founded = layer.as_analyst("GET", "/api/playbooks").json()
    layer.put_in_use({"feed-outage": firm.steps("feed-outage")})

    back = layer.retire("feed-outage", 2).json()["playbook_set"]

    assert back["version"] == firm.APPROVED_SET + 2 and back["hash"] != founded["hash"]
    assert layer.as_analyst("GET", "/api/playbooks").json()["in_use"] == founded["in_use"]


# What is said of a version nobody approved


NO_APPROVAL = "no approval is recorded"
WITHHELD = (
    "No approved step applies. The step that fits has no recorded approval, so no action is given."
)


@pytest.fixture
def unapproved(tmp_path):
    """A playbook folder whose founding versions have no recorded approval."""
    return firm.unapproved(firm.copy(tmp_path / "unapproved"))


@pytest.mark.parametrize("day, option", [(DAY, OPTION), (CROSS_CHECK_DAY, CROSS_CHECK_OPTION)])
def test_a_version_with_no_recorded_approval_gives_no_action(make_layer, unapproved, day, option):
    layer = make_layer(playbooks=unapproved, replies=lambda: [call("choose_step", option=option)])

    run = layer.triage(day)

    note = run["notes"][0]
    assert (run["state"], note["state"]) == ("completed", "chosen")
    assert note["step"]["approved_by"] is None
    assert note["action"] is None, "a person was told to act on what nobody approved"
    assert note["statement"] == WITHHELD
    page = layer.request("GET", "/").text
    assert page.count(NO_APPROVAL) == 2, "each version in use"
    assert WITHHELD in page
    said = main.describe(run)
    assert WITHHELD in said
    for telling in (page, said):
        assert "hold the advisor" not in telling.lower()
        assert "20:00 ET" not in telling and "10:00 ET" not in telling
    for line in layer.settings.triage_log.read_text().splitlines():
        if isinstance(settled := json.loads(line)["data"].get("settled"), dict):
            assert settled["note"]["action"] is None, "the log holds an action nobody was given"


def test_an_approved_version_gives_its_action_until_it_is_retired(make_layer, unapproved):
    layer = make_layer(playbooks=unapproved, replies=lambda: [call("choose_step", option=OPTION)])
    layer.put_in_use({"feed-outage": firm.steps("feed-outage", by="within 2 hours")})

    given = layer.triage()["notes"][0]

    assert given["step"]["approved_by"] == "playbook_owner"
    assert (given["action"]["by"], given["statement"]) == ("within 2 hours", None)
    assert layer.request("GET", "/").text.count(NO_APPROVAL) == 1, "the founding one still in use"
    assert layer.retire("feed-outage", 2).status_code == 200
    back = layer.triage()["notes"][0]
    assert (back["step"]["version"], back["action"], back["statement"]) == (1, None, WITHHELD)


# A note is settled only under the playbooks in use


USAGE = {"model": "stub:triage", "input_tokens": 1, "output_tokens": 1, "cost_usd": None}
RIGHT = {"kind": "step", "playbook": "feed-outage", "step": "portfolio-feed-failed"}


@pytest.mark.parametrize("refusals", [0, 1, 2])
@pytest.mark.parametrize("change", ["retire", "put in use"])
def test_no_note_is_settled_once_the_playbooks_changed(layer, change, refusals):
    run_id = layer.start()["id"]
    base = f"/api/agent/triage-runs/{run_id}/attempts/1"
    (note,) = layer.as_agent("GET", base).json()["notes"]
    of_note = f"{base}/notes/{note['id']}"
    for turn in range(1, refusals + 1):
        body = {"turn": turn, "choice": {"kind": "no_step"}, "usage": USAGE}
        assert layer.as_agent("POST", f"{of_note}/choice", json=body).status_code == 422
    before = layer.run(run_id)["notes"]
    if change == "retire":
        assert layer.retire("feed-outage", 1).status_code == 200
    else:
        layer.put_in_use({"missing-cross-check": firm.steps("missing-cross-check")})

    turn = min(refusals + 1, 2)
    for method, path, body in (
        ("GET", base, None),
        ("POST", f"{of_note}/choice", {"turn": turn, "choice": RIGHT, "usage": USAGE}),
        ("POST", f"{of_note}/choice", {"turn": 1, "choice": RIGHT, "usage": USAGE}),
        (
            "POST",
            f"{of_note}/unusable-reply",
            {"turn": turn, "reason": "no_tool_call", "calls": [], "usage": USAGE},
        ),
        ("POST", f"{of_note}/fallback", None),
    ):
        response = layer.as_agent(method, path, json=body)
        assert response.status_code == 409, path
        assert "working" in response.json()["detail"]
        assert "playbooks in use changed" in response.json()["detail"]
    assert layer.run(run_id)["notes"] == before
    assert layer.as_agent("POST", f"{base}/complete").status_code == 409


def test_a_model_choosing_while_its_step_is_retired_gives_out_no_action(make_layer):
    def retired_meanwhile():
        assert layer.retire("feed-outage", 1).status_code == 200
        return call("choose_step", option=OPTION)

    layer = make_layer(replies=lambda: [retired_meanwhile, call("choose_step", option=OPTION)])

    run = layer.triage()

    assert run["state"] == "failed"
    assert run["notes"][0]["state"] == "pending" and run["notes"][0]["action"] is None
    assert [step["outcome"] for step in run["steps"]] == ["refused", "refused"]
    assert "playbooks in use changed" in layer.stubs[0].prompts[1][-1].text, "the model read it"


def test_a_run_with_every_note_settled_still_completes_after_a_change(layer):
    run_id = layer.start()["id"]
    base = f"/api/agent/triage-runs/{run_id}/attempts/1"
    (note,) = layer.as_agent("GET", base).json()["notes"]
    body = {"turn": 1, "choice": RIGHT, "usage": USAGE}
    path = f"{base}/notes/{note['id']}/choice"
    chosen = layer.as_agent("POST", path, json=body).json()
    assert layer.retire("feed-outage", 1).status_code == 200

    assert layer.as_agent("POST", path, json=body).json() == chosen, "the same move sent again"
    assert layer.as_agent("GET", base).json()["notes"] == []
    assert layer.as_agent("POST", f"{base}/complete").json() == {"state": "completed"}


def test_retirements_at_once_make_one_move(layer):
    layer.put_in_use({"feed-outage": firm.steps("feed-outage")})
    with ThreadPoolExecutor(len(REASONS)) as pool:
        found = list(pool.map(lambda reason: layer.retire("feed-outage", 2, reason), REASONS))

    assert sorted(each.status_code for each in found) == [200, 409, 409, 409]
    assert len(ledger(layer)) == len(firm.FOUNDING_EVENTS) + 2


# The command


def test_the_retire_command_as_the_owner_and_as_the_analyst(layer, monkeypatch, tmp_path, capsys):
    layer.put_in_use({"feed-outage": firm.steps("feed-outage")})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PLAYBOOKS_URL", layer.url)
    monkeypatch.setenv("PLAYBOOKS_ANALYST_TOKEN", ANALYST)
    monkeypatch.setenv("PLAYBOOKS_OWNER_TOKEN", OWNER)
    command = ["retire", "--playbook", "feed-outage", "--version", "2", "--reason", "wrong_action"]

    assert main.main([*command, "--as", "analyst"]) == 1
    assert capsys.readouterr().err == "403: Only the playbook owner may do this.\n"
    assert main.main(command) == 0
    said = capsys.readouterr().out
    assert "Retired feed-outage version 2. Version 1 is in use again." in said
    assert f"version {firm.APPROVED_SET + 2}, " in said
    last = ["retire", "--playbook", "feed-outage", "--version", "1", "--reason", "wrong_action"]
    assert main.main(last) == 0
    assert "No version of feed-outage is in use." in capsys.readouterr().out
