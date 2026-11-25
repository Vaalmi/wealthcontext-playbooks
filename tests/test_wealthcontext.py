"""The one client to WealthContext, and how exceptions are built from what it read."""

import ast
import copy

import pytest
from layer import CUTOFF, DAY, ROOT, address, listen, serve
from stand_ins import StandInWealthContext, recorded, sign_in

from backend import exceptions
from backend.settings import Settings
from backend.wealthcontext import (
    ROUTES,
    Flag,
    Impact,
    NotOperations,
    Reading,
    WealthContext,
    WealthContextFailed,
)

THREE = {"/api/get_data_quality_report", "/api/feed_runs", "/api/feed_run_impact"}


def settings(tmp_path, url: str, token: str) -> Settings:
    return Settings(
        database=tmp_path / "unused.sqlite",
        playbooks=ROOT / "playbooks",
        triage_log=tmp_path / "unused.jsonl",
        wealthcontext_url=url,
        agent_url="http://127.0.0.1:9",
        wealthcontext_token=token,
        analyst_token="",
        playbook_owner_token="",
        triage_agent_key="",
    )


@pytest.mark.parametrize(
    "token",
    [
        sign_in("COMP-01", "compliance"),
        sign_in("ADV-01", "advisor"),
        sign_in("ENG-01", "engineer"),
        sign_in("OPS-01", "compliance"),
        sign_in("COMP-01", "ops"),
        "",
        "not-a-token",
        "a.b.c",
    ],
)
def test_the_client_can_sign_in_only_as_operations(tmp_path, token):
    with pytest.raises(NotOperations):
        WealthContext(settings(tmp_path, "http://127.0.0.1:9", token))


@pytest.fixture
def standing_in(tmp_path):
    stand_in, bound = StandInWealthContext(), listen()
    given = settings(tmp_path, address(bound), stand_in.token)
    with serve((stand_in.app, bound)), WealthContext(given) as client:
        yield stand_in, client


def test_the_client_calls_only_the_three_read_routes(standing_in):
    stand_in, client = standing_in
    client.read_day(DAY, CUTOFF)

    assert {path for path, _, _ in stand_in.asked} == THREE
    assert {sent for _, _, sent in stand_in.asked} == {f"Bearer {stand_in.token}"}
    assert {f"/api/{route}" for route in ROUTES} == THREE
    tree = ast.parse((ROOT / "backend" / "wealthcontext.py").read_text())
    sending = {"post", "get", "put", "patch", "delete", "request", "send", "stream"}
    sends = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in sending
        and ast.unparse(node.func.value) == "self._http"
    ]
    assert sends == ["post"], "one place sends, and it takes a route from the closed list"


def test_no_other_backend_module_reaches_wealthcontext():
    for module in (ROOT / "backend").glob("*.py"):
        if module.stem == "wealthcontext":
            continue
        source = module.read_text()
        assert "settings.wealthcontext_token" not in source, module.stem
        if module.stem != "asking":
            assert "httpx2.Client(" not in source, module.stem


def test_the_only_persona_anywhere_is_operations():
    folders = ("backend", "triage_agent", "modelkit", "cli", "scripts")
    named = set()
    for folder in folders:
        for module in (ROOT / folder).glob("*.py"):
            source = module.read_text()
            named |= {p for p in ("COMP-01", "ENG-01", "ADV-", "OPS-01") if p in source}
    assert named == {"OPS-01"}


def cut_short(listing: str):
    def edit(operation, envelope):
        if operation == "feed_runs" and listing == "runs":
            envelope["data"].update(truncated=True, total_item_count=21)
        if operation == "feed_run_impact" and listing == "accounts":
            envelope["data"]["affected_accounts"].update(truncated=True, total_item_count=21)
        return envelope

    return edit


def changed(operation_changed: str, change):
    def edit(operation, envelope):
        if operation == operation_changed:
            change(envelope)
        return envelope

    return edit


@pytest.mark.parametrize(
    "edit, line",
    [
        (cut_short("runs"), "cut its list of feed runs short"),
        (cut_short("accounts"), "cut its list of affected accounts short"),
        (
            changed("feed_run_impact", lambda e: e["data"].update(run_id="RUN-other")),
            "describes another feed run",
        ),
        (
            changed(
                "feed_run_impact",
                lambda e: e["data"].update(
                    account_attribution="NOT_ATTRIBUTABLE", affected_accounts=None
                ),
            ),
            "does not name the feed's accounts",
        ),
        (changed("feed_runs", lambda e: e.update(valuation_date="2026-10-14")), "valuation"),
        (changed("feed_runs", lambda e: e.update(as_of="2026-10-14")), "another date"),
        (
            changed(
                "get_data_quality_report", lambda e: e.update(known_as_of="2026-10-01T00:00:00Z")
            ),
            "another cutoff",
        ),
        (changed("get_data_quality_report", lambda e: e.pop("quality_flags")), "not in its shape"),
        (
            changed(
                "get_data_quality_report",
                lambda e: e["quality_flags"][0].update(subject="A name with spaces"),
            ),
            "not in its shape",
        ),
        (changed("get_data_quality_report", lambda e: e.update(status="ERROR")), "not in its"),
        (
            changed("get_data_quality_report", lambda e: e["quality_flags"].pop()),
            "leaves out a warning it lists",
        ),
    ],
)
def test_part_of_an_answer_is_never_used(standing_in, edit, line):
    stand_in, client = standing_in
    stand_in.edit = edit

    with pytest.raises(WealthContextFailed, match=line) as failed:
        client.read_day(DAY, CUTOFF)
    assert "A name with spaces" not in str(failed.value)


def test_a_household_list_cut_at_twenty_is_no_cut_in_the_report(standing_in):
    stand_in, client = standing_in

    def twenty_one(envelope):
        # The report's own list is whole even when the list by household is capped.
        envelope["data"].update(truncated=True, total_item_count=21)

    stand_in.edit = changed("get_data_quality_report", twenty_one)

    assert len(client.read_day(DAY, CUTOFF).flags) == 26


def test_a_refusal_names_wealthcontexts_code(standing_in):
    _, client = standing_in

    with pytest.raises(WealthContextFailed, match="AS_OF_OUT_OF_RANGE"):
        client.read_day("2026-12-01", CUTOFF)


def reading(flags: list[dict], impacts: list[tuple[dict, set[str]]]) -> Reading:
    return Reading(
        reads=[],
        flags=[Flag.model_validate(flag) for flag in flags],
        impacts=[
            Impact.model_validate({"run": run, "affected_accounts": accounts})
            for run, accounts in impacts
        ],
    )


def failed_run(**changes) -> dict:
    runs = recorded(DAY, "feed_runs")["data"]["items"]
    run = copy.deepcopy(next(run for run in runs if run["status"] != "SUCCESS"))
    return {**run, **changes}


def warning(code: str, subject: str = "WC-AC-0002", **detail) -> dict:
    return {"code": code, "severity": "warning", "subject": subject, "detail": detail}


@pytest.mark.parametrize(
    "flag",
    [
        warning("OWNERSHIP_CONFLICT", source="PAS"),
        warning("SOURCE_UNAVAILABLE", source="CUSTODY"),
        warning("SOURCE_UNAVAILABLE"),
        warning("SOURCE_UNAVAILABLE", subject="WC-AC-0099", source="PAS"),
    ],
)
def test_a_warning_joins_a_feed_only_when_all_three_hold(flag):
    (found,), notices = exceptions.build(reading([flag], [(failed_run(), {"WC-AC-0002"})]))

    assert found.facts is None and found.feed_run is None
    assert found.subjects == [flag["subject"]]
    assert notices == []


def test_a_warning_two_runs_could_explain_is_one_exception_under_the_latest():
    latest, earlier = failed_run(id="RUN-latest"), failed_run(id="RUN-earlier")
    both = {"WC-AC-0002"}
    flags = [warning("SOURCE_UNAVAILABLE", source="PAS")]

    (found,), _ = exceptions.build(reading(flags, [(latest, both), (earlier, both)]))

    assert found.feed_run is not None and found.feed_run.id == "RUN-latest"


def test_a_failed_feed_with_no_account_list_groups_nothing():
    run = failed_run(source_system="SECMASTER", feed="prices")
    flags = [warning("SOURCE_UNAVAILABLE", source="PAS")]

    (found,), _ = exceptions.build(reading(flags, [(run, set())]))

    assert found.facts is None
