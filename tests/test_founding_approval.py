"""The playbook owner's approval of the founding versions: a ledger line of its own.

The first line is never written again. The approval names what it names, is refused when
anything differs, holds at its commit point, and gives version 1 notes their actions.
"""

import json

import firm
import knowledge
import pytest
from killed import POINTS
from layer import AGENT_KEY, ANALYST, CROSS_CHECK_DAY, CURATOR_KEY, OWNER, ROOT, running_layer
from stand_ins import scripted_agent
from test_curation_restore import kill_during, logged, started

from backend.chainlog import LogInvalid, following
from backend.playbook_format import content_hash

OWNER_HEADERS = {"Authorization": f"Bearer {OWNER}"}
FIRST = json.loads((ROOT / "playbooks" / "ledger.jsonl").read_text().splitlines()[0])
EVENT = "founding_approved"
PATH = "/api/founding/approve"


@pytest.fixture
def layer(make_layer, tmp_path):
    folder = firm.unapproved(firm.copy(tmp_path / "playbooks"))
    return make_layer(playbooks=folder, agent=scripted_agent)


def approve(layer, sha256: str = FIRST["hash"], send=None):
    return (send or layer.as_owner)("POST", PATH, json={"sha256": sha256})


def kept(layer) -> tuple:
    return firm.files(layer.settings.playbooks), firm.records(layer.settings.database)


def test_the_owner_approves_with_one_line_naming_exactly_what_the_first_names(layer):
    first = knowledge.ledger(layer)[0]
    assert first["data"]["approved_by"] is None

    done = approve(layer)

    assert done.status_code == 200, done.text
    lines = knowledge.ledger(layer)
    assert lines[0] == first, "the first line is never written again"
    (line,) = lines[1:]
    assert (line["event"], line["who"]) == (EVENT, "playbook_owner")
    assert line["data"] == {
        "founding": {"line": 1, "hash": first["hash"]},
        "versions": first["data"]["versions"],
        "desks_sha256": first["data"]["desks_sha256"],
    }
    in_use = layer.as_analyst("GET", "/api/playbooks").json()
    assert (in_use["version"], done.json()["playbook_set"]) == (
        2,
        {"id": in_use["id"], "version": 2, "hash": in_use["hash"]},
    )


def test_after_it_a_version_one_note_gives_its_action(layer):
    before = layer.triage()
    (unapproved,) = [n for n in before["notes"] if n["state"] == "chosen"]
    assert unapproved["action"] is None and unapproved["step"]["approved_by"] is None

    assert approve(layer).status_code == 200
    after = layer.triage()

    (chosen,) = [n for n in after["notes"] if n["state"] == "chosen"]
    assert (chosen["step"]["version"], chosen["step"]["approved_by"]) == (1, "playbook_owner")
    assert chosen["action"] == {
        "tell": "portfolio-accounting",
        "desk": "Portfolio Accounting Operations",
        "by": "same_day 20:00 ET",
        "hold_report": True,
        "line": "Ask for the failed portfolio accounting fetch to be run again.",
    }
    assert chosen["receipt"]["playbook_set"]["version"] == 2
    assert unapproved["receipt"]["playbook_set"]["version"] == 1


def test_the_owner_reads_what_is_approved_beside_the_memos_own_words(layer):
    shown = layer.as_owner("GET", "/api/founding").json()

    assert shown["hash"] == FIRST["hash"] and shown["approved_by"] is None
    assert shown["desks"]["sha256"] == FIRST["data"]["desks_sha256"]
    assert shown["desks"]["text"] == (ROOT / "playbooks" / "desks.yaml").read_text()
    for version in shown["versions"]:
        file = ROOT / "playbooks" / version["id"] / "1.yaml"
        assert (version["text"], version["sha256"]) == (
            file.read_text(),
            content_hash(file.read_bytes()),
        )
        (memo,) = version["rests_on"]
        name = memo["id"].removeprefix("memo-")
        written = (ROOT / "sources" / "memos" / f"{name}.md").read_text()
        assert memo["words"] == written.split("---\n", 2)[2].strip()
        assert memo["sha256"] == content_hash(written.encode())


@pytest.mark.parametrize(
    "headers, status",
    [
        ({"Authorization": f"Bearer {ANALYST}"}, 403),
        ({"X-Triage-Agent-Key": AGENT_KEY}, 401),
        ({"X-Curator-Key": CURATOR_KEY}, 401),
        ({"Authorization": f"Bearer {AGENT_KEY}"}, 401),
        ({}, 401),
    ],
)
def test_nobody_but_the_owner_approves_or_reads_it(layer, headers, status):
    before = kept(layer)

    for method, path, body in (
        ("POST", PATH, {"sha256": FIRST["hash"]}),
        ("GET", "/api/founding", None),
    ):
        assert layer.request(method, path, headers, json=body).status_code == status

    assert kept(layer) == before


REFUSED = {
    "another hash": lambda layer, _: approve(layer, "0" * 64),
    "a version retired": lambda layer, _: (
        layer.retire("feed-outage", 1),
        approve(layer),
    )[1],
    "a version superseded": lambda layer, _: (
        layer.put_in_use({"feed-outage": firm.steps("feed-outage", by="within 2 hours")}),
        approve(layer),
    )[1],
    "a version file changed": lambda layer, _: (
        (layer.settings.playbooks / "feed-outage" / "1.yaml").write_bytes(
            (layer.settings.playbooks / "feed-outage" / "1.yaml").read_bytes() + b"\n"
        ),
        approve(layer),
    )[1],
    "the desks changed": lambda layer, _: (
        (layer.settings.playbooks / "desks.yaml").write_bytes(
            (layer.settings.playbooks / "desks.yaml").read_bytes() + b"\n"
        ),
        approve(layer),
    )[1],
}


@pytest.mark.parametrize("what", REFUSED)
def test_anything_that_is_not_what_the_first_line_and_the_files_hold_is_refused(
    layer, tmp_path, what
):
    lines = knowledge.ledger(layer)

    refused = REFUSED[what](layer, tmp_path)

    assert refused.status_code == 409, refused.text
    assert list(refused.json()) == ["detail"]
    assert knowledge.events(layer)[len(lines) :] in (
        [],
        ["version_retired"],
        ["versions_put_in_use"],
    )
    assert EVENT not in knowledge.events(layer)


def test_a_founding_memo_that_is_not_as_line_one_names_it_is_refused(make_layer, tmp_path):
    sources = tmp_path / "sources"
    for each in (ROOT / "sources").rglob("*.md"):
        target = sources / each.relative_to(ROOT / "sources")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(each.read_bytes())
    memo = sources / "memos" / "missing-cross-check-founding.md"
    memo.write_bytes(memo.read_bytes() + b"\nOne more line.\n")
    folder = firm.unapproved(firm.copy(tmp_path / "playbooks"))
    layer = make_layer(playbooks=folder, overrides={"sources": sources})

    refused = approve(layer)

    assert refused.status_code == 409
    assert "memo-missing-cross-check-founding" in refused.json()["detail"]
    assert EVENT not in knowledge.events(layer)


def test_approved_already_is_refused_and_the_same_move_again_is_the_first(
    layer, make_layer, tmp_path
):
    first = approve(layer)
    lines = knowledge.ledger(layer)

    again = approve(layer)
    other = approve(layer, "f" * 64)

    assert (again.status_code, again.json()) == (200, first.json())
    assert other.status_code == 409
    assert knowledge.ledger(layer) == lines
    # A founding approved in its own first line has nothing to approve.
    founded = make_layer(playbooks=firm.found(firm.copy(tmp_path / "founded")))
    refused = approve(founded, knowledge.ledger(founded)[0]["hash"])
    assert refused.status_code == 409 and "approved" in refused.json()["detail"]


def test_it_rebuilds_exactly(layer):
    assert approve(layer).status_code == 200
    layer.triage(CROSS_CHECK_DAY)
    stored = firm.records(layer.settings.database)
    seen = [layer.as_owner("GET", p).text for p in ("/api/record", "/api/playbooks")]

    assert layer.as_owner("POST", "/api/rebuild").status_code == 200

    assert firm.records(layer.settings.database) == stored
    assert seen == [layer.as_owner("GET", p).text for p in ("/api/record", "/api/playbooks")]


def test_a_changed_byte_of_the_approval_line_stops_the_backend(layer):
    assert approve(layer).status_code == 200
    settings = layer.settings
    ledger = settings.playbooks / "ledger.jsonl"
    lines = ledger.read_bytes().splitlines(keepends=True)
    changed = bytearray(lines[1])
    changed[len(changed) // 2] ^= 1

    layer.backend.state.journal.close()
    ledger.write_bytes(lines[0] + bytes(changed))
    from backend.main import create_app

    with pytest.raises(LogInvalid, match="ledger.jsonl line 2"):
        create_app(settings)


def test_a_line_that_names_other_versions_than_the_first_stops_the_backend(tmp_path):
    folder = firm.unapproved(firm.copy(tmp_path / "playbooks"))
    first = json.loads((folder / "ledger.jsonl").read_text())
    data = {
        "founding": {"line": 1, "hash": first["hash"]},
        "versions": first["data"]["versions"][:1],
        "desks_sha256": first["data"]["desks_sha256"],
    }
    from backend.chainlog import Position

    line = following(Position(1, first["hash"]), EVENT, "playbook_owner", firm.AT, data)
    with (folder / "ledger.jsonl").open("ab") as ledger:
        ledger.write(line.written())

    with pytest.raises(LogInvalid, match="ledger.jsonl line 2 cannot be applied"):
        with firm.opened(folder, tmp_path):
            pass


@pytest.mark.parametrize("point", POINTS)
def test_an_approval_killed_at_each_point_happened_or_left_no_trace(tmp_path, point):
    folder = tmp_path / "killed"
    with running_layer(folder, playbooks=firm.unapproved(firm.copy(folder / "playbooks"))):
        pass
    records, before = firm.records(folder / "playbooks.sqlite"), logged(folder)

    kill_during(folder, EVENT, point, (PATH, OWNER_HEADERS, {"sha256": FIRST["hash"]}))

    assert firm.records(folder / "playbooks.sqlite") == records
    happened = point in ("all but the line end", "after the line")
    started(folder)
    assert logged(folder) == before + ([EVENT] if happened else [])
    after = firm.records(folder / "playbooks.sqlite")
    approved = {row[0] for row in after["playbook_versions"] if row[-1] == "playbook_owner"}
    assert approved == ({"feed-outage", "missing-cross-check"} if happened else set())
    (folder / "playbooks.sqlite").unlink()
    started(folder)
    assert firm.records(folder / "playbooks.sqlite") == after


# The command


@pytest.fixture
def command(monkeypatch, tmp_path, capsys):
    import io

    from cli import main

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


def test_the_command_shows_each_file_and_memo_whole_then_asks_for_the_hash(layer, command):
    from cli import main

    status, said, _ = command(layer, "approve-founding", typed=f"{FIRST['hash']}\n")

    assert status == 0, said
    asked = said.index(main.FOUNDING_CONFIRM)
    for name in ("feed-outage-founding", "missing-cross-check-founding"):
        words = (ROOT / "sources" / "memos" / f"{name}.md").read_text().split("---\n", 2)[2]
        block = "\n".join(f"    | {line}".rstrip() for line in words.strip().split("\n"))
        assert -1 < said.index(block) < asked
    for playbook in ("feed-outage", "missing-cross-check"):
        text = (ROOT / "playbooks" / playbook / "1.yaml").read_text().rstrip("\n")
        block = "\n".join(f"  | {line}".rstrip() for line in text.split("\n"))
        assert -1 < said.index(block) < asked
    assert said.index("The desks, hash") < asked
    assert "Approved the founding versions, ledger line 2." in said
    assert knowledge.events(layer)[-1] == EVENT


@pytest.mark.parametrize("typed", ["", "\n", "yes\n", "0" * 64 + "\n"])
def test_the_command_without_the_hash_approves_nothing(layer, command, typed):
    status, _, error = command(layer, "approve-founding", typed=typed)

    assert status == 1 and error.startswith("That is not the founding line's hash")
    assert EVENT not in knowledge.events(layer)


def test_the_command_as_the_analyst_is_refused(layer, command):
    status, _, error = command(
        layer, "approve-founding", "--as", "analyst", typed=f"{FIRST['hash']}\n"
    )

    assert status == 1 and error.startswith("403: ")
    assert EVENT not in knowledge.events(layer)


@pytest.mark.parametrize("who, twice", [("analyst", False), ("playbook_owner", True)])
def test_a_line_by_another_or_a_second_approval_stops_the_backend(tmp_path, who, twice):
    folder = firm.unapproved(firm.copy(tmp_path / "playbooks"))
    first = json.loads((folder / "ledger.jsonl").read_text())
    data = {
        "founding": {"line": 1, "hash": first["hash"]},
        "versions": first["data"]["versions"],
        "desks_sha256": first["data"]["desks_sha256"],
    }
    from backend.chainlog import Position

    before = Position(1, first["hash"])
    written = []
    for _ in range(2 if twice else 1):
        line = following(before, EVENT, who, firm.AT, data)
        written.append(line.written())
        before = line.position()
    with (folder / "ledger.jsonl").open("ab") as ledger:
        ledger.write(b"".join(written))

    failing = 3 if twice else 2
    with pytest.raises(LogInvalid, match=f"ledger.jsonl line {failing} cannot be applied"):
        with firm.opened(folder, tmp_path):
            pass
