"""A backend that kills itself at a chosen point of a durable move. Run as a process of its own.

    killed.py <point> <event> serve <socket>
    killed.py <point> <event> put_in_use

A point is before the line, part of it, all but its line end, or after the line and before
the commit. The process is killed with SIGKILL, so nothing is caught or tidied.
"""

import os
import signal
import socket
import sys
import threading

import firm
import uvicorn

from backend import chainlog, main, triage
from backend.settings import Settings

POINTS = ("before the line", "part of the line", "all but the line end", "after the line")


def killed() -> None:
    os.kill(os.getpid(), signal.SIGKILL)
    # The kill returns before the process dies, and this thread must not go on to commit.
    threading.Event().wait()


def kill_at(point: str, event: str) -> None:
    write = chainlog.ChainLog.write

    def writing(log: chainlog.ChainLog, line: chainlog.Line) -> None:
        if line.event != event:
            return write(log, line)
        content = line.written()
        descriptor = log._descriptor
        assert descriptor is not None
        if point == "part of the line":
            os.write(descriptor, content[: len(content) // 2])
        elif point == "all but the line end":
            os.write(descriptor, content[:-1])
        elif point == "after the line":
            write(log, line)
        killed()

    setattr(chainlog.ChainLog, "write", writing)  # noqa: B010 (pyright refuses an assignment)


def run(point: str, event: str, how: str, *given: str) -> None:
    assert point in POINTS
    kill_at(point, event)
    settings = Settings.from_environment()
    if how == "put_in_use":
        with firm.opened(settings.playbooks, settings.database.parent) as (journal, store):
            changed = {
                "feed-outage": firm.steps("feed-outage", by="within 2 hours"),
                "missing-cross-check": firm.steps("missing-cross-check", hold_report=False),
            }
            firm.put_in_use(journal, store, changed)
        return
    # Every run reads as stalled, so that starting again needs no wait.
    triage.STALLED_AFTER = -triage.STALLED_AFTER
    server = uvicorn.Server(uvicorn.Config(main.create_app(settings), log_level="error"))
    server.run(sockets=[socket.socket(fileno=int(given[0]))])


if __name__ == "__main__":
    run(*sys.argv[1:])
