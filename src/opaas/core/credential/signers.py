"""Where a signature comes from: a key file, or a hardware token (D65).

The issuing key is an encrypted PKCS#8 file, and `Ed25519PrivateKey` signs as
it always has. The root lives on a hardware token and never leaves it:
`Pkcs11Signer` asks the token to sign and reads back only the signature and
the public key. Both satisfy `proof.Signer`, so nothing that signs needs to
know which one it holds.

PKCS#11 is the one interface the tokens D65 considers share: a YubiKey's PIV
applet through Yubico's `ykcs11`, or OpenSC's module for other PIV cards and
HSMs. The binding (`python-pkcs11`) is the optional `token` extra and is
imported inside `Pkcs11Signer.open`, so a machine that never touches a token
never needs it. The token must support Ed25519 (`CKK_EC_EDWARDS`,
`CKM_EDDSA`); one that does not fails when opened, before anything is signed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

#: The DER header of an OCTET STRING holding 32 bytes: what PKCS#11 v3 tokens
#: return as CKA_EC_POINT for an Ed25519 key. Some return the 32 bytes bare.
_OCTET_STRING_32 = b"\x04\x20"


class SignerError(RuntimeError):
    """The signer cannot be opened or cannot sign."""


@dataclass(slots=True)
class Pkcs11Signer:
    """An Ed25519 key held by a PKCS#11 token, used without ever exporting it."""

    #: The PKCS#11 module, e.g. `/usr/lib/libykcs11.so` or `opensc-pkcs11.so`.
    module: str
    #: The private key's CKA_LABEL on the token.
    key_label: str
    pin: str = field(repr=False)
    #: The token's label; without it, the first token with the key is used.
    token_label: str | None = None
    _session: Any = field(default=None, repr=False)
    _private: Any = field(default=None, repr=False)
    _public: Ed25519PublicKey | None = field(default=None, repr=False)

    def open(self) -> Pkcs11Signer:
        """Logs in and finds the key pair. Imports `pkcs11` here, never at module scope."""
        try:
            import pkcs11
            from pkcs11 import Attribute, KeyType, ObjectClass
        except ImportError:
            raise SignerError(
                "Signing with a hardware token needs the 'token' extra: "
                "pip install 'opaas[token]' (or uv sync --extra token)."
            ) from None
        try:
            library = pkcs11.lib(self.module)
            token = (
                library.get_token(token_label=self.token_label)
                if self.token_label
                else next(iter(library.get_slots(token_present=True))).get_token()
            )
            self._session = token.open(user_pin=self.pin)
            edwards = getattr(KeyType, "EC_EDWARDS", None)
            if edwards is None:
                raise SignerError(
                    "This python-pkcs11 has no EC_EDWARDS key type: upgrade it to a "
                    "version that supports Ed25519."
                )
            self._private = self._session.get_key(
                object_class=ObjectClass.PRIVATE_KEY, key_type=edwards, label=self.key_label
            )
            public = self._session.get_key(
                object_class=ObjectClass.PUBLIC_KEY, key_type=edwards, label=self.key_label
            )
            self._public = _ed25519_from_ec_point(bytes(public[Attribute.EC_POINT]))
        except SignerError:
            raise
        except Exception as exc:  # the binding raises its own hierarchy, and many kinds
            raise SignerError(
                f"Could not open the Ed25519 key {self.key_label!r} on the token "
                f"({type(exc).__name__}: {exc}). Check the module path, the PIN and that the "
                "token holds an Ed25519 key under that label."
            ) from None
        return self

    def public_key(self) -> Ed25519PublicKey:
        if self._public is None:
            self.open()
        assert self._public is not None
        return self._public

    def sign(self, data: bytes) -> bytes:
        if self._private is None:
            self.open()
        from pkcs11 import Mechanism

        eddsa = getattr(Mechanism, "EDDSA", None)
        if eddsa is None:
            raise SignerError("This python-pkcs11 has no EDDSA mechanism: upgrade it.")
        try:
            return bytes(self._private.sign(data, mechanism=eddsa))
        except Exception as exc:
            raise SignerError(f"The token refused to sign: {type(exc).__name__}: {exc}") from None

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None


def _ed25519_from_ec_point(point: bytes) -> Ed25519PublicKey:
    if len(point) == 34 and point.startswith(_OCTET_STRING_32):
        point = point[2:]
    if len(point) != 32:
        raise SignerError(
            f"The token's public key is {len(point)} bytes; an Ed25519 key is 32. "
            "Is the key under that label Ed25519?"
        )
    return Ed25519PublicKey.from_public_bytes(point)
