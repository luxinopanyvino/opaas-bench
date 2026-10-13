"""`opaas evidence`: sign and verify any evidence document (D49).

A report is signed with `opaas sign`; everything else the modules produce —
inventories, gap scans, smith reports, inquest audits, cards, deployment
manifests — is signed here, with the same key handling and the same proof.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from opaas.cli.common import (
    _load_signing_key,
    _plain,
    _signing_identity,
    console,
    err_console,
    write_json,
)
from opaas.core.credential import (
    DEFAULT_VALIDITY_DAYS,
    CredentialError,
    TrustAnchorError,
    build_evidence_credential,
    resolve_public_key,
    sign_credential,
    verify_credential,
)
from opaas.core.evidence import EvidenceError, read_document
from opaas.core.hashing import file_digest
from opaas.core.transparency import TransparencyError, append_entry

app = typer.Typer(help="Sign and verify evidence documents (inventories, audits, cards).",
                  no_args_is_help=True)

Document = Annotated[Path, typer.Argument(help="Evidence document written by an OPAAS module.")]
Key = Annotated[Path, typer.Option("--key", "-k", help="Ed25519 private key to sign with.")]
Out = Annotated[Path | None, typer.Option("--out", "-o",
                                          help="Credential to write (default: <doc>.vc.json).")]
Issuer = Annotated[str | None, typer.Option("--issuer", help="Issuer DID; defaults to did.json.")]
Passphrase = Annotated[str | None, typer.Option("--passphrase", envvar="OPAAS_KEY_PASSPHRASE",
                                                help="Passphrase of the private key.")]
Validity = Annotated[int, typer.Option("--validity-days", help="Days of validity.")]
Log = Annotated[Path | None, typer.Option("--log", help="Transparency log to record it in.")]


@app.command("sign")
def sign_evidence(
    document: Document,
    key: Key,
    out: Out = None,
    issuer: Issuer = None,
    passphrase: Passphrase = None,
    validity_days: Validity = DEFAULT_VALIDITY_DAYS,
    log: Log = None,
) -> None:
    """Signs an evidence document: the credential certifies the hash of its bytes."""
    try:
        body = read_document(document)
        private_key = _load_signing_key(key, passphrase)
        issuer, key_id = _signing_identity(key, private_key, issuer)
        credential = build_evidence_credential(
            body, file_digest(document), kind=str(body.get("kind")), issuer=issuer,
            validity_days=validity_days,
        )
    except (EvidenceError, CredentialError, OSError, TypeError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    signed = sign_credential(credential, private_key, key_id=key_id)
    target = out or document.with_suffix(".vc.json")
    write_json(target, signed)
    if log is not None:
        try:
            append_entry(log, signed, file_digest(document))
        except TransparencyError as exc:
            err_console.print(f"[red]Could not record in the log:[/red] {exc}")
            raise typer.Exit(code=2) from None
    console.print(f"[green]Signed[/green] {body.get('kind')}: {signed['id']}")
    console.print(f"  document : {signed['credentialSubject']['evidence']['documentHash']}")
    console.print(f"  file     : {target}")


@app.command("verify")
def verify_evidence(
    document: Document,
    credential: Annotated[Path, typer.Option("--credential", "-c", help="Its credential.")],
    key: Annotated[Path | None, typer.Option("--key", "-k", help="Issuer public key.")] = None,
    did_document: Annotated[Path | None, typer.Option("--did-document")] = None,
) -> None:
    """Verifies the signature offline and that the document is the one signed."""
    try:
        signed = json.loads(credential.read_text(encoding="utf-8-sig"))
        proof = signed.get("proof") or {}
        public_key, _ = resolve_public_key(
            str(signed.get("issuer", "")), proof.get("verificationMethod"),
            key_path=key, did_document_path=did_document,
        )
    except (OSError, json.JSONDecodeError, TrustAnchorError, ValueError) as exc:
        err_console.print(f"[red]{_plain(exc)}[/red]")
        raise typer.Exit(code=2) from None
    result = verify_credential(signed, public_key, report_path=document)
    for check in result.checks:
        mark = "[green]ok  [/green]" if check.ok else "[red]FAIL[/red]"
        console.print(f"  {mark} {check.name}: {_plain(check.detail)}")
    if not result.valid:
        raise typer.Exit(code=1)
    console.print("[green]Valid.[/green]")
