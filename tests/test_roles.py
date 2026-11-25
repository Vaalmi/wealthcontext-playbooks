"""Who may do what: the analyst, the curator and the playbook owner kept apart.

One row for each operation and each caller, so that a new operation has to say whose it is.
"""

from dataclasses import replace

import approving
import curation
import firm
import pytest
from layer import AGENT_KEY, ANALYST, CURATOR_KEY, OWNER

from backend.main import create_app
from backend.settings import Settings

WHOSE = {
    "public": {"health", "show_page", "show_triage_run", "press_cycle_button", "read_cycle"},
    "analyst": {"start_triage", "record_correction", "request_review"},
    "owner": {
        "approve",
        "refuse",
        "approve_founding",
        "read_founding",
        "retire",
        "rebuild",
        "intake",
        "dismiss_gap_report",
        "start_curator_run",
    },
    "either person": {
        "run_gate",
        "check_proposal",
        "read_playbooks",
        "read_record",
        "read_triage",
        "read_triage_runs",
        "read_sources",
        "read_corrections",
        "read_correction",
        "read_gap_reports",
        "read_curator_run",
        "read_curator_runs",
        "read_proposal",
    },
    "triage agent": {
        "read_triage_work",
        "record_choice",
        "record_unusable_reply",
        "record_fallback",
        "file_gap_report",
        "record_no_gap_report",
        "complete_triage",
        "fail_triage",
        "read_review_work",
        "record_review",
        "record_unusable_review",
        "record_review_fallback",
        "stop_review",
    },
    "curator": {
        "read_curator_work",
        "record_proposal",
        "record_no_change",
        "record_unusable_curator_reply",
        "fail_curator_run",
    },
}
OPERATIONS = sorted(name for names in WHOSE.values() for name in names)
CALLERS = {
    "nobody": {},
    "the analyst": {"Authorization": f"Bearer {ANALYST}"},
    "the owner": {"Authorization": f"Bearer {OWNER}"},
    "the triage agent": {"X-Triage-Agent-Key": AGENT_KEY},
    "the curator": {"X-Curator-Key": CURATOR_KEY},
    # A secret sent as another role's kind of secret.
    "the triage agent's key as a token": {"Authorization": f"Bearer {AGENT_KEY}"},
    "the curator's key as a token": {"Authorization": f"Bearer {CURATOR_KEY}"},
    "the owner's token as each key": {"X-Triage-Agent-Key": OWNER, "X-Curator-Key": OWNER},
    "the analyst's token as each key": {"X-Triage-Agent-Key": ANALYST, "X-Curator-Key": ANALYST},
}


def refusal(whose: str, caller: str) -> int | None:
    """The status that refuses this caller, or None where it is let through to the operation."""
    person = {"the analyst": "analyst", "the owner": "owner"}.get(caller)
    if whose == "public":
        return None
    if whose in ("triage agent", "curator"):
        return None if caller == f"the {whose}" else 401
    if person is None:
        return 401  # an agent's key is no person's token
    return None if whose in ("either person", person) else 403


@pytest.fixture
def layer(make_layer):
    return make_layer(agent=curation.idle, curator=curation.idle)


@pytest.fixture
def routes(layer):
    return {
        operation["operationId"]: (method.upper(), path, operation)
        for path, methods in layer.backend.openapi()["paths"].items()
        for method, operation in methods.items()
    }


def test_every_operation_says_whose_it_is(routes):
    assert sorted(routes) == OPERATIONS, "say in WHOSE whose the new operation is"


@pytest.mark.parametrize("caller", CALLERS)
def test_each_operation_opens_to_its_role_alone(layer, routes, caller):
    for whose, names in WHOSE.items():
        for name in sorted(names):
            method, path, _ = routes[name]
            where = path.replace("{attempt}", "1").replace("{version}", "9")
            for parameter in ("run_id", "note_id", "proposal_id", "correction_id", "report_id"):
                where = where.replace(f"{{{parameter}}}", "0" * 32)
            where = where.replace("{playbook}", "no-such-playbook")
            before = firm.files(layer.settings.playbooks)

            response = layer.request(method, where, dict(CALLERS[caller]), json={})

            wanted = refusal(whose, caller)
            if wanted is None:
                assert response.status_code not in (401, 403), (name, caller)
            else:
                assert response.status_code == wanted, (name, caller)
                assert firm.files(layer.settings.playbooks) == before, (name, caller)


MOVES = ("approve", "refuse", "retire")


@pytest.mark.parametrize("move", MOVES)
@pytest.mark.parametrize("caller", [each for each in CALLERS if each != "the owner"])
def test_nobody_but_the_owner_approves_refuses_or_retires(make_layer, move, caller):
    layer, proposal, run = approving.first_cycle_passed(make_layer)
    paths = {
        "approve": (
            f"/api/proposals/{proposal['id']}/approve",
            {
                "sha256": proposal["sha256"],
                "gate_run": run["id"],
                "rulings": approving.rulings(layer, proposal),
            },
        ),
        "refuse": (f"/api/proposals/{proposal['id']}/refuse", {"reason": "not_needed"}),
        "retire": ("/api/playbooks/feed-outage/versions/1/retire", {"reason": "wrong_action"}),
    }
    path, body = paths[move]
    before = firm.files(layer.settings.playbooks), firm.records(layer.settings.database)

    response = layer.request("POST", path, dict(CALLERS[caller]), json=body)

    assert response.status_code == (403 if caller == "the analyst" else 401)
    assert (firm.files(layer.settings.playbooks), firm.records(layer.settings.database)) == before
    # A request that is whole and right: the owner's own is taken.
    assert layer.as_owner("POST", path, json=body).status_code == 200


SECRETS = ("analyst_token", "playbook_owner_token", "triage_agent_key", "curator_key")


@pytest.mark.parametrize(
    "first, second",
    [(first, second) for n, first in enumerate(SECRETS) for second in SECRETS[n + 1 :]],
)
def test_the_backend_does_not_start_where_two_roles_share_a_secret(tmp_path, first, second):
    settings = Settings(
        database=tmp_path / "shared.sqlite",
        playbooks=firm.copy(tmp_path / "playbooks"),
        triage_log=tmp_path / "log.jsonl",
        wealthcontext_url="http://127.0.0.1:9",
        agent_url="http://127.0.0.1:9",
        wealthcontext_token="",
        analyst_token=ANALYST,
        playbook_owner_token=OWNER,
        triage_agent_key=AGENT_KEY,
        curator_key=CURATOR_KEY,
    )

    with pytest.raises(ValueError, match="must all differ") as stopped:
        create_app(replace(settings, **{second: getattr(settings, first)}))

    assert getattr(settings, first) not in str(stopped.value)
