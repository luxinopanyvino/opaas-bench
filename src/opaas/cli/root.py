"""`opaas root-document`, `delegate` and `revoke`: the offline root (D65).

`root-document` assembles the root's DID document from the public halves of
the token keys, once, at the ceremony. The root then signs two kinds of
document, a few times a year, from an offline machine with a hardware token: a
delegation that lets an issuing key sign credentials for a while, and the
revocation list that withdraws one. All three are top-level commands beside
`keygen`, `sign` and `trust`, the other key commands (D41), and register on
the main app at the end of `cli/main.py`.

Neither signing command takes a key from anything it is handed to sign about. The
root's identity comes from its own DID document (`--root-did-document`), and
the signer must hold a key that document publishes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

import typer

from opaas.cli.common import _open_root_signer, _plain, console, err_console, read_json
from opaas.core.credential import (
    DEFAULT_DELEGATION_DAYS,
    DEFAULT_VALIDITY_DAYS,
    DelegationError,
    SignerError,
    build_delegation,
    build_revocation_list,
    build_root_did_document,
    fingerprint,
    is_revocation_list,
    load_public_key,
    public_key_from_multibase,
    public_key_multibase,
    revocation_entry,
    root_method_id,
    sign_delegation,
    sign_revocation_list,
)
from opaas.core.credential.proof import DELEGATION, check_proof, parse_time

RootDid = Annotated[
    Path,
    typer.Option(
        "--root-did-document",
        help="The root's did.json: the issuer and the keys it publishes (#key-1, #key-2).",
    ),
]
RootKey = Annotated[
    Path | None,
    typer.Option("--root-key", help="Root private key file. For a token, use --pkcs11-module."),
]
RootPassphrase = Annotated[
    str | None,
    typer.Option("--passphrase", envvar="OPAAS_KEY_PASSPHRASE",
                  help="Passphrase of --root-key; asked at the keyboard when absent."),
]
Pkcs11Module = Annotated[
    str | None,
    typer.Option(
        "--pkcs11-module",
        help="PKCS#11 module of the hardware token (e.g. libykcs11.so). Needs the 'token' "
        "extra. The PIN comes from OPAAS_TOKEN_PIN or the keyboard.",
    ),
]
KeyLabel = Annotated[
    str | None, typer.Option("--key-label", help="Label of the root key on the token.")
]
TokenLabel = Annotated[
    str | None, typer.Option("--token-label", help="Token label, when several are plugged in.")
]


def _root(
    root_did_document: Path,
    *,
    root_key: Path | None,
    passphrase: str | None,
    pkcs11_module: str | None,
    key_label: str | None,
    token_label: str | None,
) -> tuple[dict[str, Any], Any, str]:
    """(root DID document, signer, the method id the signer's key is published under)."""
    try:
        document = read_json(root_did_document)
        signer = _open_root_signer(
            key=root_key,
            passphrase=passphrase,
            pkcs11_module=pkcs11_module,
            key_label=key_label,
            token_label=token_label,
        )
        method = root_method_id(document, signer.public_key())
    except (OSError, json.JSONDecodeError, TypeError, ValueError, SignerError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    return document, signer, method


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


TokenPublicKeys = Annotated[
    list[Path],
    typer.Option(
        "--public-key",
        help="Public key (.pub.pem) exported from a root token. Repeat it: the first becomes "
        "#key-1, the second #key-2, in the order given.",
    ),
]
RootDidName = Annotated[
    str,
    typer.Option("--did", help="The root's DID, e.g. did:web:onpremialabs.com. Never defaulted."),
]
RootDocumentOut = Annotated[
    Path, typer.Option("--out", "-o", help="DID document to write.")
]
RootOverwrite = Annotated[
    bool, typer.Option("--overwrite", help="Replace an existing document at --out.")
]


def root_document(
    public_keys: TokenPublicKeys,
    did: RootDidName,
    out: RootDocumentOut = Path("did.json"),
    overwrite: RootOverwrite = False,
) -> None:
    """Assembles the root's DID document from the tokens' public keys (D65).

    Each root key was generated on its own token and never left it; only the
    public halves meet here, so this runs anywhere. What it prints is what the
    ceremony writes down and checks over a second channel.
    """
    if len(public_keys) < 2:
        # D65 holds the root on two tokens, each the other's backup. One key
        # is a root that a single lost token ends.
        console.print(
            f"[yellow]{len(public_keys)} key given.[/yellow] D65 holds the root on two tokens, "
            "each the other's backup; with one, losing it means a new root."
        )
    if out.exists() and not overwrite:
        err_console.print(
            f"[red]{out} already exists.[/red] A root document that is already published is "
            "what verifiers pinned; pass --overwrite only to replace a draft."
        )
        raise typer.Exit(code=2)
    try:
        keys = [load_public_key(path) for path in public_keys]
        document = build_root_did_document(did, keys)
    except (OSError, TypeError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    _write(out, document)

    console.print("[green]Root DID document written.[/green]")
    console.print(f"  issuer   : {_plain(did)}")
    for path, key, method in zip(public_keys, keys, document["verificationMethod"], strict=True):
        console.print(f"  {_plain(method['id'])}")
        console.print(f"    from        : {path}")
        console.print(f"    fingerprint : {fingerprint(key)}")
        console.print(f"    multibase   : {method['publicKeyMultibase']}")
    console.print(f"  file     : {out}")
    console.print(f"  sha256   : {hashlib.sha256(out.read_bytes()).hexdigest()}")
    console.print(
        "\n[dim]Write the fingerprints down and check each against its token over a second "
        "channel before anything is signed or published.[/dim]"
    )


IssuingKey = Annotated[
    Path, typer.Option("--issuing-key", help="Public key (.pub.pem) of the key to delegate to.")
]
DelegateId = Annotated[
    str | None,
    typer.Option(
        "--id",
        help="Verification-method id for the issuing key, under the root's DID (e.g. "
        "did:web:onpremialabs.com#issuing-2026-10). Defaults to #issuing-<fingerprint>.",
    ),
]
ValidDays = Annotated[
    int,
    typer.Option(
        "--valid-days",
        help=f"How long the delegation lasts. More than the {DEFAULT_VALIDITY_DAYS} days a "
        "credential lives (D65).",
    ),
]
ValidFrom = Annotated[
    str | None,
    typer.Option("--valid-from", help="ISO date it starts; defaults to now."),
]
DelegationOut = Annotated[
    Path, typer.Option("--out", "-o", help="Delegation file to write.")
]


def delegate(
    issuing_key: IssuingKey,
    root_did_document: RootDid,
    root_key: RootKey = None,
    passphrase: RootPassphrase = None,
    pkcs11_module: Pkcs11Module = None,
    key_label: KeyLabel = None,
    token_label: TokenLabel = None,
    delegate_id: DelegateId = None,
    valid_days: ValidDays = DEFAULT_DELEGATION_DAYS,
    valid_from: ValidFrom = None,
    out: DelegationOut = Path("delegation.json"),
) -> None:
    """Has the root delegate credential signing to an issuing key (D65)."""
    if valid_days <= DEFAULT_VALIDITY_DAYS:
        err_console.print(
            f"[red]A delegation of {valid_days} days is not longer than the "
            f"{DEFAULT_VALIDITY_DAYS} days a credential lives.[/red] D65 sets it to months; "
            "pass a longer --valid-days."
        )
        raise typer.Exit(code=2)
    try:
        delegated = load_public_key(issuing_key)
        start = parse_time(valid_from) if valid_from else datetime.now(timezone.utc)
    except (OSError, TypeError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None

    document, signer, method = _root(
        root_did_document, root_key=root_key, passphrase=passphrase,
        pkcs11_module=pkcs11_module, key_label=key_label, token_label=token_label,
    )
    issuer = str(document.get("id") or "")
    try:
        unsigned = build_delegation(
            issuer, delegated, valid_from=start, valid_until=start + timedelta(days=valid_days),
            delegate_id=delegate_id,
        )
        signed = sign_delegation(unsigned, signer, key_id=method)
    except (DelegationError, SignerError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None

    # A token that signed something else than it was asked would be found out
    # at the first verification; better now, while the ceremony is still on.
    ok, detail = check_proof(signed, signer.public_key(), proof_purpose=DELEGATION)
    if not ok:
        err_console.print(f"[red]The root's signature does not verify: {_plain(detail)}[/red]")
        raise typer.Exit(code=1)
    _write(out, signed)

    console.print("[green]Delegation signed.[/green]")
    console.print(f"  issuer      : {_plain(issuer)}")
    console.print(f"  root key    : {_plain(method)} ({fingerprint(signer.public_key())})")
    console.print(f"  issuing key : {_plain(signed['delegate']['id'])} ({fingerprint(delegated)})")
    console.print(f"  valid       : {signed['validFrom']} → {signed['validUntil']}")
    console.print(f"  file        : {out}")
    console.print(
        "\n[dim]Give it to the signing machine: opaas sign --delegation "
        f"{out.name}. It is public; it holds no secret.[/dim]"
    )


RevokeDelegation = Annotated[
    Path | None,
    typer.Option("--delegation", help="Revoke the issuing key this delegation names."),
]
RevokeKey = Annotated[
    Path | None,
    typer.Option("--public-key", help="Revoke this public key (.pub.pem); needs --id."),
]
RevokeId = Annotated[
    str | None,
    typer.Option("--id", help="Verification-method id of the key to revoke. With "
                 "--public-key, the id it was used under; alone, a key of the root document "
                 "(a lost token)."),
]
Reason = Annotated[str, typer.Option("--reason", help="Why: 'compromise', 'lost token', …")]
ExistingList = Annotated[
    Path | None,
    typer.Option("--list", help="The current revocation list, to extend. It must verify."),
]
RevocationsOut = Annotated[
    Path | None,
    typer.Option("--out", "-o", help="List to write (default: --list, or revocations.json)."),
]


def revoke(
    root_did_document: RootDid,
    reason: Reason,
    delegation: RevokeDelegation = None,
    public_key: RevokeKey = None,
    key_id: RevokeId = None,
    existing: ExistingList = None,
    root_key: RootKey = None,
    passphrase: RootPassphrase = None,
    pkcs11_module: Pkcs11Module = None,
    key_label: KeyLabel = None,
    token_label: TokenLabel = None,
    out: RevocationsOut = None,
) -> None:
    """Adds a key to the root's signed revocation list (D65)."""
    document, signer, method = _root(
        root_did_document, root_key=root_key, passphrase=passphrase,
        pkcs11_module=pkcs11_module, key_label=key_label, token_label=token_label,
    )
    issuer = str(document.get("id") or "")
    try:
        revoked_id, multibase = _key_to_revoke(document, delegation, public_key, key_id)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None

    entries: list[dict[str, Any]] = []
    if existing is not None:
        try:
            current = read_json(existing)
        except (OSError, json.JSONDecodeError) as exc:
            err_console.print(f"[red]{_plain(exc)}[/red]")
            raise typer.Exit(code=2) from None
        if not _verifies_under(current, document, issuer):
            err_console.print(
                f"[red]{existing} is not a revocation list the root {_plain(issuer)} signed.[/red] "
                "Extending it would launder whatever it says under the root's signature."
            )
            raise typer.Exit(code=2)
        entries = list(current.get("revoked") or [])
    if any(entry.get("publicKeyMultibase") == multibase for entry in entries):
        console.print(f"[yellow]{_plain(revoked_id)} is already revoked;[/yellow] re-signing.")
    else:
        entries.append(revocation_entry(revoked_id, multibase, reason=reason))

    try:
        signed = sign_revocation_list(
            build_revocation_list(issuer, entries), signer, key_id=method
        )
    except (SignerError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    target = out or existing or Path("revocations.json")
    _write(target, signed)
    console.print(f"[green]Revoked[/green] {_plain(revoked_id)}")
    console.print(f"  list : {target} ({len(entries)} key(s), signed by {_plain(method)})")
    console.print(
        "\n[dim]Publish it beside did.json, and have verifiers install it with "
        "'opaas trust --add'. A verifier that never updates its copy never learns of this.[/dim]"
    )


def _key_to_revoke(
    root_document: dict[str, Any],
    delegation: Path | None,
    public_key: Path | None,
    key_id: str | None,
) -> tuple[str, str]:
    if delegation is not None:
        if public_key is not None:
            raise ValueError("Pass --delegation or --public-key, not both.")
        delegate = (read_json(delegation).get("delegate") or {})
        if not delegate.get("id") or not delegate.get("publicKeyMultibase"):
            raise ValueError(f"{delegation} names no delegated key.")
        public_key_from_multibase(str(delegate["publicKeyMultibase"]))
        return str(delegate["id"]), str(delegate["publicKeyMultibase"])
    if public_key is not None:
        if not key_id:
            raise ValueError("--public-key needs --id: the verification method it was used under.")
        return key_id, public_key_multibase(load_public_key(public_key))
    if key_id:
        for method in root_document.get("verificationMethod") or []:
            if method.get("id") == key_id and method.get("publicKeyMultibase"):
                return key_id, str(method["publicKeyMultibase"])
        raise ValueError(f"The root document lists no key {key_id!r}.")
    raise ValueError("Name the key to revoke: --delegation, --public-key with --id, or --id.")


def _verifies_under(listed: Any, root_document: dict[str, Any], issuer: str) -> bool:
    if not is_revocation_list(listed) or listed.get("issuer") != issuer:
        return False
    signed_by = (listed.get("proof") or {}).get("verificationMethod")
    for method in root_document.get("verificationMethod") or []:
        if method.get("id") == signed_by and method.get("publicKeyMultibase"):
            key = public_key_from_multibase(str(method["publicKeyMultibase"]))
            return check_proof(listed, key, proof_purpose=DELEGATION)[0]
    return False
