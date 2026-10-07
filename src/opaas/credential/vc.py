"""W3C verifiable credential: construction, signing and verification.

**Deliberate deviation from the design document.** The design proposed
`Ed25519Signature2020`, whose signature is computed over the RDF
canonicalization (URDNA2015) of the JSON-LD. That forces every verifier — in
any language — to drag along a full JSON-LD processor, and makes the
signature's validity depend on resolving remote contexts. Here
`DataIntegrityProof` is used with the `eddsa-jcs-2022` cryptosuite, which is
just as standard (W3C VC Data Integrity), signs over JCS (RFC 8785) and is
verified with SHA-256 + Ed25519 and nothing else.

For the stated goal — "anyone can validate the signature without depending on a
central server" — the difference is substantial.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from opaas.canonical import canonicalize
from opaas.credential.keys import b58decode, b58encode
from opaas.hashing import file_digest, short_hash, slugify

CREDENTIAL_CONTEXT = [
    "https://www.w3.org/ns/credentials/v2",
    "https://onpremialabs.com/credentials/v1",
]
CREDENTIAL_TYPE = ["VerifiableCredential", "OpaasModelCertificate"]
PROOF_TYPE = "DataIntegrityProof"
CRYPTOSUITE = "eddsa-jcs-2022"

#: 90 days (D28). A new driver, engine release or quantization changes the
#: result even when the model is the same, and on-prem stacks update faster
#: than the 6 months the design suggested.
DEFAULT_VALIDITY_DAYS = 90


class CredentialError(ValueError):
    """The credential cannot be built or is malformed."""


# ------------------------------------------------------------------ construction


def build_credential(
    report: dict[str, Any],
    report_hash: str,
    *,
    issuer: str,
    key_id: str,
    issued_at: datetime | None = None,
    validity_days: int = DEFAULT_VALIDITY_DAYS,
    report_url: str | None = None,
) -> dict[str, Any]:
    """Builds the unsigned credential from a report."""
    for required in ("runId", "model", "configuration"):
        if required not in report:
            raise CredentialError(f"The report is missing the required field '{required}'")

    if report.get("referenceType") == "cloud-baseline":
        raise CredentialError(
            "This report is a CLOUD REFERENCE, not the evaluation of a deployment. "
            "Signing it as a certificate would claim that an on-prem installation was "
            "audited when it was never measured. Use it with 'opaas compare' "
            "alongside the local run to see how much quality your deployment loses."
        )

    issued = issued_at or datetime.now(timezone.utc)
    model = report["model"]
    configuration = report["configuration"]
    coverage = report.get("coverage", {})

    return {
        "@context": CREDENTIAL_CONTEXT,
        "id": credential_id(report),
        "type": CREDENTIAL_TYPE,
        "issuer": issuer,
        "issuanceDate": _iso(issued),
        "expirationDate": _iso(issued + timedelta(days=validity_days)),
        "credentialSubject": {
            "model": {
                "name": model.get("name"),
                "weightsHash": (model.get("weightsHash") or {}).get("hash"),
                "source": model.get("source"),
                "quantization": model.get("quantization"),
            },
            "configuration": {
                "backend": configuration.get("backend"),
                "hardware": configuration.get("hardware"),
                "os": configuration.get("os"),
                "configHash": configuration.get("configHash"),
                # What was serving, not how OPAAS ran: a re-pulled model or an
                # upgraded engine changes this and leaves configHash alone (D34).
                "deploymentHash": (report.get("deploymentFingerprint") or {}).get("hash"),
            },
            "evalRun": {
                "runId": report.get("runId"),
                "opaasVersion": report.get("opaasVersion"),
                "reportHash": report_hash,
                "reportUrl": report_url,
                "status": report.get("status"),
                # Coverage travels in the credential: without this, a certificate
                # issued while measuring only speed would be indistinguishable from
                # one that passed all five blocks.
                "blocksEvaluated": coverage.get("blocksEvaluated", []),
                "blocksNotEvaluated": [
                    block.get("block") for block in coverage.get("blocksMissing", [])
                ],
                "completeEvaluation": bool(coverage.get("complete", False)),
            },
        },
        "proof": None,
    }


def credential_id(report: dict[str, Any]) -> str:
    """`urn:opaas:cert:<model>:<short-config-hash>:<run-id>`, as in the design."""
    model = slugify(str(report.get("model", {}).get("name", "unknown")))
    config = short_hash(str(report.get("configuration", {}).get("configHash", "")))
    return f"urn:opaas:cert:{model}:{config or 'nohash'}:{report.get('runId', 'norun')}"


def public_identifier(credential: dict[str, Any]) -> str:
    """Short identifier for badges and QR codes."""
    return str(credential.get("id", "")).replace("urn:opaas:cert:", "onpremialabs:cert:")


# ----------------------------------------------------------------------- signing


def sign_credential(
    credential: dict[str, Any],
    private_key: Ed25519PrivateKey,
    *,
    key_id: str,
    created: datetime | None = None,
) -> dict[str, Any]:
    """Returns the credential with its Data Integrity `proof`."""
    unsecured = {k: v for k, v in credential.items() if k != "proof"}
    proof_config = {
        "type": PROOF_TYPE,
        "cryptosuite": CRYPTOSUITE,
        "created": _iso(created or datetime.now(timezone.utc)),
        "verificationMethod": key_id,
        "proofPurpose": "assertionMethod",
        "@context": credential.get("@context", CREDENTIAL_CONTEXT),
    }
    signature = private_key.sign(_signing_input(unsecured, proof_config))

    proof = {k: v for k, v in proof_config.items() if k != "@context"}
    proof["proofValue"] = "z" + b58encode(signature)
    return {**unsecured, "proof": proof}


def _signing_input(unsecured: dict[str, Any], proof_config: dict[str, Any]) -> bytes:
    """VC Data Integrity signing input: H(config) || H(document)."""
    return hashlib.sha256(canonicalize(proof_config)).digest() + hashlib.sha256(
        canonicalize(unsecured)
    ).digest()


# ----------------------------------------------------------------- verification


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass(slots=True)
class VerificationResult:
    checks: list[Check] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return all(check.ok for check in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name=name, ok=ok, detail=detail))


def verify_credential(
    credential: dict[str, Any],
    public_key: Ed25519PublicKey,
    *,
    now: datetime | None = None,
    report_path: Path | None = None,
) -> VerificationResult:
    """Verifies a credential completely offline.

    Checks structure, cryptographic signature and validity period. If the
    report is supplied, it also checks that it is exactly the certified report.
    """
    result = VerificationResult()
    moment = now or datetime.now(timezone.utc)

    types = credential.get("type") or []
    result.add(
        "structure",
        "VerifiableCredential" in types and "OpaasModelCertificate" in types,
        f"type = {types}",
    )

    proof = credential.get("proof")
    if not isinstance(proof, dict):
        result.add("proof", False, "The credential has no proof: it is not signed")
        return result

    suite_ok = proof.get("type") == PROOF_TYPE and proof.get("cryptosuite") == CRYPTOSUITE
    result.add(
        "cryptosuite",
        suite_ok,
        f"{proof.get('type')} / {proof.get('cryptosuite')}"
        + ("" if suite_ok else f" (expected {PROOF_TYPE} / {CRYPTOSUITE})"),
    )
    if not suite_ok:
        return result

    result.add("signature", *_check_signature(credential, proof, public_key))
    result.add("validity", *_check_validity(credential, moment))

    if report_path is not None:
        result.add("report", *_check_report(credential, report_path))

    eval_run = credential.get("credentialSubject", {}).get("evalRun", {})
    if not eval_run.get("completeEvaluation", False):
        missing = eval_run.get("blocksNotEvaluated") or []
        result.warnings.append(
            "The credential is authentic but the evaluation is NOT complete: "
            f"unevaluated blocks {', '.join(missing) or 'unknown'}. "
            f"Evaluated blocks: {', '.join(eval_run.get('blocksEvaluated') or []) or 'none'}."
        )
    return result


def _check_signature(
    credential: dict[str, Any], proof: dict[str, Any], public_key: Ed25519PublicKey
) -> tuple[bool, str]:
    proof_value = str(proof.get("proofValue", ""))
    if not proof_value.startswith("z"):
        return False, "proofValue is not in base58btc multibase ('z...')"
    try:
        signature = b58decode(proof_value[1:])
    except ValueError as exc:
        return False, f"unreadable proofValue: {exc}"

    unsecured = {k: v for k, v in credential.items() if k != "proof"}
    proof_config = {k: v for k, v in proof.items() if k != "proofValue"}
    proof_config["@context"] = credential.get("@context", CREDENTIAL_CONTEXT)

    try:
        public_key.verify(signature, _signing_input(unsecured, proof_config))
    except InvalidSignature:
        return False, "The signature does NOT match this key or the document was altered"
    return True, f"valid Ed25519 ({proof.get('verificationMethod')})"


def _check_validity(credential: dict[str, Any], now: datetime) -> tuple[bool, str]:
    try:
        issued = _parse(credential.get("issuanceDate"))
        expires = _parse(credential.get("expirationDate"))
    except (TypeError, ValueError) as exc:
        return False, f"unreadable dates: {exc}"

    if now < issued:
        return False, f"Not valid yet (issued on {_iso(issued)})"
    if now >= expires:
        return False, f"EXPIRED on {_iso(expires)}: requires re-certification"
    remaining = (expires - now).days
    return True, f"Valid until {_iso(expires)} ({remaining} days remaining)"


def _check_report(credential: dict[str, Any], report_path: Path) -> tuple[bool, str]:
    declared = credential.get("credentialSubject", {}).get("evalRun", {}).get("reportHash")
    if not declared:
        return False, "The credential does not declare a reportHash"
    if not report_path.exists():
        return False, f"Report {report_path} does not exist"
    actual = file_digest(report_path)
    if actual != declared:
        return False, f"The report is NOT the certified one: {actual} != {declared}"
    return True, f"The report matches the certified one ({declared})"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"Date missing or not a string: {value!r}")
    # `fromisoformat` only learned to read the 'Z' suffix in 3.11, and every
    # date we emit ends in one (`_iso`). Normalising it here keeps a credential
    # verifiable on 3.10 — refusing to parse our own output would be absurd.
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
