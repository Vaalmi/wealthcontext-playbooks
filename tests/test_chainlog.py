"""The chained log: a change to any byte is seen, and a line a crash cut short is not a change."""

import pytest

from backend import chainlog
from backend.chainlog import START, ChainLog, LogInvalid, following

AT = "2026-11-22T07:00:00.000000Z"
MOVES = [{"subjects": ["WC-AC-0002", "WC-AC-0004"]}, {"reason": "it was wrong"}, {"cost": None}]


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "moves.jsonl"
    log = ChainLog("moves", path, create=True)
    log.open()
    log.repair()
    for data in MOVES:
        log.write(following(log.tail, "moved", "someone", AT, data))
    log.close()
    return path


def opened(path) -> list[chainlog.Line]:
    log = ChainLog("moves", path)
    try:
        found = log.open()
        log.repair()
        return found
    finally:
        log.close()


def test_what_was_written_is_read_back(path):
    found = opened(path)

    assert [line.data for line in found] == MOVES
    assert [line.line for line in found] == [1, 2, 3]
    assert [line.prev for line in found] == [START.hash, found[0].hash, found[1].hash]


@pytest.mark.parametrize("change", ["a low bit", "the case", "a line end", "a space"])
def test_a_change_to_any_byte_is_refused_naming_its_line(path, change):
    content = path.read_bytes()
    for position, was in enumerate(content):
        now = {"a low bit": was ^ 1, "the case": was ^ 32, "a line end": 10, "a space": 32}[change]
        if now == was:
            continue
        changed = content[:position] + bytes([now]) + content[position + 1 :]
        path.write_bytes(changed)
        number = content.count(b"\n", 0, position) + 1

        with pytest.raises(LogInvalid, match=f"^moves.jsonl line {number} "):
            opened(path)

        assert path.read_bytes() == changed, "a changed log was mended"


def test_a_line_cut_short_at_any_byte_leaves_no_trace(path):
    content = path.read_bytes()
    kept = content[: content.rindex(b"\n", 0, len(content) - 1) + 1]
    last = content[len(kept) : -1]
    for length in range(1, len(last)):
        path.write_bytes(kept + last[:length])

        assert [line.line for line in opened(path)] == [1, 2], length

        assert path.read_bytes() == kept


def test_a_whole_line_with_no_line_end_happened(path):
    content = path.read_bytes()
    path.write_bytes(content[:-1])

    assert len(opened(path)) == 3
    assert path.read_bytes() == content


@pytest.mark.parametrize("removed", [0, 1])
def test_a_line_taken_out_is_refused(path, removed):
    lines = path.read_bytes().splitlines(keepends=True)
    del lines[removed]
    path.write_bytes(b"".join(lines))

    with pytest.raises(LogInvalid, match=f"line {removed + 1} does not follow"):
        opened(path)


def test_lines_in_another_order_are_refused(path):
    one, two, three = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(one + three + two)

    with pytest.raises(LogInvalid, match="line 2 does not follow"):
        opened(path)


def test_one_backend_holds_a_log_at_a_time(path):
    log = ChainLog("moves", path)
    log.open()

    with pytest.raises(LogInvalid, match="held by another backend"):
        ChainLog("moves", path).open()
    log.close()


@pytest.mark.parametrize("fails", ["write", "fsync"])
def test_a_line_that_could_not_be_flushed_is_taken_back(path, monkeypatch, fails):
    log = ChainLog("moves", path)
    log.open()
    log.repair()
    before, tail = path.read_bytes(), log.tail
    real = getattr(chainlog.os, fails)

    def broken(descriptor, *content):
        if content:
            real(descriptor, content[0][: len(content[0]) // 2])
        monkeypatch.setattr(chainlog.os, fails, real)
        raise OSError("the disk is full")

    monkeypatch.setattr(chainlog.os, fails, broken)
    with pytest.raises(OSError, match="full"):
        log.write(following(log.tail, "moved", "someone", AT, {}))

    assert (path.read_bytes(), log.tail) == (before, tail)
    log.write(following(log.tail, "moved", "someone", AT, {}))
    log.close()
    assert len(opened(path)) == 4


def test_nothing_is_added_after_a_line_that_may_be_there(path, monkeypatch):
    log = ChainLog("moves", path)
    log.open()
    log.repair()

    def broken(*_):
        raise OSError("the disk is gone")

    monkeypatch.setattr(chainlog.os, "fsync", broken)
    monkeypatch.setattr(chainlog.os, "ftruncate", broken)
    with pytest.raises(OSError):
        log.write(following(log.tail, "moved", "someone", AT, {}))
    monkeypatch.undo()

    with pytest.raises(LogInvalid, match="start the backend again"):
        log.write(following(log.tail, "moved", "someone", AT, {}))
    log.close()
    # The whole line is there, so it happened.
    assert len(opened(path)) == 4


def test_nothing_is_added_to_a_log_changed_under_the_backend(path):
    log = ChainLog("moves", path)
    log.open()
    log.repair()
    with path.open("ab") as elsewhere:
        elsewhere.write(b"something else\n")

    with pytest.raises(LogInvalid, match="changed while the backend ran"):
        log.write(following(log.tail, "moved", "someone", AT, {}))
    log.close()
