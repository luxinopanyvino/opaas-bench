"""Evidence documents: everything a module produces that can be signed (D49).

Every module of the agent work writes its result the way `run` writes a
report (D2): canonical JCS bytes and nothing wrapping them, so that verifying
is `sha256sum` of the file. The envelope fields are the same everywhere:

- `kind`, one of `EVIDENCE_KINDS`, says what the document is;
- `opaasVersion` and `specVersion` say which methodology produced it;
- `environmentFingerprint` binds the result to the environment it was
  measured in (N5). A result says nothing outside that fingerprint, and a
  document that could not establish one says so rather than omitting it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opaas import OPAAS_SPEC_VERSION, __version__
from opaas.core.canonical import canonicalize
from opaas.core.credential.vc import EVIDENCE_KINDS
from opaas.core.events import utcnow_iso
from opaas.core.hashing import file_digest, sha256_json


class EvidenceError(ValueError):
    """An evidence document is malformed or of the wrong kind."""


def envelope(
    kind: str, body: dict[str, Any], *, environment: dict[str, Any] | None
) -> dict[str, Any]:
    """The document, with the fields every evidence document carries."""
    if kind not in EVIDENCE_KINDS:
        raise EvidenceError(f"Unknown evidence kind '{kind}'.")
    return {
        "kind": kind,
        "opaasVersion": __version__,
        "specVersion": OPAAS_SPEC_VERSION,
        "createdAt": utcnow_iso(),
        "environmentFingerprint": environment_binding(environment),
        **body,
    }


def environment_binding(environment: dict[str, Any] | None) -> dict[str, Any]:
    """What the result is bound to (N5), or why nothing is.

    `environment` is a discovery inventory, a deployment fingerprint, or both
    under `inventory` / `deployment`. Only their hashes are carried: the
    documents themselves travel separately and are matched by hash.
    """
    if not environment:
        return {"bound": False, "reason": "No environment inventory or deployment "
                "fingerprint was supplied: this result is bound to no environment."}
    parts = {}
    for name in ("inventory", "deployment"):
        value = environment.get(name)
        if isinstance(value, dict) and value.get("hash"):
            parts[f"{name}Hash"] = value["hash"]
    if not parts and environment.get("hash"):
        parts["inventoryHash"] = environment["hash"]
    if not parts:
        return {"bound": False, "reason": "The environment supplied carries no hash."}
    return {"bound": True, **parts, "hash": sha256_json(parts)}


def write_document(path: Path, document: dict[str, Any]) -> str:
    """Writes canonical bytes and returns the file hash that a credential certifies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonicalize(document))
    return file_digest(path)


def read_document(path: Path, *, kind: str | tuple[str, ...] | None = None) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"Cannot read {path}: {exc}") from None
    if not isinstance(document, dict):
        raise EvidenceError(f"{path} is not an evidence document.")
    kinds = (kind,) if isinstance(kind, str) else kind
    if kinds and document.get("kind") not in kinds:
        raise EvidenceError(
            f"{path} is a '{document.get('kind')}' document; expected {' or '.join(kinds)}."
        )
    return document


__all__ = [
    "EvidenceError",
    "envelope",
    "environment_binding",
    "read_document",
    "write_document",
]
