"""Prices per token, used to hold a live run's dollar cap, and the efforts each model takes."""

from dataclasses import dataclass, field
from decimal import Decimal

# Anthropic's: from https://platform.claude.com/docs/en/about-claude/pricing, standard rates,
# checked on the date below. Input is priced whole at the base rate: no request here uses a cache.
CHECKED = "2026-11-21"
# OpenAI's: from https://developers.openai.com/api/docs/pricing, Standard, short context, and
# the efforts each takes from https://developers.openai.com/api/docs/models, checked on the date
# below. Input is priced whole: cached input is cheaper, so this only ever counts more.
OPENAI_CHECKED = "2026-11-23"
_PER = Decimal(1_000_000)


@dataclass(frozen=True)
class Price:
    input: Decimal  # dollars for a million tokens
    output: Decimal
    # The reasoning efforts the model takes. None: it takes none, and a setting naming one is
    # refused.
    efforts: frozenset[str] = field(default_factory=frozenset)
    # A reasoning model's thinking is billed as output and counts against its output limit.
    reasoning: bool = False

    def dollars(self, input_tokens: int, output_tokens: int) -> Decimal:
        return (self.input * input_tokens + self.output * output_tokens) / _PER


_SOL_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_EFFORTS = _SOL_EFFORTS | {"none"}


PRICES = {
    "anthropic:claude-opus-5-5": Price(Decimal(4), Decimal(20)),
    "anthropic:claude-sonnet-5-5": Price(Decimal(2), Decimal(10)),
    # For prompts of up to 100,000 tokens, which every request here is far below.
    "anthropic:claude-haiku-5-5": Price(Decimal("0.10"), Decimal("0.50")),
    # For prompts of up to OpenAI's short context, which every request here is far below.
    "openai:gpt-6.2-sol": Price(Decimal(2), Decimal(10), _SOL_EFFORTS, reasoning=True),
    "openai:gpt-6.1-terra": Price(Decimal(1), Decimal(5), _EFFORTS, reasoning=True),
    "openai:gpt-6-luna": Price(Decimal("0.10"), Decimal("0.50"), _EFFORTS, reasoning=True),
}
