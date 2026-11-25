"""No check calls a model or reaches anything beyond this machine's loopback."""

import os

NEVER_SET = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
    "TRIAGE_MODEL",
    "CURATOR_MODEL",
)
# Before anything imports LangChain, which reads some of these when it loads.
for name in NEVER_SET:
    os.environ.pop(name, None)
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

import socket  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402

import pytest  # noqa: E402
from layer import Layer, running_layer  # noqa: E402

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_connect = socket.socket.connect


def _loopback_only(self: socket.socket, address) -> None:
    if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in LOOPBACK:
        raise AssertionError("a check reached beyond loopback")
    _connect(self, address)


socket.socket.connect = _loopback_only  # type: ignore[method-assign]


@pytest.fixture
def make_layer(tmp_path) -> Iterator[Callable[..., Layer]]:
    """Start a layer: the real backend and agent, on a stand-in WealthContext.

    `after` stops that layer and starts this one on its folder: the backend started again.
    """
    started = []

    def make(*, after: Layer | None = None, **options) -> Layer:
        folder = tmp_path / str(len(started))
        if after is not None:
            folder = after.settings.database.parent
            for each in started:
                if each[1] is after:
                    each[0].__exit__(None, None, None)
                    each[0] = None
        manager = running_layer(folder, **options)
        layer = manager.__enter__()
        started.append([manager, layer])
        return layer

    yield make
    for manager, _ in reversed(started):
        if manager is not None:
            manager.__exit__(None, None, None)
