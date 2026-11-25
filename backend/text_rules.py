"""The closed lists text is checked against, each named once."""

import re
import unicodedata

# The number words are the ones WealthContext's verifier lints, from "zero" to "dozen".
NUMBER_WORDS = frozenset(
    """
    zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen
    fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy
    eighty ninety hundred thousand million billion trillion half quarter double triple
    twice thrice dozen
    hundreds thousands millions billions trillions dozens halves quarters
    """.split()
)
CURRENCY_WORDS = frozenset(
    """
    currency currencies dollar dollars cent cents euro euros pound pounds sterling yen
    usd eur gbp cad jpy chf
    """.split()
)
PERCENTAGE_WORDS = frozenset("percent percents percentage percentages pct bps".split())


def figure_in(text: str) -> bool:
    """Whether a text carries a number, a currency or a percentage, in figures or in words."""
    if any(character.isnumeric() for character in text):
        return True
    if any(c in "%‰" or unicodedata.category(c) == "Sc" for c in text):
        return True
    # Compatibility forms, so that full-width letters are read as the words they spell.
    plain = unicodedata.normalize("NFKC", text).casefold()
    # An invisible combining mark inside a word would split it.
    plain = "".join(c for c in plain if not unicodedata.category(c).startswith("M"))
    words = set(re.findall(r"[^\W\d_]+", plain))
    return bool(words & (NUMBER_WORDS | CURRENCY_WORDS | PERCENTAGE_WORDS))


# The names and titles this repository's own adversarial files use. They are invented here.
# None is taken from WealthContext, whose names never reach this layer.
NAMES_IN_OUR_FILES = (
    "quillon vantree",
    "vantree",
    "vantree household",
    "vantree family trust",
    "orla pennyfeather",
    "pennyfeather",
    "brindlemoor charitable foundation",
    "brindlemoor",
)

_AGENT = r"(?:triage )?(?:ai|agents?|curator|assistant|llm|model|chatbot|bot)"
# Words addressed to an agent, matched as whole words on text with its punctuation taken out.
ADDRESSED_TO_AN_AGENT = (
    r"(?:ignore|disregard|forget|override|overrule|bypass)"
    r"(?: (?:all|any|the|your|these|those|of|every|previous|prior|earlier|above|preceding"
    r"|other|system))* (?:instructions?|rules?|prompts?|directions|guardrails|guidance|messages)",
    r"(?:instructions?|message|notice|note|attention|prompt) (?:to|for) "
    rf"(?:the |any |all )?{_AGENT}",
    rf"(?:dear|attention|hey|hello) (?:the )?{_AGENT}",
    rf"{_AGENT} (?:must|should|shall|please|will now|is to|are to|is instructed|needs? to)",
    r"to the (?:triage )?(?:ai|agents?|curator|assistant|llm|chatbot)",
    rf"you are (?:now|(?:an?|the) {_AGENT})",
    r"(?:system|developer) (?:prompt|message|instructions?)",
    r"(?:new|updated|revised) instructions",
    r"as (?:an?|the) (?:ai|agent|curator|assistant|language model)",
    r"approve (?:this|the|your|my) proposal",
    r"curator|llm|chatbot|language model",
)
# The same, where the marks themselves are what addresses an agent.
AGENT_MARKS = (
    r"<\|",
    r"\[/?inst\]",
    r"</?\s*(?:system|assistant|developer|user|instructions?)\s*>",
    r"<!--",
    r"^[^\n\w]*(?:system|assistant|developer|curator|agent)[^\n\w]*:",
    r"submit_proposal|file_gap_report|no_gap_report|choose_step|no_step_applies",
)

# The four instructions no playbook step may give, each as the words that would give it. They
# are matched as whole words on a text in lower case with its punctuation taken out, whether
# or not a "do not" stands before them: a step's line has no need of these words, and a
# negation is easy to lose. A rewording on no list gets by.
_LATER = r"(?: \w+){0,4}"
_WARNING = r"(?:warnings?|flags?|alerts?|exceptions?|notices?|caveats?|codes?)"
FORBIDDEN_INSTRUCTIONS: dict[str, tuple[str, ...]] = {
    "show a total on an incomplete household": (
        r"totals?|subtotals?|aggregates?|aggregated|sums?|summed|net worth|grand totals?",
        r"roll ?ups?|rolled up|(?:combined|overall|household) (?:values?|balances?|worth)",
    ),
    "carry an old price forward": (
        rf"(?:carr(?:y|ies|ied|ying)|roll(?:s|ed|ing)?|bring(?:s|ing)?|brought){_LATER} forward",
        r"(?:yesterday s|yesterdays|previous|prior|last|old|older|earlier|stale|cached|outdated"
        r"|out of date|day old)(?: \w+){0,2} (?:prices?|pricing|quotes?|valuations?|marks?)",
        r"(?:prices?|quotes?|valuations?|marks?)(?: \w+){0,4} (?:forward|from yesterday)",
    ),
    "leave out a warning": (
        r"(?:leave|leaves|leaving|left)(?: \w+){0,4} (?:out|off)",
        rf"(?:omit\w*|drop\w*|suppress\w*|hid(?:e|es|den|ing)?|skip\w*|remov\w*|delet\w*"
        rf"|mut(?:e|es|ed|ing)|silenc\w*|filter\w*|strip\w*|exclud\w*|waiv\w*|ignor\w*"
        rf"|dismiss\w*|clear\w*){_LATER} {_WARNING}",
        rf"without{_LATER} {_WARNING}|no {_WARNING}",
    ),
    "give personal data to operations": (
        r"personal (?:data|details|information|info)|contact (?:details|information|info)",
        r"(?:client|clients|holder|holders|owner|owners|person|people|household|households"
        r"|account|full|first|last|family|legal|maiden)(?: s)? names?",
        r"names? of(?: the)? (?:clients?|holders?|owners?|people|persons?|households?)",
        r"(?:home|postal|mailing|street|email|e mail|residential) address(?:es)?",
        r"e ?mails?|phones?|telephones?|mobile numbers?|date of birth|birth ?dates?|ssns?",
        r"social security|tax (?:ids?|numbers?)|passports?|account (?:numbers?|titles?)",
    ),
}


def forbidden_instruction(words: str) -> str | None:
    """Which forbidden instruction a text's plain words give, or nothing."""
    for instruction, patterns in FORBIDDEN_INSTRUCTIONS.items():
        if re.search(rf"\b(?:{'|'.join(patterns)})\b", words):
            return instruction
    return None
