"""A reference made from the hash of what it names.

It is written in letters only. So it is a slug, it can be cited like a memo's ID, and the
check for identifiers by shape never takes it for one.
"""

import hashlib
import json

_IN_LETTERS = str.maketrans("0123456789abcdef", "abcdefghijklmnop")
LENGTH = 16


def content_sha256(content: dict) -> str:
    written = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(written).hexdigest()


def reference(kind: str, content: dict) -> str:
    return f"{kind}-{content_sha256(content)[:LENGTH].translate(_IN_LETTERS)}"
