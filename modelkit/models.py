"""Builds the chat model from a `provider:model` setting, and reads a reply's tokens."""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx2
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from modelkit.prices import PRICES, Price
from modelkit.recorder import Keeper, RecordingModel
from modelkit.replay import Recording, ReplayModel, canonical

MAX_OUTPUT_TOKENS = 4096
RETRIES = 2
TIMEOUT_SECONDS = 60
# A reasoning model's thinking counts against its output limit and is billed as output: the
# agent's own limit is raised by what OpenAI recommends reserving for reasoning, so that a reply
# at high effort is not cut off before its call, and the bound on a call still holds.
REASONING_TOKENS = 25_000
# A reasoning model thinks before it answers: a call is given longer before it is given up,
# and tried once more at most (only after an error the provider answered with: see spending), so
# that with both tries it still ends inside the five minutes after which a run, a review or a
# trial reads stalled.
REASONING_TIMEOUT_SECONDS = 120
REASONING_RETRIES = 1
OPENAI_URL = "https://api.openai.com/v1"
# A provider adds its own instructions when tools are offered.
PROVIDER_OVERHEAD_TOKENS = 1024
KEY_OF = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
# Where a live model's replies are written as a recording, when it is set.
RECORD_TO = "RECORD_REPLIES_TO"


class Unavailable(Exception):
    """No run can start on the model the setting names."""

    def __init__(self, reason: Literal["no_model", "no_price", "no_effort", "stray_setting"]):
        super().__init__(reason)
        self.reason: Literal["no_model", "no_price", "no_effort", "stray_setting"] = reason


@dataclass(frozen=True)
class Configured:
    name: str
    model: BaseChatModel
    # None for a model that costs nothing: a replay, or a stand-in in a test.
    price: Price | None
    max_output_tokens: int = MAX_OUTPUT_TOKENS
    # One model for every ask of an agent: a replay gives each request's replies in order
    # across asks, and a recording is one file.
    lasting: bool = False
    # The tries a call may take, each counted as it is made (see spending): the provider's
    # client makes none of its own.
    attempts: int = 1 + RETRIES
    # A call is required, as OpenAI's models take it. Current Claude models refuse a forced
    # tool choice, so for them it stays `auto` and the instructions ask for the call.
    forced: bool = False

    def offering(self, tools: Sequence[BaseTool]) -> Any:
        # One call a reply, with a strict schema.
        chosen = {"tool_choice": "required"} if self.forced else {}
        return self.model.bind_tools(list(tools), strict=True, parallel_tool_calls=False, **chosen)

    def most_it_could_cost(
        self, messages: Sequence[BaseMessage], tools: Sequence[BaseTool]
    ) -> Decimal:
        """An upper bound on one call: a byte is at most a token, and every try is counted."""
        if self.price is None:
            return Decimal(0)
        asked = canonical(messages, [convert_to_openai_tool(tool) for tool in tools]).encode()
        # A reply with thinking is sent back whole, which the canonical form leaves out.
        earlier = sum(len(str(m.content).encode()) for m in messages if isinstance(m, AIMessage))
        taken = len(asked) + earlier + PROVIDER_OVERHEAD_TOKENS
        return self.price.dollars(taken, self.max_output_tokens) * self.attempts


def configured(
    setting: str,
    environment: Mapping[str, str],
    *,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
    timeout: float = TIMEOUT_SECONDS,
) -> Configured:
    provider, _, named = setting.strip().partition(":")
    if not named:
        raise Unavailable("no_model")
    if provider == "replay":
        path = Path(named)
        try:
            recording = Recording.read(path)
        except (OSError, ValueError):
            raise Unavailable("no_model") from None
        return Configured(
            name=f"replay:{path.stem} ({recording.origin})",
            model=ReplayModel(recording=recording),
            price=None,
            lasting=True,
        )
    key_name = KEY_OF.get(provider)
    key = environment.get(key_name, "") if key_name else ""
    # The key is taken from the setting only. The provider's SDK would otherwise look for
    # credentials elsewhere on the machine.
    if not key:
        raise Unavailable("no_model")
    model_id, effort = _split(named)
    price = priced(f"{provider}:{model_id}", effort)
    retries = RETRIES
    if price.reasoning:
        max_output_tokens += REASONING_TOKENS
        timeout, retries = max(timeout, REASONING_TIMEOUT_SECONDS), REASONING_RETRIES
    options: dict[str, Any] = {}
    if provider == "openai":
        # The Responses API, which OpenAI's reasoning models take tools on; nothing stored at
        # OpenAI; its address set here, so that no variable of the machine's can move it. No
        # effort named: none is sent, and the model's own applies.
        stray = sorted(name for name in os.environ if _read_by_openais_client(name))
        if stray:
            raise Unavailable("stray_setting")
        options = {
            "use_responses_api": True,
            "store": False,
            "base_url": OPENAI_URL,
            # Standard processing, which the prices are for, whatever the project's default.
            "service_tier": "default",
            # No proxy, certificate or netrc of the machine's: the client goes to OPENAI_URL.
            "http_client": httpx2.Client(trust_env=False),
            **({"reasoning": {"effort": effort}} if effort else {}),
        }
    model = init_chat_model(
        f"{provider}:{model_id}",
        api_key=key,
        max_tokens=max_output_tokens,
        # The tries are spending's, each counted: the client makes none of its own.
        max_retries=0,
        timeout=timeout,
        **options,
    )
    # The name is the setting whole, its effort with it: what each run records of its model.
    setting = f"{provider}:{named}"
    forced = provider == "openai"
    if recorded := environment.get(RECORD_TO, ""):
        about = (
            f"Captured from {setting} on {datetime.now(UTC):%Y-%m-%d}: each reply that model "
            "gave, in order, under the hash of the request it answered."
        )
        keeper = Keeper(Path(recorded), "recorded", about)
        recording_model = RecordingModel(inner=model, keeper=keeper)
        return Configured(
            setting,
            recording_model,
            price,
            max_output_tokens,
            lasting=True,
            attempts=1 + retries,
            forced=forced,
        )
    return Configured(setting, model, price, max_output_tokens, attempts=1 + retries, forced=forced)


def _read_by_openais_client(name: str) -> bool:
    """A variable OpenAI's client or langchain's would read in place of what is set here."""
    return (name.startswith("OPENAI_") and name != "OPENAI_API_KEY") or name.startswith(
        "LANGCHAIN_OPENAI_"
    )


def priced(model: str, effort: str | None) -> Price:
    """The price of a `provider:model`, refused when there is none or when it does not take the
    effort named."""
    price = PRICES.get(model)
    if price is None:
        raise Unavailable("no_price")
    if effort is not None and effort not in price.efforts:
        raise Unavailable("no_effort")
    return price


def check(setting: str) -> None:
    """Refuse, as building the model would, a live setting that names no priced model or an
    effort its model does not take. The key is not looked at."""
    provider, _, named = setting.strip().partition(":")
    model_id, effort = _split(named)
    if not model_id or provider not in KEY_OF:
        raise Unavailable("no_model")
    priced(f"{provider}:{model_id}", effort)


def _split(named: str) -> tuple[str, str | None]:
    """A setting's model and its effort, the part after the model's name, if one is named."""
    model_id, separated, effort = named.partition(":")
    if separated and not effort:
        raise Unavailable("no_effort")
    return model_id, effort or None


def tokens(reply: AIMessage) -> tuple[int | None, int | None]:
    reported = reply.usage_metadata
    if not reported:
        return None, None
    return reported["input_tokens"], reported["output_tokens"]
