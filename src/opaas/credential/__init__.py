"""Verifiable digital credential (W3C VC).

`keys` manages the root Ed25519 pair, `vc` builds/signs/verifies credentials
and `trust` resolves where the public key comes from when verifying offline.
"""

from opaas.credential.keys import (
    ONPREMIALABS_DID,
    KeyPairPaths,
    b58decode,
    b58encode,
    build_did_document,
    did_key,
    fingerprint,
    generate_root_keypair,
    load_private_key,
    load_public_key,
    public_key_from_did_document,
    public_key_from_multibase,
    public_key_multibase,
)
from opaas.credential.trust import TrustAnchorError, resolve_public_key, user_anchor_dir
from opaas.credential.vc import (
    CRYPTOSUITE,
    DEFAULT_VALIDITY_DAYS,
    CredentialError,
    VerificationResult,
    build_credential,
    credential_id,
    public_identifier,
    sign_credential,
    verify_credential,
)

__all__ = [
    "CRYPTOSUITE",
    "DEFAULT_VALIDITY_DAYS",
    "ONPREMIALABS_DID",
    "CredentialError",
    "KeyPairPaths",
    "TrustAnchorError",
    "VerificationResult",
    "b58decode",
    "b58encode",
    "build_credential",
    "build_did_document",
    "credential_id",
    "did_key",
    "fingerprint",
    "generate_root_keypair",
    "load_private_key",
    "load_public_key",
    "public_identifier",
    "public_key_from_did_document",
    "public_key_from_multibase",
    "public_key_multibase",
    "resolve_public_key",
    "sign_credential",
    "user_anchor_dir",
    "verify_credential",
]
