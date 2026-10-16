"""Verifiable digital credential (W3C VC).

`keys` manages Ed25519 pairs, `vc` builds/signs/verifies credentials and
`trust` resolves where the root comes from when verifying offline. Since D65
the root signs `delegation`s to an issuing key and a `revocation` list; both
use the same proof (`proof`), and `signers` lets the root sign from a
hardware token.
"""

from opaas.core.credential.delegation import (
    DEFAULT_DELEGATION_DAYS,
    DELEGATION_FIELD,
    DelegationError,
    build_delegation,
    identity_for_signing,
    root_method_id,
    sign_delegation,
)
from opaas.core.credential.keys import (
    ONPREMIALABS_DID,
    KeyPairPaths,
    b58decode,
    b58encode,
    build_did_document,
    build_root_did_document,
    did_key,
    fingerprint,
    generate_root_keypair,
    load_private_key,
    load_public_key,
    public_key_from_did_document,
    public_key_from_multibase,
    public_key_multibase,
)
from opaas.core.credential.proof import Signer
from opaas.core.credential.revocation import (
    build_revocation_list,
    is_revocation_list,
    revocation_entry,
    sign_revocation_list,
)
from opaas.core.credential.signers import Pkcs11Signer, SignerError
from opaas.core.credential.trust import (
    TrustAnchorError,
    TrustRoot,
    find_revocation_lists,
    resolve_public_key,
    resolve_trust,
    user_anchor_dir,
)
from opaas.core.credential.vc import (
    CRYPTOSUITE,
    DEFAULT_VALIDITY_DAYS,
    EVIDENCE_KINDS,
    CredentialError,
    VerificationResult,
    build_credential,
    build_evidence_credential,
    build_system_credential,
    credential_id,
    public_identifier,
    sign_credential,
    verify_credential,
)

__all__ = [
    "CRYPTOSUITE",
    "DEFAULT_DELEGATION_DAYS",
    "DEFAULT_VALIDITY_DAYS",
    "DELEGATION_FIELD",
    "EVIDENCE_KINDS",
    "ONPREMIALABS_DID",
    "CredentialError",
    "DelegationError",
    "KeyPairPaths",
    "Pkcs11Signer",
    "Signer",
    "SignerError",
    "TrustAnchorError",
    "TrustRoot",
    "VerificationResult",
    "b58decode",
    "b58encode",
    "build_credential",
    "build_delegation",
    "build_did_document",
    "build_root_did_document",
    "build_evidence_credential",
    "build_revocation_list",
    "build_system_credential",
    "credential_id",
    "did_key",
    "find_revocation_lists",
    "fingerprint",
    "generate_root_keypair",
    "identity_for_signing",
    "is_revocation_list",
    "load_private_key",
    "load_public_key",
    "public_identifier",
    "public_key_from_did_document",
    "public_key_from_multibase",
    "public_key_multibase",
    "resolve_public_key",
    "resolve_trust",
    "revocation_entry",
    "root_method_id",
    "sign_credential",
    "sign_delegation",
    "sign_revocation_list",
    "user_anchor_dir",
    "verify_credential",
]
