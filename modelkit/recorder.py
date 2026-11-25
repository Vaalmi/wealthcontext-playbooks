"""A chat model that asks another and writes each reply under the hash of its request.

The file it writes is a recording in the replay model's own format, so that a cycle run on
a live model can be replayed with no key. The hash is the replay's: the same request
replayed finds the reply the live model gave.
"""

import os
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict

from modelkit.replay import (
    RecordedCall,
    RecordedInvalidCall,
    RecordedReply,
    Recording,
    request_hash,
)
from modelkit.replies import whole


class Keeper:
    """One recording file, written whole after each reply, so that a run stopped at any point
    leaves the replies given until then."""

    def __init__(self, path: Path, origin: Literal["scripted", "recorded"], about: str):
        self.path = path
        self._lock = threading.Lock()
        self._recording = Recording(origin=origin, about=about, replies=[])

    def add(self, reply: RecordedReply) -> None:
        with self._lock:
            self._recording.replies.append(reply)
            writing = self.path.with_name(self.path.name + ".writing")
            writing.write_text(self._recording.model_dump_json(indent=2) + "\n", "utf-8")
            os.replace(writing, self.path)

    @property
    def replies(self) -> list[RecordedReply]:
        with self._lock:
            return list(self._recording.replies)


class RecordingModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # The model asked: a chat model, or one already given its tools.
    inner: Any
    keeper: Keeper

    @property
    def _llm_type(self) -> str:
        return "recording"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ):
        chosen = {} if tool_choice is None else {"tool_choice": tool_choice}
        bound = self.inner.bind_tools(tools, **chosen, **kwargs)
        # Offered to the hash as the replay model takes them.
        offered = [convert_to_openai_tool(tool) for tool in tools]
        return RecordingModel(inner=bound, keeper=self.keeper).bind(tools=offered)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        reply = self.inner.invoke(messages)
        assert isinstance(reply, AIMessage)
        # Cut off at its limit: kept, and given, as the agent may use it, with no call.
        reply = whole(reply)
        kept = RecordedReply(
            request=request_hash(messages, kwargs.get("tools", [])),
            text=reply.text,
            tool_calls=[RecordedCall(name=c["name"], args=c["args"]) for c in reply.tool_calls],
            invalid_tool_calls=[
                RecordedInvalidCall(name=c["name"], args=c["args"])
                for c in reply.invalid_tool_calls
            ],
        )
        self.keeper.add(kept)
        # The agent is given the reply as its replay will give it, with what the provider
        # reported of its use: so the live run asks next what the replay will ask.
        given = AIMessage(
            content=kept.text,
            tool_calls=reply.tool_calls,
            invalid_tool_calls=reply.invalid_tool_calls,
            usage_metadata=reply.usage_metadata,
            response_metadata=reply.response_metadata,
            id=reply.id,
        )
        return ChatResult(generations=[ChatGeneration(message=given)])
