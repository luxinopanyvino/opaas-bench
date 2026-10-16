"""The revocation list: which keys the root has withdrawn (D65, phase 10 step 4).

It is a document signed by the root, like a delegation, and `verify` reads it
offline from the same places it reads anchors — `--revocations`, then
`~/.opaas/anchors/revocations/`, then the anchors bundled with the tool —
never from the credential. A credential cannot be trusted to carry the news
that its own key was withdrawn.

An entry names a key by its public key multibase, so it matches whatever id
the key was published under. A revoked key is refused for every credential
it signed, whatever date the proof claims: after a compromise the proof's
`created` is exactly what an attacker would backdate. Legitimate credentials
signed with the key are reissued under a new delegation (`key-custody.md`).
An entry may name a root key too, for a lost token: delegations it signed
stop verifying.

**What it does not do.** An offline list is as fresh as the copy the
verifier holds. A verifier who never updates it never learns of a
revocation; the short delegations of D65 bound how long that lasts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from opaas.core.credential.proof import (
    CREDENTIAL_CONTEXT,
    DELEGATION,
    Signer,
    iso,
    sign_document,
)

REVOCATION_TYPE = "OpaasRevocationList"


def build_revocation_list(
    issuer: str,
    entries: list[dict[str, Any]],
    *,
    updated: datetime | None = None,
) -> dict[str, Any]:
    """The unsigned list. Each entry: `id`, `publicKeyMultibase`, `revokedAt`, `reason`."""
    for entry in entries:
        if not isinstance(entry.get("publicKeyMultibase"), str):
            raise ValueError(f"A revocation entry names no publicKeyMultibase: {entry!r}")
    seen: set[str] = set()
    unique = []
    for entry in entries:
        if entry["publicKeyMultibase"] not in seen:
            seen.add(entry["publicKeyMultibase"])
            unique.append(entry)
    return {
        "@context": CREDENTIAL_CONTEXT,
        "type": [REVOCATION_TYPE],
        "issuer": issuer,
        "updated": iso(updated or datetime.now(timezone.utc)),
        "revoked": unique,
    }


def revocation_entry(
    key_id: str, public_key_multibase: str, *, reason: str, revoked_at: datetime | None = None
) -> dict[str, Any]:
    return {
        "id": key_id,
        "publicKeyMultibase": public_key_multibase,
        "revokedAt": iso(revoked_at or datetime.now(timezone.utc)),
        "reason": reason,
    }


def sign_revocation_list(
    revocations: dict[str, Any], signer: Signer, *, key_id: str, created: datetime | None = None
) -> dict[str, Any]:
    """Signed by a root key, for the same purpose as a delegation."""
    return sign_document(
        revocations, signer, key_id=key_id, proof_purpose=DELEGATION, created=created
    )


def is_revocation_list(document: Any) -> bool:
    return isinstance(document, dict) and REVOCATION_TYPE in (document.get("type") or [])
