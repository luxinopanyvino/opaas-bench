"""The one Data Integrity proof every OPAAS signature uses (`eddsa-jcs-2022`).

A credential, a delegation and a revocation list are three different documents
signed the same way: SHA-256 of the JCS bytes of the proof configuration,
followed by SHA-256 of the JCS bytes of the document without its proof, and
Ed25519 over the 64 bytes (D1, D5). Keeping it in one place is what makes the
browser verifier (`verify.js`) a single reimplementation rather than three.

`proofPurpose` is part of the signed configuration. A credential is signed for
`assertionMethod`, a delegation and a revocation list for `capabilityDelegation`
(D65), so a signature made for one cannot be replayed as the other.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from opaas.core.canonical import canonicalize
from opaas.core.credential.keys import b58decode, b58encode

CREDENTIAL_CONTEXT = [
    "https://www.w3.org/ns/credentials/v2",
    "https://onpremialabs.com/credentials/v1",
]
PROOF_TYPE = "DataIntegrityProof"
CRYPTOSUITE = "eddsa-jcs-2022"
ASSERTION = "assertionMethod"
DELEGATION = "capabilityDelegation"


class Signer(Protocol):
    """Anything that holds an Ed25519 private key and signs with it.

    `Ed25519PrivateKey` already is one, so a key loaded from a file needs no
    wrapper. A hardware token is another (`credential.signers.Pkcs11Signer`):
    the private key never leaves it, and only `sign` crosses the boundary.
    """

    def public_key(self) -> Ed25519PublicKey: ...

    def sign(self, data: bytes) -> bytes: ...


def sign_document(
    document: dict[str, Any],
    signer: Signer,
    *,
    key_id: str,
    proof_purpose: str = ASSERTION,
    created: datetime | None = None,
) -> dict[str, Any]:
    """Returns the document with its Data Integrity `proof`."""
    unsecured = {k: v for k, v in document.items() if k != "proof"}
    proof_config = {
        "type": PROOF_TYPE,
        "cryptosuite": CRYPTOSUITE,
        "created": iso(created or datetime.now(timezone.utc)),
        "verificationMethod": key_id,
        "proofPurpose": proof_purpose,
        "@context": document.get("@context", CREDENTIAL_CONTEXT),
    }
    signature = signer.sign(signing_input(unsecured, proof_config))

    proof = {k: v for k, v in proof_config.items() if k != "@context"}
    proof["proofValue"] = "z" + b58encode(signature)
    return {**unsecured, "proof": proof}


def signing_input(unsecured: dict[str, Any], proof_config: dict[str, Any]) -> bytes:
    """VC Data Integrity signing input: H(config) || H(document)."""
    return hashlib.sha256(canonicalize(proof_config)).digest() + hashlib.sha256(
        canonicalize(unsecured)
    ).digest()


def check_proof(
    document: dict[str, Any],
    public_key: Ed25519PublicKey,
    *,
    proof_purpose: str | None = None,
) -> tuple[bool, str]:
    """Whether `document`'s proof is a valid signature by `public_key`.

    With `proof_purpose`, a proof made for another purpose is refused before
    the signature is even looked at.
    """
    proof = document.get("proof")
    if not isinstance(proof, dict):
        return False, "no proof: it is not signed"
    if proof.get("type") != PROOF_TYPE or proof.get("cryptosuite") != CRYPTOSUITE:
        return False, (
            f"{proof.get('type')} / {proof.get('cryptosuite')} "
            f"(expected {PROOF_TYPE} / {CRYPTOSUITE})"
        )
    if proof_purpose is not None and proof.get("proofPurpose") != proof_purpose:
        return False, (
            f"signed for {proof.get('proofPurpose')!r}, not {proof_purpose!r}: a signature "
            "made for one purpose is not accepted for another"
        )
    proof_value = str(proof.get("proofValue", ""))
    if not proof_value.startswith("z"):
        return False, "proofValue is not in base58btc multibase ('z...')"
    try:
        signature = b58decode(proof_value[1:])
    except ValueError as exc:
        return False, f"unreadable proofValue: {exc}"

    unsecured = {k: v for k, v in document.items() if k != "proof"}
    proof_config = {k: v for k, v in proof.items() if k != "proofValue"}
    proof_config["@context"] = document.get("@context", CREDENTIAL_CONTEXT)
    try:
        public_key.verify(signature, signing_input(unsecured, proof_config))
    except InvalidSignature:
        return False, "The signature does NOT match this key or the document was altered"
    return True, f"valid Ed25519 ({proof.get('verificationMethod')})"


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"Date missing or not a string: {value!r}")
    # `fromisoformat` only learned to read the 'Z' suffix in 3.11, and every
    # date we emit ends in one (`iso`). Normalising it here keeps a credential
    # verifiable on 3.10 — refusing to parse our own output would be absurd.
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
