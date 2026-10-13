"""What integrity a log actually has, found rather than claimed (IS, VC).

`discover` asks it of the agent logs the operator names; `inquest` asks it of
every record set before auditing it. The answer is the strongest level the
file *demonstrates*:

- `signed`: an OPAAS event log whose chain verifies and whose seals verify
  with a key the verifier supplied. A seal checked against a key taken from
  the log itself proves nothing (invariant 6), so without `--key` a sealed
  log reaches `hash-chained` and the result says the seals went unverified.
- `hash-chained`: the chain verifies.
- `append-only`: never found in a file. Only an operator can declare it, and
  the result keeps that declaration apart (invariant 9).
- `none`: anything else, including a chained log whose chain is broken —
  which is reported as broken, not merely as unchained.

The time and bytes it took are recorded too: verification cost (VC) is the
paper's diagnostic for whether anyone will actually run the check.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from opaas.core.events import EventError, read_log, verify_chain, verify_seals
from opaas.core.metrics import integrity_level


def assess_log(
    path: Path,
    *,
    public_key: Ed25519PublicKey | None = None,
    declared_append_only: bool = False,
) -> dict[str, Any]:
    """The IS level the file demonstrates, with VC and every problem found."""
    started = time.perf_counter()
    size = path.stat().st_size if path.exists() else 0
    result: dict[str, Any] = {"path": path.name, "bytes": size, "problems": [],
                              "declaredAppendOnly": declared_append_only}
    if not path.exists():
        return {**result, "level": None, "format": "missing",
                "problems": [f"{path} does not exist."]}

    chained = signed = False
    try:
        entries, seals = read_log(path)
        result["format"] = "opaas-event-log"
        result["entries"] = len(entries)
        try:
            verify_chain(entries)
            chained = bool(entries)
        except EventError as exc:
            result["problems"].append(str(exc))
        result["seals"] = len(seals)
        if seals and chained:
            if public_key is None:
                result["problems"].append(
                    "The log carries seals, but no key was supplied to verify them; a key "
                    "taken from the log itself would prove nothing."
                )
            else:
                covered, problems = verify_seals(entries, seals, public_key)
                result["problems"] += problems
                result["sealedEntries"] = covered
                result["unsealedTail"] = len(entries) - covered
                signed = covered > 0 and not problems
    except EventError:
        result["format"] = _plain_format(path)
        result["entries"] = _count_lines(path)

    result["level"] = integrity_level(
        chained=chained, signed=signed, declared_append_only=declared_append_only
    )
    if result["level"] == "append-only":
        result["levelBasis"] = "declared by the operator, not observed"
    result["verificationCost"] = {
        "ms": round((time.perf_counter() - started) * 1000, 3),
        "bytes": size,
        "requires": "the verifier's public key" if signed else "nothing but the file",
    }
    return result


def _plain_format(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return "unreadable"
    try:
        json.loads(text)
        return "json"
    except json.JSONDecodeError:
        pass
    lines = [line for line in text.splitlines() if line.strip()]
    try:
        for line in lines[:50]:
            json.loads(line)
        return "jsonl"
    except json.JSONDecodeError:
        return "text"


def _count_lines(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8-sig").splitlines()
                   if line.strip())
    except (OSError, UnicodeDecodeError):
        return 0


__all__ = ["assess_log"]
