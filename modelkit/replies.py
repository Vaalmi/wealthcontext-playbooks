"""A provider's reply as an agent may use it: one cut off at its output limit gives no call."""

from langchain_core.messages import AIMessage


def cut_off(reply: AIMessage) -> bool:
    """Whether the reply ended at its output limit, as OpenAI's and Anthropic's say so."""
    said = reply.response_metadata
    return (
        said.get("status") == "incomplete"
        or said.get("finish_reason") == "length"
        or said.get("stop_reason") == "max_tokens"
    )


def whole(reply: AIMessage) -> AIMessage:
    """The reply, or, where it was cut off, the same with no call: a call whose arguments were
    still being written when the limit came is never used, whether or not they happen to parse.
    What the provider reported of its use is kept, so that it is charged."""
    if not cut_off(reply):
        return reply
    return AIMessage(
        content=reply.text,
        tool_calls=[],
        invalid_tool_calls=[],
        usage_metadata=reply.usage_metadata,
        response_metadata=reply.response_metadata,
        id=reply.id,
    )
