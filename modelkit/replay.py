"""A chat model that gives recorded replies, in order for each request, and refuses any other."""

import hashlib
import json
import threading
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, PrivateAttr


class RecordingMismatch(Exception):
    """The recording no longer matches what is asked."""


class RecordedCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    args: dict[str, Any]


class RecordedInvalidCall(BaseModel):
    """A call whose arguments a provider could not read as JSON, as it gave them."""

    model_config = ConfigDict(extra="forbid")

    name: str | None
    args: str | None


class RecordedReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: str
    text: str
    tool_calls: list[RecordedCall]
    invalid_tool_calls: list[RecordedInvalidCall] = []


class Recording(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `scripted`: written for the repository. `recorded`: captured from a model.
    origin: Literal["scripted", "recorded"]
    about: str
    replies: list[RecordedReply]

    @classmethod
    def read(cls, path: Path) -> "Recording":
        return cls.model_validate_json(path.read_bytes())


def canonical(messages: Sequence[BaseMessage], tools: Sequence[dict]) -> str:
    """A request as it is hashed: what was said and offered, without the ids of calls."""
    said = []
    for message in messages:
        one: dict[str, Any] = {"role": message.type, "text": message.text}
        if isinstance(message, AIMessage):
            one["calls"] = [{"name": c["name"], "args": c["args"]} for c in message.tool_calls]
            # A call a provider could not read differs by its text: it is in the hash only
            # where there is one.
            if message.invalid_tool_calls:
                one["invalid"] = [
                    {"name": c["name"], "args": c["args"]} for c in message.invalid_tool_calls
                ]
        said.append(one)
    return json.dumps(
        {"messages": said, "tools": list(tools)}, sort_keys=True, separators=(",", ":")
    )


def request_hash(messages: Sequence[BaseMessage], tools: Sequence[dict]) -> str:
    return hashlib.sha256(canonical(messages, tools).encode()).hexdigest()


class ReplayModel(BaseChatModel):
    """Each request's replies are given in the order they were recorded, for as long as this
    one model is used: an agent keeps it for all its asks, as three trials of one case send
    one request and a live model gave each trial a reply of its own."""

    recording: Recording
    _given: Counter[str] = PrivateAttr(default_factory=Counter)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    @property
    def _llm_type(self) -> str:
        return "replay"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable | BaseTool],
        *,
        tool_choice: str | None = None,
        **_: Any,
    ):
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools])

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        request = request_hash(messages, kwargs.get("tools", []))
        replies = [each for each in self.recording.replies if each.request == request]
        with self._lock:
            if self._given[request] >= len(replies):
                raise RecordingMismatch("The recording no longer matches what is asked.")
            reply = replies[self._given[request]]
            self._given[request] += 1
        calls = [
            {"name": call.name, "args": call.args, "id": f"replay-{request[:12]}-{number}"}
            for number, call in enumerate(reply.tool_calls)
        ]
        invalid = [
            {
                "type": "invalid_tool_call",
                "name": call.name,
                "args": call.args,
                "id": f"replay-{request[:12]}-invalid-{number}",
                "error": None,
            }
            for number, call in enumerate(reply.invalid_tool_calls)
        ]
        message = AIMessage(content=reply.text, tool_calls=calls, invalid_tool_calls=invalid)
        return ChatResult(generations=[ChatGeneration(message=message)])
