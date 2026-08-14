"""JSON canonicalization per RFC 8785 (JCS).

Everything that gets signed or hashed goes through here. JCS is used instead
of a home-grown serialization for one concrete reason: a verifier written in
Go, Rust or JavaScript has to be able to reconstruct exactly the same bytes
without replicating our decisions. JCS is specified and has implementations in
those languages; "json.dumps with sort_keys" does not.

Compared to the RDF canonicalization that `Ed25519Signature2020` requires, JCS
does not need a JSON-LD processor: that is the difference between a 50-line
verifier and one that drags a heavy library along per language.
"""

from __future__ import annotations

import math
from typing import Any

#: Outside this range, ECMAScript switches to exponential notation with rules
#: we do not replicate. Rather than emitting non-canonical bytes — and
#: therefore a signature nobody can verify — it fails explicitly.
_MAX_PLAIN = 1e21
_MIN_PLAIN = 1e-6

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


class CanonicalizationError(ValueError):
    """The value cannot be canonicalized in an interoperable way."""


def canonicalize(value: Any) -> bytes:
    """Serializes `value` to the canonical JCS bytes (UTF-8)."""
    return _serialize(value).encode("utf-8")


def canonicalize_str(value: Any) -> str:
    return _serialize(value)


def _serialize(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_serialize(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(f"{_string(k)}:{_serialize(v)}" for k, v in _sorted(value)) + "}"
    raise CanonicalizationError(f"Type is not JSON-serializable: {type(value).__name__}")


def _sorted(mapping: dict[Any, Any]) -> list[tuple[str, Any]]:
    """Sorts by UTF-16 code units, as JCS requires.

    This is not the same as Python's code-point ordering: they differ for
    characters outside the BMP.
    """
    for key in mapping:
        if not isinstance(key, str):
            raise CanonicalizationError(f"Non-string key: {key!r}")
    return sorted(mapping.items(), key=lambda item: item[0].encode("utf-16-be"))


def _string(value: str) -> str:
    out = ['"']
    for char in value:
        if escape := _ESCAPES.get(char):
            out.append(escape)
        elif char < "\x20":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise CanonicalizationError(f"JSON does not allow {value}")
    if value == 0:
        # -0.0 and 0.0 canonicalize the same way, as in ECMAScript.
        return "0"
    magnitude = abs(value)
    if magnitude >= _MAX_PLAIN or magnitude < _MIN_PLAIN:
        raise CanonicalizationError(
            f"Number outside the interoperably canonicalizable range: {value!r}. "
            "Round it or express the value in a different unit before signing it."
        )
    if value.is_integer():
        return str(int(value))
    # Python's repr() is already the shortest representation that preserves the
    # value, the same as ECMAScript's Number::toString in this range.
    return repr(value)
