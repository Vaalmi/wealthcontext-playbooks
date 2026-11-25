"""The fixed-rule checks on text: personal data, client identifiers and words to an agent.

Five parts: client identifier shapes; email and phone shapes; the names and titles this
repository's own files use; words addressed to an agent; and bounds on the text. They run
over a text, or over every string in a structure. They cannot catch a name they have
never seen, or an instruction in words that are on no list.
"""

import re
import unicodedata
from collections.abc import Iterator, Mapping
from datetime import date
from typing import Any, Literal

from backend import text_rules

Rule = Literal[
    "too_long", "not_plain_text", "client_identifier", "personal_data", "addressed_to_an_agent"
]
WHY: dict[Rule, str] = {
    "too_long": "is over its length",
    "not_plain_text": "is not plain, visible text",
    "client_identifier": "carries a client identifier",
    "personal_data": "carries personal data",
    "addressed_to_an_agent": "carries words addressed to an agent",
}
MEMO_LENGTH = 4000
MODEL_NAME_LENGTH = 120
REASON_LENGTH = 300
SUMMARY_LENGTH = 300

# Punctuation a person's editor puts in. Any other character that is not ASCII is refused,
# so that no look-alike letter and no hidden character can slip a word past a list.
_TYPOGRAPHIC = frozenset("‘’“”–—‐•")
_IDENTIFIERS = re.compile(
    r"(?<![a-z0-9])(?:"
    r"(?:wc|crm)[-_]\s*[a-z0-9]"
    # The same identifiers with their separators changed.
    r"|(?:wc|crm)[\W_]{0,3}(?:ac|hh|per|sec|pos|cash|iss|lot|tr|txn)[\W_]{0,3}[0-9]{2,}"
    r"|(?:ac|hh|per|pos|txn)[-_][0-9]{3,}"
    r"|(?:adv|team)[-_][0-9]"
    # A run's, a result's, a trace's, and this layer's own.
    r"|run[\W_]{0,2}(?=[0-9a-f]*[0-9])[0-9a-f]{6,}"
    r"|[rh]_[0-9a-f]{6,}"
    r"|(?=[0-9a-f]*[0-9])[0-9a-f]{16,}"
    r")"
)
_CONTACT = re.compile(
    r"[^\s@]@[^\s@]"
    r"|\+[0-9][0-9 ().-]{6,20}[0-9]"
    r"|\([0-9]{2,4}\)\s?[0-9]{3}[\s.-]?[0-9]{3,4}"
    r"|(?<![0-9])(?:[0-9]{3}[\s.-])?[0-9]{3}[\s.-][0-9]{4}(?![0-9])"
)
_DAY = re.compile(r"(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9])")
# Six digits or more, also when a single separator is between them.
_DIGITS = re.compile(r"[0-9](?:(?:\s?[-./_]\s?|\s|,)?[0-9]){5,}")
_NAMES = re.compile(rf"\b(?:{'|'.join(map(re.escape, text_rules.NAMES_IN_OUR_FILES))})\b")
_ADDRESSED = re.compile(rf"\b(?:{'|'.join(text_rules.ADDRESSED_TO_AN_AGENT)})\b")
_MARKS = re.compile("|".join(text_rules.AGENT_MARKS), re.MULTILINE)


def _without_accents(text: str) -> str:
    """Letters with their accents off. Any other mark stays, and so is not plain text: some,
    such as a variation selector, are invisible and can carry words a person does not see.
    """
    split = unicodedata.normalize("NFKD", text)
    return "".join(c for c in split if not "\u0300" <= c <= "\u036f" or c == "\u034f")


def _plain(text: str, multiline: bool) -> bool:
    for character in text:
        if character == "\n":
            if not multiline:
                return False
        elif character in _TYPOGRAPHIC:
            continue
        elif not (character.isascii() and character.isprintable()):
            return False
    return any(character.isalnum() for character in text)


def _without_days(text: str) -> str:
    def put(found: re.Match[str]) -> str:
        try:
            date.fromisoformat(found.group())
        except ValueError:
            return found.group()  # digits in a date's shape that are no date
        return " date "

    return _DAY.sub(put, text)


def _words(lowered: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


def _in_text(text: str, limit: int, multiline: bool, agent_words: bool) -> Rule | None:
    # First, so that nothing longer is ever matched against a pattern.
    if len(text) > limit:
        return "too_long"
    read = _without_accents(text)
    if not _plain(read, multiline):
        return "not_plain_text"
    lowered = read.casefold()
    dateless = _without_days(lowered)
    words = _words(lowered)
    if _IDENTIFIERS.search(lowered):
        return "client_identifier"
    if _CONTACT.search(dateless) or _NAMES.search(words):
        return "personal_data"
    if _DIGITS.search(dateless):
        return "client_identifier"
    if agent_words and _addressed(lowered, words):
        return "addressed_to_an_agent"
    return None


def _addressed(lowered: str, words: str) -> bool:
    return bool(_MARKS.search(lowered) or _ADDRESSED.search(words))


def addressed_to_an_agent(text: str) -> bool:
    """Whether a text carries words addressed to an agent, read as `broken_rule` reads them."""
    lowered = _without_accents(text).casefold()
    return _addressed(lowered, _words(lowered))


def plain_words(text: str) -> str:
    """A text's words in lower case, with its accents and punctuation taken out."""
    return _words(_without_accents(text).casefold())


def written(value: Any) -> Iterator[str]:
    """Everything in a structure a model may have written, as text: each string, each key, and
    each number, which an identifier's rule reads as its digits. Not a yes or a no."""
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, int | float):
        yield str(value)
    elif isinstance(value, Mapping):
        for key, inner in value.items():
            yield from written(key)
            yield from written(inner)
    elif isinstance(value, list | tuple):
        for inner in value:
            yield from written(inner)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, inner in value.items():
            yield from _strings(key)
            yield from _strings(inner)
    elif isinstance(value, list | tuple):
        for inner in value:
            yield from _strings(inner)


def broken_rule(
    value: Any, limit: int, *, multiline: bool = False, agent_words: bool = True
) -> Rule | None:
    """The first rule a text breaks, or any string in a structure, or nothing.

    With `agent_words` off, only personal data, client identifiers and the bounds on text.
    """
    for text in _strings(value):
        if rule := _in_text(text, limit, multiline, agent_words):
            return rule
    return None


def refusal(what: str, rule: Rule) -> str:
    """The one line a refusal says. It repeats nothing of the text."""
    return f"The {what} is refused: it {WHY[rule]} (rule {rule})."
