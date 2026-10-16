"""Trust anchors for verifying credentials without a network.

`verify` cannot resolve `did:web` over HTTP if the goal is to work in an
air-gapped environment — which is exactly where the typical on-prem customer
lives. The public key has to come from somewhere the verifier already considers
trustworthy:

1. `--key` / `--did-document`: the verifier supplies it explicitly
2. anchors installed in `~/.opaas/anchors/` (pinned once)
3. anchors bundled with the tool

Never from the credential itself: a credential that carries its own key
verifies itself and proves nothing.

**What an anchor is, since D65.** It is the issuer's *root*: every key its
DID document publishes (`#key-1` and `#key-2`, one per hardware token). A
credential signed by one of them directly verifies against it as before. A
credential signed by an issuing key verifies only through a delegation that
one of them signed (`credential.delegation`). `--key` keeps its meaning: trust
this one key directly, whether it is the root or an issuing key.

The revocation list is found the same way and for the same reason
(`find_revocation_lists`): from the verifier's side, never from the credential.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from opaas.core.credential.keys import (
    load_public_key,
    public_key_from_did_document,
    public_key_from_multibase,
)
from opaas.core.credential.revocation import is_revocation_list

#: Anchors shipped with the package. Empty until the production root key
#: exists: we would rather have `verify` ask for the key than trust something
#: we have not published.
BUNDLED_ANCHORS = Path(__file__).parent / "anchors"
#: Revocation lists live beside the anchors, in their own directory, so the
#: anchor listing never mistakes one for a DID document.
REVOCATIONS_DIRNAME = "revocations"


class TrustAnchorError(RuntimeError):
    """There is no reliable way to obtain the issuer's public key."""


@dataclass(slots=True)
class TrustRoot:
    """The keys a verification may start from, and where they came from.

    `keys` pairs each verification-method id with its key. A bare `--key` has
    no id (None): the verifier vouched for the key itself, not for a name.
    """

    keys: list[tuple[str | None, Ed25519PublicKey]]
    origin: str
    #: The anchor's DID, or None for a bare `--key`.
    issuer: str | None = None

    @classmethod
    def direct(cls, public_key: Ed25519PublicKey, origin: str = "key supplied") -> TrustRoot:
        return cls(keys=[(None, public_key)], origin=origin)

    @property
    def is_direct(self) -> bool:
        return self.issuer is None

    def exact(self, method_id: Any) -> Ed25519PublicKey | None:
        """The key published under exactly this id; a bare `--key` answers for any id."""
        for key_id, key in self.keys:
            if key_id is None or key_id == method_id:
                return key
        return None

    def for_proof(self, method_id: Any) -> Ed25519PublicKey:
        """The key for a credential signed directly by the anchor.

        Falls back to the first key, as verification always has: a wrong key
        then fails on the signature rather than here.
        """
        return self.exact(method_id) or self.keys[0][1]


def user_anchor_dir() -> Path:
    if override := os.environ.get("OPAAS_ANCHOR_DIR"):
        return Path(override)
    return Path.home() / ".opaas" / "anchors"


def resolve_trust(
    issuer: str,
    *,
    key_path: Path | None = None,
    did_document_path: Path | None = None,
) -> TrustRoot:
    """The root to verify `issuer`'s credentials from, or a failure saying what is missing."""
    if key_path is not None:
        return TrustRoot.direct(load_public_key(key_path), f"key supplied ({key_path})")

    if did_document_path is not None:
        document = _read_json(did_document_path)
        _assert_issuer(document, issuer, did_document_path)
        return _root_from(document, f"DID document supplied ({did_document_path})")

    for directory, label in _anchor_dirs():
        for candidate in sorted(directory.glob("*.json")):
            document = _read_json(candidate)
            if document.get("id") == issuer:
                return _root_from(document, f"{label} ({candidate})")

    raise TrustAnchorError(
        f"There is no trust anchor for issuer {issuer!r}. Options:\n"
        f"  * supply the public key:       --key <file.pub.pem>\n"
        f"  * supply the DID document:     --did-document <did.json>\n"
        f"  * install the anchor once:     copy did.json into {user_anchor_dir()}\n"
        "The key is NOT taken from the credential: that would make it self-verifying."
    )


def resolve_public_key(
    issuer: str,
    key_id: str | None = None,
    *,
    key_path: Path | None = None,
    did_document_path: Path | None = None,
) -> tuple[Ed25519PublicKey, str]:
    """Returns (key, human-readable origin) for a credential signed by the anchor itself.

    Kept for callers that only handle direct signatures; `resolve_trust` is
    what can also walk a delegation.
    """
    if key_path is None and did_document_path is not None:
        document = _read_json(did_document_path)
        _assert_issuer(document, issuer, did_document_path)
        return (
            public_key_from_did_document(document, key_id),
            f"DID document supplied ({did_document_path})",
        )
    root = resolve_trust(issuer, key_path=key_path, did_document_path=did_document_path)
    return root.for_proof(key_id), root.origin


def find_revocation_lists(
    issuer: str, *, path: Path | None = None
) -> list[tuple[dict[str, Any], str]]:
    """Every revocation list held for `issuer`, with where each came from.

    `path` is an explicit `--revocations` file and replaces the search. Lists
    are returned as found; whether each verifies under the root is decided
    by `verify_credential`, which has the root.
    """
    if path is not None:
        document = _read_json(path)
        if not is_revocation_list(document):
            raise TrustAnchorError(f"{path} is not a revocation list.")
        if document.get("issuer") != issuer:
            raise TrustAnchorError(
                f"The revocation list at {path} belongs to {document.get('issuer')!r}, "
                f"not to {issuer!r}."
            )
        return [(document, f"revocations supplied ({path})")]

    found = []
    for directory, label in _anchor_dirs():
        for candidate in sorted((directory / REVOCATIONS_DIRNAME).glob("*.json")):
            document = _read_json(candidate)
            if is_revocation_list(document) and document.get("issuer") == issuer:
                found.append((document, f"{label} ({candidate})"))
    return found


def _anchor_dirs() -> list[tuple[Path, str]]:
    sources = ((user_anchor_dir(), "local anchor"), (BUNDLED_ANCHORS, "package anchor"))
    return [(directory, label) for directory, label in sources if directory.is_dir()]


def _root_from(document: dict[str, Any], origin: str) -> TrustRoot:
    keys: list[tuple[str | None, Ed25519PublicKey]] = []
    for method in document.get("verificationMethod") or []:
        if multibase := method.get("publicKeyMultibase"):
            try:
                keys.append((str(method.get("id")), public_key_from_multibase(str(multibase))))
            except ValueError as exc:
                raise TrustAnchorError(
                    f"{origin}: unreadable key {method.get('id')}: {exc}"
                ) from None
    if not keys:
        raise TrustAnchorError(f"{origin}: the DID document contains no usable key.")
    return TrustRoot(keys=keys, origin=origin, issuer=str(document.get("id")))


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise TrustAnchorError(f"Does not exist: {path}") from None
    except json.JSONDecodeError as exc:
        raise TrustAnchorError(f"Invalid JSON in {path}: {exc}") from None


def _assert_issuer(document: dict, issuer: str, path: Path) -> None:
    if document.get("id") != issuer:
        raise TrustAnchorError(
            f"The DID document at {path} belongs to {document.get('id')!r}, "
            f"but the credential claims to have been issued by {issuer!r}"
        )
