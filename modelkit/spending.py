"""One call to a model within a cap on dollars, and what the call used."""

import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import cast

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool

from modelkit.models import Configured, tokens
from modelkit.replay import RecordingMismatch
from modelkit.replies import whole

# Between tries of a call the provider answered with an error it bills nothing for.
BACKOFF_SECONDS = 1.0


def _again(status: object) -> bool:
    """Whether a provider's answer of this status is one to try again: it was busy or failed."""
    return isinstance(status, int) and (status in (408, 409, 429) or status >= 500)


class CostCapReached(Exception):
    """The next call could take the work past its cap."""


class ModelFailed(Exception):
    """The model could not be called, or gave no answer. What the call could have cost is
    charged all the same, as a lost answer may have been billed: `charged`."""

    def __init__(self, what: str, charged: Decimal = Decimal(0)):
        super().__init__(what)
        self.charged = charged


@dataclass
class Budget:
    cap: Decimal
    spent: Decimal = Decimal(0)

    def allow(self, most: Decimal) -> None:
        if most and self.spent + most > self.cap:
            raise CostCapReached


@dataclass(frozen=True)
class Spent:
    reply: AIMessage
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: str | None


def ask(
    configured: Configured,
    budget: Budget,
    messages: Sequence[BaseMessage],
    offered: Sequence[BaseTool],
) -> Spent:
    """Call the model, unless the call could pass the cap, and count what it took.

    The tries are made here, not by the provider's client, so that each is counted: a try the
    provider answered with an error it bills nothing for (it was busy, or failed on its side) is
    tried again, up to the model's tries; a try that brought no answer back may have been billed,
    so it is charged its bound and not tried again, and the call fails."""
    most = configured.most_it_could_cost(messages, offered)
    budget.allow(most)
    reply = _answered(configured, budget, messages, offered, most / configured.attempts)
    taken, given = tokens(reply)
    cost = None
    if configured.price is not None:
        if taken is None or given is None:
            # Nothing reported, so the most it could have been: recorded so, a cap that sums
            # recorded costs (a run started again, a gate's trials) counts it too.
            budget.spent += most
            cost = recorded(most)
        else:
            dollars = configured.price.dollars(taken, given)
            budget.spent += dollars
            cost = recorded(dollars)
    return Spent(reply, taken, given, cost)


def recorded(dollars: Decimal) -> str:
    """A cost as a run records it, to the millionth of a dollar, rounded up: a cap summed from
    recorded costs never counts less than was charged."""
    return f"{dollars.quantize(Decimal('0.000001'), rounding=ROUND_CEILING)}"


def _answered(
    configured: Configured,
    budget: Budget,
    messages: Sequence[BaseMessage],
    offered: Sequence[BaseTool],
    each: Decimal,
) -> AIMessage:
    """The reply, tried as `ask` says, or the call's failure with what it was charged."""
    tried = 0
    while True:
        try:
            return whole(cast(AIMessage, configured.offering(offered).invoke(list(messages))))
        except RecordingMismatch:
            raise
        except Exception as error:
            tried += 1
            answered = getattr(error, "status_code", None)
            if _again(answered) and tried < configured.attempts:
                time.sleep(BACKOFF_SECONDS * 2 ** (tried - 1))
                continue
            # No answer, or one that could not be read, may still have been billed: its bound is
            # charged. An error the provider answered with (a status of 400 or more) bills nothing.
            failed = isinstance(answered, int) and answered >= 400
            lost = each if configured.price is not None and not failed else Decimal(0)
            budget.spent += lost
            # Its kind alone, and not chained: a provider's error can carry the key it was sent.
            raise ModelFailed(type(error).__name__, lost) from None
