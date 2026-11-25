"""A changed file or line is noticed: the backend refuses to start and says which."""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace

import firm
import pytest
from layer import CROSS_CHECK_DAY, ROOT, running_layer
from stand_ins import idle_agent

from backend import chainlog
from backend.chainlog import LogInvalid
from backend.main import create_app
from backend.store import StoreInvalid


@pytest.fixture
def left(tmp_path):
    """What a backend left when it stopped: two versions, a retirement and two runs' lines."""
    with running_layer(tmp_path / "left", agent=lambda *_: idle_agent()) as layer:
        layer.put_in_use({"feed-outage": firm.steps("feed-outage", by="within 2 hours")})
        layer.start()
        assert layer.retire("feed-outage", 2).status_code == 200
        layer.start(CROSS_CHECK_DAY)
    return layer.settings


def change_a_byte(path, position: int) -> None:
    content = bytearray(path.read_bytes())
    content[position] ^= 1
    path.write_bytes(content)


def each_line(path) -> list[int]:
    """A position in the middle of each line."""
    found, start = [], 0
    for line in path.read_bytes().splitlines(keepends=True):
        found.append(start + len(line) // 2)
        start += len(line)
    return found


def refuses(settings, error, said: str) -> None:
    with pytest.raises(error, match=said):
        create_app(settings)


@pytest.mark.parametrize("database", ["there", "deleted"])
@pytest.mark.parametrize(
    "file",
    ["desks.yaml", "feed-outage/1.yaml", "feed-outage/2.yaml", "missing-cross-check/1.yaml"],
)
def test_one_byte_of_a_version_file_and_the_backend_names_the_file(left, database, file):
    path = left.playbooks / file
    content = path.read_bytes()
    if database == "deleted":
        left.database.unlink()
    for position in (0, len(content) // 2, len(content) - 1):
        change_a_byte(path, position)

        refuses(left, StoreInvalid, f"^{file} has been changed")

        path.write_bytes(content)


@pytest.mark.parametrize("database", ["there", "deleted"])
@pytest.mark.parametrize("which", ["ledger", "triage log"])
def test_one_byte_of_any_line_and_the_backend_names_the_line(left, database, which):
    path = left.triage_log if which == "triage log" else left.playbooks / "ledger.jsonl"
    content = path.read_bytes()
    middles = each_line(path)
    assert len(middles) == (2 if which == "triage log" else 4)
    if database == "deleted":
        left.database.unlink()
    for number, position in enumerate(middles, start=1):
        change_a_byte(path, position)

        refuses(left, LogInvalid, f"^{path.name} line {number} ")

        path.write_bytes(content)


@pytest.mark.parametrize("which", ["ledger", "triage log"])
def test_lines_taken_off_the_end_are_missed(left, which):
    path = left.triage_log if which == "triage log" else left.playbooks / "ledger.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:-1]))
    before = firm.files(left.playbooks)

    refuses(left, LogInvalid, f"{path.name} line {len(lines)} is missing")

    assert firm.files(left.playbooks) == before, "a file was removed on a ledger that is short"


def test_a_triage_log_that_is_gone_is_missed(left):
    left.triage_log.unlink()

    refuses(left, LogInvalid, "triage-log.jsonl line 1 is missing")


def test_a_chain_written_again_is_not_the_one_the_database_has(left):
    again, before = b"", chainlog.START
    for number, each in enumerate(left.triage_log.read_bytes().splitlines()):
        old = chainlog.Line.model_validate_json(each)
        at = "2026-01-01T00:00:00.000000Z" if number == 0 else old.at
        new = chainlog.following(before, old.event, old.who, at, old.data)
        again, before = again + new.written(), new.position()
    left.triage_log.write_bytes(again)

    refuses(left, LogInvalid, "line 2 is not the one the database has")


@pytest.mark.parametrize("gone", ["ledger.jsonl", "feed-outage/2.yaml", "desks.yaml"])
def test_a_file_that_is_gone_is_named(left, gone):
    (left.playbooks / gone).unlink()

    refuses(left, (LogInvalid, StoreInvalid), gone)


def test_a_database_with_records_that_no_log_holds_is_refused(left):
    with closing(sqlite3.connect(left.database)) as connection, connection:
        connection.execute("DELETE FROM applied")

    refuses(left, LogInvalid, "records that are in no log")


@pytest.mark.parametrize("what", ["triage_log", "database"])
@pytest.mark.parametrize("how", ["in it", "below it", "through a link", "spelled another way"])
def test_neither_the_triage_log_nor_the_database_may_be_in_the_playbook_folder(left, what, how):
    folder, name = left.playbooks, "records.file"
    if how == "below it":
        path = folder / "kept" / name
    elif how == "through a link":
        linked = folder.parent / "elsewhere"
        linked.symlink_to(folder)
        path = linked / name
    elif how == "spelled another way":
        path = folder / ".." / folder.name / name
    else:
        path = folder / name

    refuses(replace(left, **{what: path}), StoreInvalid, "may not be in the playbook folder")

    assert not path.exists()


def test_the_triage_log_names_accounts_and_the_playbook_folder_none(left):
    assert b"WC-AC-0002" in left.triage_log.read_bytes()
    assert left.triage_log.stat().st_mode & 0o077 == 0
    for name, content in firm.files(left.playbooks).items():
        assert b"WC-AC-" not in content, name


@pytest.mark.parametrize(
    "changed, said",
    [
        ("feed-outage/1.yaml", "feed-outage/1.yaml has been changed"),
        ("ledger.jsonl", "ledger.jsonl line 1 "),
        ("triage log", "triage-log.jsonl line 1 "),
    ],
)
def test_the_backend_process_stops_and_says_which(left, changed, said):
    path = left.triage_log if changed == "triage log" else left.playbooks / changed
    change_a_byte(path, 30)
    environment = {
        "PATH": os.environ["PATH"],
        "PLAYBOOKS_DATABASE": str(left.database),
        "PLAYBOOKS_FOLDER": str(left.playbooks),
        "PLAYBOOKS_TRIAGE_LOG": str(left.triage_log),
        "WEALTHCONTEXT_TOKEN": left.wealthcontext_token,
    }
    command = [sys.executable, "-m", "uvicorn", "--factory", "backend.main:create_app"]

    done = subprocess.run(
        [*command, "--port", "0"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert done.returncode != 0
    assert said in done.stderr.strip().splitlines()[-1]
