"""A stub chat model, so that no check calls a real model, with helpers to script its replies.

It keeps every prompt it was sent and every set of tools it was offered, those of a choice
of step apart from those of whether to file a gap report. The second question has an answer
of its own, no gap report, so that a test scripts only what it is about.
"""

import inspect
import itertools
import json
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool

# What a provider reports for each reply, so usage and cost code is exercised.
USAGE = {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500}
NO_TOKENS = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
CALL_IDS = itertools.count()


class StubModel(GenericFakeChatModel):
    """A chat model that gives scripted replies, or raises a scripted error."""

    prompts: list[list] = []
    offered: list[list] = []
    gap_prompts: list[list] = []
    gap_offered: list[list] = []
    gap_answer: Any = None
    about_a_gap: bool = False

    def bind_tools(self, tools, **kwargs):
        made = [convert_to_openai_tool(tool) for tool in tools]
        self.about_a_gap = any(tool["function"]["name"] == "file_gap_report" for tool in made)
        (self.gap_offered if self.about_a_gap else self.offered).append(made)
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if not self.about_a_gap:
            self.prompts.append(list(messages))
            return super()._generate(messages, stop, run_manager, **kwargs)
        self.gap_prompts.append(list(messages))
        if self.gap_answer is None:
            reply = call("no_gap_report")
            reply.usage_metadata = NO_TOKENS  # type: ignore[assignment]
        else:
            reply = self.gap_answer(list(messages))
            if isinstance(reply, BaseException):
                raise reply
        return ChatResult(generations=[ChatGeneration(message=reply)])


def stub(*replies, gaps=None) -> StubModel:
    """A stub model giving these replies in turn.

    An exception among them is raised. A function is called for its reply when it is wanted.
    """

    def given():
        for reply in replies:
            if inspect.isfunction(reply):
                reply = reply()
            if isinstance(reply, BaseException):
                raise reply
            yield reply

    return StubModel(
        messages=given(),
        prompts=[],
        offered=[],
        gap_prompts=[],
        gap_offered=[],
        gap_answer=gaps,
    )


def says(text: str) -> AIMessage:
    """A reply in words, with no tool call."""
    return AIMessage(content=text, usage_metadata=USAGE)  # type: ignore[arg-type]


def call(name: str, **args) -> AIMessage:
    """A reply that calls one tool, with an id of its own as a real model gives."""
    tool_call = {"name": name, "args": args, "id": f"call-{next(CALL_IDS)}"}
    return AIMessage(content="", tool_calls=[tool_call], usage_metadata=USAGE)  # type: ignore[arg-type]


def calls(*named: tuple[str, dict]) -> AIMessage:
    """A reply that calls several tools at once."""
    made = [{"name": name, "args": args, "id": f"call-{next(CALL_IDS)}"} for name, args in named]
    return AIMessage(content="", tool_calls=made, usage_metadata=USAGE)  # type: ignore[arg-type]


def fitting_choice(request: dict) -> AIMessage:
    """What a right model answers: the one step whose conditions the exception meets."""
    facts = request["exception"]["feed_facts"]
    for step in request["steps"]:
        conditions = step["conditions"].items()
        if all(v == "any" or (facts and facts[k] == v) for k, v in conditions):
            return call("choose_step", option=step["option"])
    return call("no_step_applies")


def wrong_choice(request: dict) -> AIMessage:
    """An answer the backend refuses: no step where one fits, or a step where none does."""
    right = fitting_choice(request)
    if right.tool_calls[0]["name"] == "choose_step":
        return call("no_step_applies")
    return call("choose_step", option=request["steps"][0]["option"])


class ChoosingModel(StubModel):
    """A stub that reads each request and answers it: `wrong` answers wrong, then right."""

    wrong: int = 0
    # A provider that reports no usage, as some do.
    unreported: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.about_a_gap:
            return super()._generate(messages, stop, run_manager, **kwargs)
        self.prompts.append(list(messages))
        request = json.loads(messages[1].content)
        if self.wrong > 0:
            self.wrong -= 1
            reply = wrong_choice(request)
        else:
            reply = fitting_choice(request)
        if self.unreported:
            reply.usage_metadata = None
        return ChatResult(generations=[ChatGeneration(message=reply)])


def choosing(wrong: int = 0, unreported: bool = False) -> ChoosingModel:
    return ChoosingModel(
        messages=iter([]),
        prompts=[],
        offered=[],
        gap_prompts=[],
        gap_offered=[],
        wrong=wrong,
        unreported=unreported,
    )
