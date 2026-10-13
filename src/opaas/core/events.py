"""The `AuditEvent`: one record of one action, shared by every module (D42).

Every producer (the `smith` mock-tool oracle, the `sentinel` proxy, the
`inquest` importers) emits these records and every consumer reads them. The
schema is the most consequential decision in the agent work: *Auditable
Agents* (Proposition 1) shows that one field missing from the record schema
makes a whole class of policies undecidable, however many events are logged.
What a field here does not carry, no audit downstream can recover.

**A record is a dict, not an object.** Records arrive from third parties
(OpenTelemetry spans, framework traces) with fields missing, and *missing* has
to stay distinguishable from *empty*: an `actorChain` that is absent says the
producer never recorded who acted; an empty one says it recorded nobody. A
dataclass with defaults would fill in exactly the gap the audit exists to
find. `AuditEvent` below is the producer-side builder; everything that reads
evidence works on the plain dict and asks `missing_fields()`.

**Arguments and outputs are digests by default (N4).** `args` and `output`
are `{"digest": "sha256:…"}`; a `value` travels next to the digest only when
a policy needs it and the operator kept it. The digest still proves *what*
was sent to anyone holding the value, without publishing the value.

**The log is a hash chain, sealed in batches.** `EventLog` appends each
record with the hash of the previous one, and every so often a *seal* line
signs the head of the chain with an Ed25519 key. The chain makes any
rewrite of a past record detectable; the seal says who vouches for the
chain up to that point. The key is an operational key, never the root key
(key-custody.md), and a verifier never takes it from the log (invariant 6).
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from opaas.core.canonical import canonicalize
from opaas.core.credential.keys import b58decode, b58encode, did_key

#: Bumped whenever a field below changes meaning. A log written under one
#: version is audited under that version's required fields.
EVENT_SCHEMA_VERSION = 1

#: What kind of action a record describes.
ACTIONS: tuple[str, ...] = (
    "tool_call",
    "model_call",
    "delegation",
    "approval",
    "handoff",
    "message",
)

#: Where in its lifecycle the action was. A retry that is logged as a plain
#: attempt hides the failure before it; a block that is not logged at all
#: hides the policy that fired. Lifecycle coverage (LPC) counts these.
PHASES: tuple[str, ...] = ("attempt", "retry", "fallback", "block", "pending", "approval",
                           "handoff")

#: Roles in an actor chain, outermost first: user → agent → skill → tool → service.
ACTOR_ROLES: tuple[str, ...] = ("user", "service", "schedule", "agent", "skill", "tool",
                                "model")

#: Roles that can originate a chain. A chain that starts at an agent says the
#: agent acted on nobody's behalf, which is a finding, not a default.
PRINCIPAL_ROLES: tuple[str, ...] = ("user", "service", "schedule")

#: Fields every record must carry to be fully recoverable, per action. Relative
#: field recoverability (RF) is measured against this table, so changing it
#: changes the metric: it is versioned with the schema.
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "tool_call": ("id", "ts", "action", "target", "phase", "actorChain", "args", "output"),
    "model_call": ("id", "ts", "action", "target", "phase", "actorChain", "args", "output"),
    "delegation": ("id", "ts", "action", "target", "phase", "actorChain"),
    "approval": ("id", "ts", "action", "target", "phase", "actorChain"),
    "handoff": ("id", "ts", "action", "target", "phase", "actorChain"),
    "message": ("id", "ts", "action", "phase", "actorChain"),
}

#: Integrity levels, weakest first. `append-only` is a property of the storage,
#: not of the content: it can be declared, never observed from the file.
INTEGRITY_LEVELS: tuple[str, ...] = ("none", "append-only", "hash-chained", "signed")

GENESIS = "sha256:" + "0" * 64


class EventError(ValueError):
    """A record or a log is malformed, or a chain or seal does not verify."""


def digest(value: Any) -> str:
    """The digest a record carries instead of a value: SHA-256 over JCS.

    Bytes and text that are not JSON are hashed as they are, so a digest can
    be recomputed by whoever holds the original request body.
    """
    if isinstance(value, bytes):
        return "sha256:" + hashlib.sha256(value).hexdigest()
    return "sha256:" + hashlib.sha256(canonicalize(value)).hexdigest()


def evidence(value: Any, *, keep: bool = False) -> dict[str, Any]:
    """`{"digest": …}`, plus the value itself only when the caller must keep it (N4)."""
    recorded: dict[str, Any] = {"digest": digest(value)}
    if keep:
        recorded["value"] = value
    return recorded


def utcnow_iso() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(slots=True)
class AuditEvent:
    """Builder for a complete record. Producers use it; auditors read dicts."""

    action: str
    target: str | None
    actor_chain: list[dict[str, str]] | None
    phase: str = "attempt"
    args: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    policy_decisions: list[dict[str, Any]] = field(default_factory=list)
    ts: str = field(default_factory=utcnow_iso)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    #: W3C Trace Context identifiers, when the producer saw them.
    trace: dict[str, str] | None = None
    duration_ms: float | None = None
    error: str | None = None
    #: Who wrote the record: "sentinel", "smith-oracle", "agent", "import:otel"…
    source: str | None = None
    #: Producer-specific facts that are not part of the audited schema.
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise EventError(f"Unknown action '{self.action}'. Known: {', '.join(ACTIONS)}.")
        if self.phase not in PHASES:
            raise EventError(f"Unknown phase '{self.phase}'. Known: {', '.join(PHASES)}.")

    def to_record(self) -> dict[str, Any]:
        """The record as it is logged. A field the producer did not have is left out."""
        record: dict[str, Any] = {
            "schema": EVENT_SCHEMA_VERSION,
            "id": self.id,
            "ts": self.ts,
            "action": self.action,
            "phase": self.phase,
            "policyDecisions": list(self.policy_decisions),
        }
        optional = {
            "target": self.target,
            "actorChain": self.actor_chain,
            "args": self.args,
            "output": self.output,
            "trace": self.trace,
            "durationMs": self.duration_ms,
            "error": self.error,
            "source": self.source,
        }
        record.update({k: v for k, v in optional.items() if v is not None})
        if self.extra:
            record["extra"] = dict(self.extra)
        return record


def parse_actor_chain(text: str) -> list[dict[str, str]]:
    """`user:alice>agent:planner>tool:search` → the chain, outermost first.

    The compact form travels in the `X-OPAAS-Actor-Chain` header, which is how
    a caller tells the sentinel on whose behalf it acts.
    """
    chain: list[dict[str, str]] = []
    for part in text.split(">"):
        role, _, ident = part.strip().partition(":")
        if role and ident:
            chain.append({"role": role.strip(), "id": ident.strip()})
    return chain


def missing_fields(record: dict[str, Any]) -> list[str]:
    """Required fields this record does not carry, for its action."""
    required = REQUIRED_FIELDS.get(str(record.get("action")), REQUIRED_FIELDS["message"])
    return [name for name in required if _absent(record, name)]


def _absent(record: dict[str, Any], name: str) -> bool:
    value = record.get(name)
    if value is None:
        return True
    if name in ("args", "output"):
        return not isinstance(value, dict) or not (value.get("digest") or "value" in value)
    return name == "actorChain" and not isinstance(value, list)


# ------------------------------------------------------------------------- log


@dataclass(slots=True)
class Seal:
    """A signature over the head of the chain, up to and including `upTo`."""

    up_to: int
    head_hash: str
    signed_at: str
    verification_method: str
    proof_value: str

    def payload(self) -> dict[str, Any]:
        return {"upTo": self.up_to, "headHash": self.head_hash, "signedAt": self.signed_at,
                "verificationMethod": self.verification_method}


def _entry_hash(seq: int, previous: str, record: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(
        canonicalize({"seq": seq, "previousHash": previous, "event": record})
    ).hexdigest()


class EventLog:
    """Append-only, hash-chained JSONL of records, optionally sealed in batches.

    Lines are either `{"seq", "previousHash", "event", "hash"}` or
    `{"seal": {...}}`. Thread-safe: the sentinel proxy appends from many
    request threads.
    """

    def __init__(
        self,
        path: Path,
        *,
        signing_key: Ed25519PrivateKey | None = None,
        seal_every: int = 100,
    ) -> None:
        self.path = path
        self._key = signing_key
        self._key_id = (
            f"{did_key(signing_key.public_key())}#key-1" if signing_key is not None else None
        )
        self._seal_every = max(1, seal_every)
        self._lock = threading.Lock()
        self._seq, self._head, self._unsealed = 0, GENESIS, 0
        if path.exists():
            entries, _ = read_log(path)
            if entries:
                verify_chain(entries)
                self._seq = entries[-1]["seq"] + 1
                self._head = entries[-1]["hash"]

    @property
    def head(self) -> str:
        return self._head

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            entry = {
                "seq": self._seq,
                "previousHash": self._head,
                "event": record,
                "hash": _entry_hash(self._seq, self._head, record),
            }
            self._write(entry)
            self._seq += 1
            self._head = entry["hash"]
            self._unsealed += 1
            if self._key is not None and self._unsealed >= self._seal_every:
                self._seal_locked()
            return entry

    def seal(self) -> Seal | None:
        """Signs the current head. Nothing to do without a key or new entries."""
        with self._lock:
            return self._seal_locked()

    def _seal_locked(self) -> Seal | None:
        if self._key is None or self._seq == 0 or self._unsealed == 0:
            return None
        payload = {"upTo": self._seq - 1, "headHash": self._head, "signedAt": utcnow_iso(),
                   "verificationMethod": self._key_id}
        signature = self._key.sign(canonicalize(payload))
        seal = Seal(
            up_to=payload["upTo"], head_hash=self._head, signed_at=payload["signedAt"],
            verification_method=str(self._key_id), proof_value="z" + b58encode(signature),
        )
        self._write({"seal": {**seal.payload(), "proofValue": seal.proof_value}})
        self._unsealed = 0
        return seal

    def _write(self, line: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def read_log(path: Path) -> tuple[list[dict[str, Any]], list[Seal]]:
    """Entries and seals, in file order. Malformed lines raise: a log is all or nothing."""
    entries: list[dict[str, Any]] = []
    seals: list[Seal] = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EventError(f"{path}:{number} is not JSON: {exc}") from None
        if "seal" in item:
            s = item["seal"]
            try:
                seals.append(Seal(int(s["upTo"]), str(s["headHash"]), str(s["signedAt"]),
                                  str(s["verificationMethod"]), str(s["proofValue"])))
            except (KeyError, TypeError, ValueError):
                raise EventError(f"{path}:{number} is a malformed seal.") from None
        elif {"seq", "previousHash", "event", "hash"} <= item.keys():
            entries.append(item)
        else:
            raise EventError(f"{path}:{number} is neither a chained entry nor a seal.")
    return entries, seals


def verify_chain(entries: list[dict[str, Any]]) -> None:
    """Raises naming every place the chain breaks."""
    problems: list[str] = []
    previous = GENESIS
    for position, entry in enumerate(entries):
        if entry.get("seq") != position:
            problems.append(f"entry {position}: declared seq {entry.get('seq')}")
        if entry.get("previousHash") != previous:
            problems.append(f"entry {position}: the chain breaks here")
        if _entry_hash(int(entry.get("seq", -1)), str(entry.get("previousHash")),
                       entry.get("event")) != entry.get("hash"):
            problems.append(f"entry {position}: the record is not what was chained")
        previous = str(entry.get("hash"))
    if problems:
        raise EventError("The event log is broken:\n  " + "\n  ".join(problems))


def verify_seals(
    entries: list[dict[str, Any]], seals: list[Seal], public_key: Ed25519PublicKey
) -> tuple[int, list[str]]:
    """How many entries a valid seal covers, and every seal that failed.

    The key is the verifier's, never the log's: a seal names its key, and a
    forger names their own (invariant 6).
    """
    problems: list[str] = []
    covered = 0
    for seal in seals:
        if not 0 <= seal.up_to < len(entries) or entries[seal.up_to]["hash"] != seal.head_hash:
            problems.append(f"seal up to {seal.up_to}: it signs a head this log does not have")
            continue
        try:
            signature = b58decode(seal.proof_value[1:])
            public_key.verify(signature, canonicalize(seal.payload()))
        except (InvalidSignature, ValueError):
            problems.append(f"seal up to {seal.up_to}: the signature does not match the key")
            continue
        covered = max(covered, seal.up_to + 1)
    return covered, problems


def records(entries: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for entry in entries:
        yield entry["event"]


__all__ = [
    "ACTIONS",
    "ACTOR_ROLES",
    "EVENT_SCHEMA_VERSION",
    "GENESIS",
    "INTEGRITY_LEVELS",
    "PHASES",
    "PRINCIPAL_ROLES",
    "REQUIRED_FIELDS",
    "AuditEvent",
    "EventError",
    "EventLog",
    "Seal",
    "digest",
    "evidence",
    "missing_fields",
    "parse_actor_chain",
    "parse_ts",
    "read_log",
    "records",
    "utcnow_iso",
    "verify_chain",
    "verify_seals",
]
