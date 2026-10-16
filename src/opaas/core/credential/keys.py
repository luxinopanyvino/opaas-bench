"""Ed25519 root key pair (root of trust, Phase 0).

The private key signs the credentials. The public one is published in a DID
document so anyone can verify a credential without depending on the issuer's
infrastructure.

**Whose name a key signs under is chosen, never assumed (D39).** Without
`did`, a new pair is named by itself: `did:key:z6Mk…`, derived from the public
key, which claims nothing about any organisation. A `did:web` name is only
used when asked for, by whoever controls that domain and will publish the
document there. onpremIALabs' production key is generated with
`did="did:web:onpremialabs.com"` (see `docs/key-custody.md`); before D39 that
was the default, and every key anyone generated claimed to be onpremIALabs.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

PRIVATE_KEY_FILENAME = "opaas-root-ed25519.pem"
PUBLIC_KEY_FILENAME = "opaas-root-ed25519.pub.pem"
DID_DOCUMENT_FILENAME = "did.json"

#: onpremIALabs' own issuer DID. Named, not defaulted: see the module docstring.
ONPREMIALABS_DID = "did:web:onpremialabs.com"


@dataclass(slots=True)
class KeyPairPaths:
    private_key: Path
    public_key: Path
    did_document: Path


def generate_root_keypair(
    out_dir: Path,
    *,
    passphrase: str | None = None,
    did: str | None = None,
    key_id: str | None = None,
    overwrite: bool = False,
) -> KeyPairPaths:
    """Generates the root pair and the DID document that publishes the public key."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = KeyPairPaths(
        private_key=out_dir / PRIVATE_KEY_FILENAME,
        public_key=out_dir / PUBLIC_KEY_FILENAME,
        did_document=out_dir / DID_DOCUMENT_FILENAME,
    )
    if paths.private_key.exists() and not overwrite:
        raise FileExistsError(
            f"A private key already exists at {paths.private_key}. "
            "Overwriting it would invalidate every credential signed with it; "
            "use --overwrite only if you know what you are doing."
        )

    private_key = Ed25519PrivateKey.generate()
    encryption: serialization.KeySerializationEncryption = serialization.NoEncryption()
    if passphrase:
        encryption = serialization.BestAvailableEncryption(passphrase.encode("utf-8"))

    paths.private_key.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=encryption,
        )
    )
    _restrict_permissions(paths.private_key)

    public_key = private_key.public_key()
    paths.public_key.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    paths.did_document.write_text(
        json.dumps(build_did_document(public_key, did=did, key_id=key_id), indent=2) + "\n",
        encoding="utf-8",
    )
    return paths


def did_key(public_key: Ed25519PublicKey) -> str:
    """`did:key` for an Ed25519 key: the key's own multibase, so it names nothing else."""
    return f"did:key:{public_key_multibase(public_key)}"


def build_did_document(
    public_key: Ed25519PublicKey, *, did: str | None = None, key_id: str | None = None
) -> dict:
    """The DID document for the key. `did:web` is served at `/.well-known/did.json`.

    Without `did`, the key is named by `did:key`, whose verification method id
    is, by that method's convention, the DID followed by the key's multibase.
    """
    if did is None:
        did = did_key(public_key)
        key_id = key_id or f"{did}#{public_key_multibase(public_key)}"
    key_id = key_id or f"{did}#key-1"
    return {
        "@context": ["https://www.w3.org/ns/did/v1", "https://w3id.org/security/suites/ed25519-2020/v1"],
        "id": did,
        "verificationMethod": [
            {
                "id": key_id,
                "type": "Ed25519VerificationKey2020",
                "controller": did,
                "publicKeyMultibase": public_key_multibase(public_key),
            }
        ],
        "assertionMethod": [key_id],
    }


def build_root_did_document(did: str, public_keys: list[Ed25519PublicKey]) -> dict:
    """The DID document of a root held on several tokens: `#key-1`, `#key-2`, … (D65).

    Each key was generated on its own token and never left it; only the
    public halves meet here. Every one is listed as an assertion method and as
    a delegation capability, because the root signs delegations and
    revocation lists, and a credential signed by a root key directly (before
    D65, or for a test) must still verify.
    """
    if not public_keys:
        raise ValueError("A root document needs at least one key.")
    multibases = [public_key_multibase(key) for key in public_keys]
    if len(set(multibases)) != len(multibases):
        raise ValueError("The same key appears twice: each token must hold its own key.")
    ids = [f"{did}#key-{index}" for index in range(1, len(public_keys) + 1)]
    return {
        "@context": ["https://www.w3.org/ns/did/v1", "https://w3id.org/security/suites/ed25519-2020/v1"],
        "id": did,
        "verificationMethod": [
            {
                "id": key_id,
                "type": "Ed25519VerificationKey2020",
                "controller": did,
                "publicKeyMultibase": multibase,
            }
            for key_id, multibase in zip(ids, multibases, strict=True)
        ],
        "assertionMethod": ids,
        "capabilityDelegation": ids,
    }


def public_key_multibase(public_key: Ed25519PublicKey) -> str:
    """base58btc multibase encoding with the ed25519-pub multicodec prefix (0xed01)."""
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return "z" + b58encode(b"\xed\x01" + raw)


def load_private_key(path: Path, passphrase: str | None = None) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(
        path.read_bytes(), password=passphrase.encode("utf-8") if passphrase else None
    )
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(f"The key at {path} is not Ed25519: {type(key).__name__}")
    return key


def load_public_key(path: Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError(f"The key at {path} is not Ed25519: {type(key).__name__}")
    return key


def fingerprint(public_key: Ed25519PublicKey) -> str:
    """Short fingerprint to check at a glance which key is in use."""
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return base64.b16encode(raw[:8]).decode("ascii").lower()


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _B58_ALPHABET[remainder] + encoded
    leading_zeros = len(data) - len(data.lstrip(b"\x00"))
    return "1" * leading_zeros + encoded


def b58decode(encoded: str) -> bytes:
    number = 0
    for char in encoded:
        index = _B58_ALPHABET.find(char)
        if index < 0:
            raise ValueError(f"Invalid character in base58: {char!r}")
        number = number * 58 + index
    leading_zeros = len(encoded) - len(encoded.lstrip("1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * leading_zeros + body


def public_key_from_multibase(value: str) -> Ed25519PublicKey:
    """Inverse of `public_key_multibase`, for reading keys from a DID document."""
    if not value.startswith("z"):
        raise ValueError("Expected base58btc multibase ('z...')")
    raw = b58decode(value[1:])
    if not raw.startswith(b"\xed\x01"):
        raise ValueError("The multicodec prefix is not ed25519-pub (0xed01)")
    return Ed25519PublicKey.from_public_bytes(raw[2:])


def public_key_from_did_document(document: dict, key_id: str | None = None) -> Ed25519PublicKey:
    """Extracts the public key from a `did:web` document."""
    methods = document.get("verificationMethod") or []
    if key_id:
        methods = [m for m in methods if m.get("id") == key_id] or methods
    for method in methods:
        if multibase := method.get("publicKeyMultibase"):
            return public_key_from_multibase(str(multibase))
    raise ValueError("The DID document contains no usable publicKeyMultibase")


def _restrict_permissions(path: Path) -> None:
    """Owner-only permissions. On Windows the ACL rules; this is best effort."""
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
