"""Transparency log: a hash chain of the certificates issued.

Every issuance appends an entry chained to the previous one. Altering or
deleting a past entry breaks the chain detectably, and a certificate that does
not appear in the log is suspicious in itself.

**What this is NOT.** It is not Certificate Transparency. CT uses Merkle
trees, compact inclusion and consistency proofs, and — the essential part —
*independent monitors* that watch the issuer and gossip with each other. Here
the log is published by the issuer itself: a hash chain only proves that the
**published** log is internally consistent. If onpremIALabs rewrote the entire
log and republished it, the chain would still add up.

What it does provide, and it is not nothing:

- Anyone keeping an earlier copy detects any retroactive rewrite.
- A certificate missing from the log gives away an out-of-process issuance.
- Publishing the log in a git repository puts the history in third-party
  hands: every `git clone` is a date-stamped copy.

Moving to real CT requires external monitors, and that is a product decision —
who operates them and why you would trust them — before it is a code one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opaas.core.canonical import canonicalize

#: Hash of entry zero: nothing precedes the first issuance.
GENESIS = "sha256:" + "0" * 64


class TransparencyError(ValueError):
    """The log is malformed or broken."""


@dataclass(slots=True)
class LogEntry:
    index: int
    issuedAt: str  # noqa: N815 - the log is public JSON in camelCase
    credentialId: str  # noqa: N815
    reportHash: str  # noqa: N815
    credentialHash: str  # noqa: N815
    previousHash: str  # noqa: N815
    entryHash: str  # noqa: N815

    def payload(self) -> dict[str, Any]:
        """What goes into the hash: everything except the hash itself."""
        return {k: v for k, v in asdict(self).items() if k != "entryHash"}


def entry_hash(payload: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonicalize(payload)).hexdigest()


def credential_hash(credential: dict[str, Any]) -> str:
    """Hash of the signed credential, including its proof."""
    return "sha256:" + hashlib.sha256(canonicalize(credential)).hexdigest()


def load_log(path: Path) -> list[LogEntry]:
    if not path.exists():
        return []
    entries = []
    # utf-8-sig: the log is shared and edited with Windows tools, which add a
    # BOM. Failing over that would be an obstacle with no upside.
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(LogEntry(**json.loads(line)))
        except (json.JSONDecodeError, TypeError) as exc:
            raise TransparencyError(f"Invalid entry in {path}:{number}: {exc}") from None
    return entries


def append_entry(
    path: Path,
    credential: dict[str, Any],
    report_hash: str,
    *,
    issued_at: datetime | None = None,
) -> LogEntry:
    """Appends an issuance to the log. Rejects duplicates."""
    entries = load_log(path)
    verify_log(entries)  # do not chain on top of an already-broken log

    credential_id = str(credential.get("id", ""))
    if not credential_id:
        raise TransparencyError("The credential has no identifier")
    if any(entry.credentialId == credential_id for entry in entries):
        raise TransparencyError(
            f"{credential_id} is already in the log. Reissuing the same identifier "
            "would hide which of the two versions is the good one."
        )

    payload = {
        "index": len(entries),
        "issuedAt": (issued_at or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "credentialId": credential_id,
        "reportHash": report_hash,
        "credentialHash": credential_hash(credential),
        "previousHash": entries[-1].entryHash if entries else GENESIS,
    }
    entry = LogEntry(**payload, entryHash=entry_hash(payload))

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
    return entry


def verify_log(entries: list[LogEntry]) -> dict[str, Any]:
    """Checks indices, chaining and hashes."""
    problems: list[str] = []
    previous = GENESIS

    for position, entry in enumerate(entries):
        if entry.index != position:
            problems.append(f"entry {position}: declared index {entry.index}")
        if entry.previousHash != previous:
            problems.append(
                f"entry {position} ({entry.credentialId}): the chain breaks here — "
                f"it points to {entry.previousHash[:23]}… and the previous one is {previous[:23]}…"
            )
        recomputed = entry_hash(entry.payload())
        if recomputed != entry.entryHash:
            problems.append(
                f"entry {position} ({entry.credentialId}): altered hash; "
                "the content is not what was logged"
            )
        previous = entry.entryHash

    if problems:
        raise TransparencyError(
            "The transparency log is broken:\n  " + "\n  ".join(problems)
        )
    return {
        "entries": len(entries),
        "headHash": entries[-1].entryHash if entries else GENESIS,
        "intact": True,
    }


def find_entry(entries: list[LogEntry], credential_id: str) -> LogEntry | None:
    return next((entry for entry in entries if entry.credentialId == credential_id), None)


def inclusion_check(
    entries: list[LogEntry], credential: dict[str, Any], report_hash: str
) -> tuple[bool, str]:
    """Is this specific credential in the log, and unchanged?"""
    credential_id = str(credential.get("id", ""))
    entry = find_entry(entries, credential_id)
    if entry is None:
        return False, (
            f"{credential_id} does NOT appear in the transparency log. "
            "Either it was not issued through the normal process, or the log is incomplete."
        )
    if entry.credentialHash != credential_hash(credential):
        return False, (
            "The credential is not the one logged under that identifier: "
            "someone modified it after issuance."
        )
    if entry.reportHash != report_hash:
        return False, (
            f"The logged report ({entry.reportHash}) is not this one ({report_hash})."
        )
    return True, f"Logged as entry {entry.index} on {entry.issuedAt}"
