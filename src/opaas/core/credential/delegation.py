"""Delegations: the offline root vouching for an issuing key (D65).

The root (`did:web:onpremialabs.com`, `#key-1` and `#key-2`, each on its own
hardware token) does not sign credentials. A few times a year it signs a
*delegation*: this public key, under this verification-method id, may sign
credentials in this issuer's name between these two dates. The issuing key,
an encrypted file on the signing machine, signs every credential and carries
the delegation inside it (`issuerDelegation`).

The delegation travels with the credential and is still never believed for
what it says. `verify` takes the root from an anchor obtained outside the
credential (D4) and accepts the issuing key only once the delegation verifies
under it. A credential that carries a key no anchor delegates is refused,
exactly as one that carried its own key always was.

**Depth is fixed** at root → issuing key → credential. A delegation names a
key, never another delegation, and there is no path building: a delegation
that tries to nest one is malformed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from opaas.core.credential.keys import fingerprint, public_key_multibase
from opaas.core.credential.proof import (
    CREDENTIAL_CONTEXT,
    DELEGATION,
    Signer,
    iso,
    parse_time,
    sign_document,
)

DELEGATION_TYPE = "OpaasKeyDelegation"
#: Where a signed credential carries its delegation. Inside the signed body, so
#: the issuing key's signature also covers which delegation it claimed.
DELEGATION_FIELD = "issuerDelegation"
#: Months, never less than the 90 days a credential lives (D28). The exact
#: length is the ceremony's choice and is recorded in the delegation itself.
DEFAULT_DELEGATION_DAYS = 180


class DelegationError(ValueError):
    """A delegation cannot be built, or does not allow this signature."""


def build_delegation(
    issuer: str,
    delegate_key: Ed25519PublicKey,
    *,
    valid_from: datetime,
    valid_until: datetime,
    delegate_id: str | None = None,
) -> dict[str, Any]:
    """The unsigned delegation from `issuer`'s root to `delegate_key`.

    The verification-method id lives under the issuer's DID, because that is
    the name every credential signed with it will carry. Without one, it is
    derived from the key's fingerprint, so two issuing keys never share a name.
    """
    delegate_id = delegate_id or f"{issuer}#issuing-{fingerprint(delegate_key)}"
    if not delegate_id.startswith(f"{issuer}#"):
        raise DelegationError(
            f"The delegated key's id {delegate_id!r} is not under {issuer!r}. Credentials "
            f"signed with it carry this id, so it has to name the issuer: use "
            f"{issuer}#<fragment>."
        )
    if valid_until <= valid_from:
        raise DelegationError("The delegation ends before it starts.")
    return {
        "@context": CREDENTIAL_CONTEXT,
        "type": [DELEGATION_TYPE],
        "issuer": issuer,
        "delegate": {
            "id": delegate_id,
            "type": "Ed25519VerificationKey2020",
            "controller": issuer,
            "publicKeyMultibase": public_key_multibase(delegate_key),
        },
        "validFrom": iso(valid_from),
        "validUntil": iso(valid_until),
    }


def sign_delegation(
    delegation: dict[str, Any], signer: Signer, *, key_id: str, created: datetime | None = None
) -> dict[str, Any]:
    """Signs a delegation with a root key, for `capabilityDelegation` only."""
    return sign_document(
        delegation, signer, key_id=key_id, proof_purpose=DELEGATION, created=created
    )


def root_method_id(did_document: dict[str, Any], public_key: Ed25519PublicKey) -> str:
    """The id under which `did_document` publishes `public_key`, or a refusal.

    The root signs under the id its published document gives the key. A token
    holding a key the document does not list would sign a delegation nobody
    can verify against that root.
    """
    own = public_key_multibase(public_key)
    for method in did_document.get("verificationMethod") or []:
        if method.get("publicKeyMultibase") == own:
            return str(method.get("id"))
    raise DelegationError(
        f"The root document {did_document.get('id')!r} does not list the key this signer "
        f"holds (fingerprint {fingerprint(public_key)}). Use the token whose key is "
        "published there, or the document that publishes this one."
    )


def delegation_problem(delegation: Any) -> str | None:
    """What makes this not a delegation, or None. Shape only: no signature."""
    if not isinstance(delegation, dict):
        return "the delegation is not a JSON object"
    if DELEGATION_TYPE not in (delegation.get("type") or []):
        return f"type is {delegation.get('type')!r}, not {DELEGATION_TYPE}"
    if DELEGATION_FIELD in delegation or DELEGATION_FIELD in (delegation.get("delegate") or {}):
        return "it nests another delegation; depth is fixed at root → issuing key → credential"
    delegate = delegation.get("delegate")
    if not isinstance(delegate, dict) or not delegate.get("id"):
        return "it names no delegated key id"
    if not isinstance(delegate.get("publicKeyMultibase"), str):
        return "it carries no publicKeyMultibase for the delegated key"
    if not isinstance(delegation.get("issuer"), str):
        return "it names no issuer"
    try:
        parse_time(delegation.get("validFrom"))
        parse_time(delegation.get("validUntil"))
    except (TypeError, ValueError) as exc:
        return f"unreadable validity window: {exc}"
    return None


def covers(delegation: dict[str, Any], moment: datetime) -> tuple[bool, str]:
    """Whether the delegation's window contains `moment` (start inclusive, end exclusive)."""
    start = parse_time(delegation.get("validFrom"))
    end = parse_time(delegation.get("validUntil"))
    window = f"{iso(start)} → {iso(end)}"
    if moment < start:
        return False, f"{iso(moment)} is before the delegation starts ({window})"
    if moment >= end:
        return False, f"{iso(moment)} is after the delegation ended ({window})"
    return True, f"{iso(moment)} is within {window}"


def identity_for_signing(
    delegation: dict[str, Any],
    public_key: Ed25519PublicKey,
    *,
    at: datetime | None = None,
) -> tuple[str, str]:
    """(issuer, verification-method id) a credential is signed under, or a refusal.

    Refused at signing time rather than discovered at verification: a
    credential signed outside its delegation, or by a key the delegation does
    not name, would never verify, and a certificate already handed over that
    then fails is worse than one that was never issued.
    """
    if problem := delegation_problem(delegation):
        raise DelegationError(f"This is not a usable delegation: {problem}.")
    if not isinstance(delegation.get("proof"), dict):
        raise DelegationError(
            "The delegation is not signed. Have the root sign it with 'opaas delegate'."
        )
    delegate = delegation["delegate"]
    if delegate["publicKeyMultibase"] != public_key_multibase(public_key):
        raise DelegationError(
            f"The delegation is for another key: it names {delegate['id']} with fingerprint "
            f"different from the signing key's ({fingerprint(public_key)}). Sign with the "
            "key it delegates, or have the root delegate this one."
        )
    ok, detail = covers(delegation, at or datetime.now(timezone.utc))
    if not ok:
        raise DelegationError(
            f"The delegation does not cover this signature: {detail}. Have the root sign a "
            "new delegation before issuing anything else."
        )
    return str(delegation["issuer"]), str(delegate["id"])
