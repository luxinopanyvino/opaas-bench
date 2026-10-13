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
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from opaas.core.credential.keys import load_public_key, public_key_from_did_document

#: Anchors shipped with the package. Empty until the production root key
#: exists: we would rather have `verify` ask for the key than trust something
#: we have not published.
BUNDLED_ANCHORS = Path(__file__).parent / "anchors"


class TrustAnchorError(RuntimeError):
    """There is no reliable way to obtain the issuer's public key."""


def user_anchor_dir() -> Path:
    if override := os.environ.get("OPAAS_ANCHOR_DIR"):
        return Path(override)
    return Path.home() / ".opaas" / "anchors"


def resolve_public_key(
    issuer: str,
    key_id: str | None = None,
    *,
    key_path: Path | None = None,
    did_document_path: Path | None = None,
) -> tuple[Ed25519PublicKey, str]:
    """Returns (key, human-readable origin) or fails explaining what is missing."""
    if key_path is not None:
        return load_public_key(key_path), f"key supplied ({key_path})"

    if did_document_path is not None:
        document = _read_json(did_document_path)
        _assert_issuer(document, issuer, did_document_path)
        return (
            public_key_from_did_document(document, key_id),
            f"DID document supplied ({did_document_path})",
        )

    sources = ((user_anchor_dir(), "local anchor"), (BUNDLED_ANCHORS, "package anchor"))
    for directory, label in sources:
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.glob("*.json")):
            document = _read_json(candidate)
            if document.get("id") == issuer:
                return public_key_from_did_document(document, key_id), f"{label} ({candidate})"

    raise TrustAnchorError(
        f"There is no trust anchor for issuer {issuer!r}. Options:\n"
        f"  * supply the public key:       --key <file.pub.pem>\n"
        f"  * supply the DID document:     --did-document <did.json>\n"
        f"  * install the anchor once:     copy did.json into {user_anchor_dir()}\n"
        "The key is NOT taken from the credential: that would make it self-verifying."
    )


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
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
