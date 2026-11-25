"""`playbooks approve` and `playbooks refuse`: the exact words first, then the hash to confirm."""

import io

import approving
import curation
import firm
import gating
import knowledge
import pytest
import stand_ins
import yaml
from layer import ANALYST, OWNER, ROOT

from cli import main


@pytest.fixture
def command(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PLAYBOOKS_ANALYST_TOKEN", ANALYST)
    monkeypatch.setenv("PLAYBOOKS_OWNER_TOKEN", OWNER)

    def run(layer, *arguments: str, typed: str = "") -> tuple[int, str, str]:
        monkeypatch.setenv("PLAYBOOKS_URL", layer.url)
        monkeypatch.setattr("sys.stdin", io.StringIO(typed))
        status = main.main(list(arguments))
        said = capsys.readouterr()
        return status, said.out, said.err

    return run


@pytest.fixture
def passed(make_layer):
    return approving.first_cycle_passed(make_layer)


def words(memo: str) -> str:
    """A memo's text, as its file has it."""
    return (ROOT / "sources" / "memos" / f"{memo}.md").read_text().split("---\n", 2)[2].strip()


def quoted(text: str) -> str:
    return "\n".join(f"       | {line}".rstrip() for line in text.split("\n"))


# As `approve` numbers them: the correction's disagreement, then the gap report's.
RULED = (f"1={approving.CORRECTION}", f"2={approving.GAP_REPORT}")


def approving_with(proposal: dict, *ruled: str) -> list[str]:
    named = [part for each in ruled for part in ("--rule-for", each)]
    return ["approve", "--proposal", proposal["id"], *named]


def test_approve_shows_each_change_next_to_the_exact_words_then_asks_for_the_hash(passed, command):
    layer, proposal, run = passed

    status, said, _ = command(
        layer, *approving_with(proposal, *RULED), typed=f"{proposal['sha256']}\n"
    )

    assert status == 0, said
    asked = said.index(main.CONFIRM)
    one, two, three = (said.index(f"\n{n}. ") for n in (1, 2, 3))
    disagreement = said.index("\nDisagreement: ")
    assert one < two < three < disagreement < asked < said.index("Approved proposal")
    feed, added, cross = said[one:two], said[two:three], said[three:disagreement]
    # Each source's exact words are under the change that cites it.
    memo = quoted(words("custodian-b-feed-escalation"))
    founding = quoted(words("feed-outage-founding"))
    assert memo in feed and founding in feed
    assert memo in added and founding not in added
    correction = knowledge.committed_correction()["text"]
    for text in (words("missing-cross-check-founding"), correction, gating.SCRIPTED_SUMMARY):
        assert quoted(text) in cross
        # And again with the disagreement, side by side.
        assert quoted(text) in said[disagreement:asked]
    # The changed steps.
    assert "1. change feed-outage/portfolio-feed-failed, version 1" in feed
    assert "in version 1: when SOURCE_UNAVAILABLE: source_family PAS, custodian any" in feed
    assert "proposed: when SOURCE_UNAVAILABLE: source_family PAS, " in feed
    assert "tell Custodian B Service Desk, by within 2 hours, hold the advisor's report" in added
    assert "next_business_day 10:00 ET, hold the advisor's report" in cross
    assert "next_business_day 10:00 ET, do not hold the advisor's report" in cross
    assert "corrects custody-cross-check-missing: hold_report should be no" in cross
    assert gating.HONEST["disagreements"][0]["about"] in said[disagreement:asked]
    assert "Approving needs your ruling" in said[disagreement:asked]
    for number, source in ((1, approving.CORRECTION), (2, approving.GAP_REPORT)):
        listed = f"  {number}: on change 3, between {source}, {approving.FOUNDING}"
        assert listed in said[disagreement:asked]
    assert f"hash {proposal['sha256']}" in said[:one] and f"gate run {run['id']}" in said[:one]
    assert approving.state(layer, proposal["id"]) == "approved"
    assert f"ruled for {approving.CORRECTION}" in said and "gap report resolved: " in said


def test_the_view_gives_the_exact_words_the_curator_was_given(passed):
    layer, proposal, _ = passed

    shown = layer.as_analyst("GET", f"/api/proposals/{proposal['id']}").json()

    feed, added, cross = shown["changes"]
    assert [c["reference"] for c in feed["cites"]] == [curation.MEMO, curation.FOUNDING]
    assert feed["cites"][0]["words"] == words("custodian-b-feed-escalation")
    assert feed["cites"][1]["words"] == words("feed-outage-founding")
    assert added["before"] is None and added["desk"] == "Custodian B Service Desk"
    given = {each["reference"]: each["words"] for each in cross["cites"]}
    assert given[approving.CORRECTION] == knowledge.committed_correction()["text"]
    assert given[approving.GAP_REPORT] == gating.SCRIPTED_SUMMARY
    assert cross["before"]["hold_report"] is True
    assert [each["between"] for each in shown["disagreements"]] == [
        gating.HONEST["disagreements"][0]["between"]
    ]


def test_the_view_shows_no_action_of_a_version_nobody_approved(make_layer, command, tmp_path):
    unapproved = firm.unapproved(firm.copy(tmp_path / "unapproved"))
    layer, proposal, _ = approving.first_cycle_passed(make_layer, playbooks=unapproved)

    shown = layer.as_owner("GET", f"/api/proposals/{proposal['id']}").json()
    _, said, _ = command(layer, *approving_with(proposal), typed="no\n")

    for change in (shown["changes"][0], shown["changes"][2]):
        before = change["before"]
        assert before["approval_recorded"] is False
        assert {before[k] for k in ("tell", "desk", "by", "hold_report")} == {None}
    assert said.count("no approval is recorded for it, so its action is not shown") == 2


def test_the_view_stays_what_was_approved_after_the_set_has_moved_on(passed):
    layer, proposal, run = passed
    path = f"/api/proposals/{proposal['id']}"
    before = layer.as_owner("GET", path).json()

    approving.approved(layer, proposal, run)

    assert layer.as_owner("GET", path).json() == before | {"state": "approved"}


@pytest.mark.parametrize("typed", ["", "\n", "yes\n", "0" * 64 + "\n"])
def test_approve_without_the_hash_approves_nothing(passed, command, typed):
    layer, proposal, _ = passed
    before = firm.files(layer.settings.playbooks)

    status, said, error = command(layer, *approving_with(proposal, *RULED), typed=typed)

    assert status == 1
    assert main.CONFIRM in said and error.strip() == main.NOT_CONFIRMED
    assert firm.files(layer.settings.playbooks) == before


@pytest.mark.parametrize(
    "given",
    [approving.CORRECTION, f"9={approving.CORRECTION}", f"2={approving.CORRECTION}", "1=", "x=y"],
)
def test_a_ruling_that_names_none_of_the_list_is_not_sent(passed, command, given):
    layer, proposal, _ = passed

    status, said, error = command(
        layer, *approving_with(proposal, given), typed=f"{proposal['sha256']}\n"
    )

    assert status == 1 and error.startswith("A --rule-for is <number>=<source>")
    assert main.CONFIRM not in said
    assert approving.state(layer, proposal["id"]) == "gate_passed"


def test_approve_with_no_ruling_says_one_is_needed(passed, command):
    layer, proposal, _ = passed

    status, _, error = command(layer, *approving_with(proposal), typed=f"{proposal['sha256']}\n")

    assert status == 1 and error.startswith("422: ") and "has no ruling" in error
    assert approving.state(layer, proposal["id"]) == "gate_passed"


def test_approve_asks_nothing_of_a_proposal_that_has_not_passed(make_layer, command):
    layer = approving.scripted(make_layer, gating.honest())
    gating.first_cycle(layer)
    proposal = approving.drafted(layer)

    status, said, error = command(layer, *approving_with(proposal))

    assert status == 1 and "The proposal is drafted" in error
    assert main.CONFIRM not in said


def test_approve_names_the_model_the_held_out_trials_ran_on_before_asking(passed, command):
    layer, proposal, run = passed

    status, said, _ = command(
        layer, *approving_with(proposal, *RULED), typed=f"{proposal['sha256']}\n"
    )

    assert status == 0, said
    named = said.index(f"The held-out trials of gate run {run['id']} ran on stub:triage.")
    assert said.index("Approving needs your ruling") < named < said.index(main.CONFIRM)
    assert "replay" not in said[named:]


# A replay's setting, and how its replies name themselves.
REPLAY = "replay:recordings/scripted-triage.json"
# What the playbook owner must be told of replayed trials, in the order the command says it.
TOLD = (
    "That is a replay",
    "the trials' replies were read from a file",
    "no live model ran these trials",
    "A real promotion should rest on held-out trials run on a live model",
    "approving on a replay is still allowed",
)


def warned(said: str) -> int:
    """Where the replay warning starts, once each of its statements is there in order."""
    at = [said.index(each) for each in TOLD]
    assert at == sorted(at), said
    return at[0]


@pytest.fixture
def replayed(make_layer):
    """A proposal passed by a gate whose held-out trials were replayed from a file."""
    return approving.first_cycle_passed(
        make_layer,
        agent=lambda url, key: stand_ins.trial_agent(
            url, key, model=REPLAY, answers_as="replay:scripted-triage (scripted)"
        ),
        overrides={"triage_model": REPLAY},
    )


def test_approve_says_plainly_the_trials_were_replayed_and_still_approves(replayed, command):
    layer, proposal, run = replayed
    assert run["held_out"]["trial_model"] == REPLAY

    status, said, _ = command(
        layer, *approving_with(proposal, *RULED), typed=f"{proposal['sha256']}\n"
    )

    assert status == 0, said
    named = said.index(f"The held-out trials of gate run {run['id']} ran on {REPLAY}.")
    assert named < warned(said) < said.index(main.CONFIRM)
    assert approving.state(layer, proposal["id"]) == "approved"


def test_the_replay_warning_comes_before_the_hash_is_asked(replayed, command):
    layer, proposal, _ = replayed

    status, said, error = command(layer, *approving_with(proposal, *RULED), typed="no\n")

    assert status == 1 and error.strip() == main.NOT_CONFIRMED
    assert warned(said) < said.index(main.CONFIRM)
    assert approving.state(layer, proposal["id"]) == "gate_passed"


@pytest.mark.parametrize(
    ("model", "is_replay"),
    [
        (REPLAY, True),
        ("replay:recordings/first-cycle-triage.json", True),
        ("anthropic:claude-opus-5-5", False),
        ("stub:triage", False),
        ("stub:replay", False),
        ("replays:recordings/scripted-triage.json", False),
        # A replay names its file: the provider alone is no replay.
        ("replay", False),
        ("replay:", False),
    ],
)
def test_a_trials_model_is_a_replay_of_a_file_by_its_provider(model, is_replay):
    assert main.replayed(model) is is_replay


def test_the_analyst_cannot_approve_or_refuse_by_the_command(passed, command):
    layer, proposal, _ = passed
    typed = f"{proposal['sha256']}\n"

    approve = command(layer, *approving_with(proposal), "--as", "analyst", typed=typed)
    refuse = command(
        layer, "refuse", "--proposal", proposal["id"], "--reason", "not_needed", "--as", "analyst"
    )

    for status, _, error in (approve, refuse):
        assert status == 1 and error.startswith("403: ")
    assert approving.state(layer, proposal["id"]) == "gate_passed"


def test_refuse_says_what_it_did(passed, command):
    layer, proposal, _ = passed
    (report,) = approving.reports(layer)

    status, said, _ = command(
        layer, "refuse", "--proposal", proposal["id"], "--reason", "source_not_trusted"
    )

    assert status == 0
    assert f"Refused proposal {proposal['id']}: source_not_trusted" in said
    assert f"gap report open again: {report}" in said


def test_the_commands_reasons_are_the_backends(passed):
    layer, _, _ = passed
    listed = layer.backend.openapi()["components"]["schemas"]["Refuse"]["properties"]["reason"]

    assert tuple(listed["enum"]) == main.REFUSE_REASONS


def test_an_approved_file_is_plain_yaml_a_person_reads(passed):
    layer, proposal, run = passed
    approving.approved(layer, proposal, run)

    written = yaml.safe_load((layer.settings.playbooks / "feed-outage" / "2.yaml").read_text())

    assert list(written) == ["id", "version", "replaces", "steps"]
