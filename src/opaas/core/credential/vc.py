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

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from opaas.core.credential.delegation import (
    DELEGATION_FIELD,
    DelegationError,
    covers,
    delegation_problem,
    identity_for_signing,
)
from opaas.core.credential.keys import public_key_from_multibase, public_key_multibase
from opaas.core.credential.proof import (
    CREDENTIAL_CONTEXT,
    CRYPTOSUITE,
    DELEGATION,
    PROOF_TYPE,
    Signer,
    check_proof,
    sign_document,
)
from opaas.core.credential.proof import iso as _iso
from opaas.core.credential.proof import parse_time as _parse
from opaas.core.credential.trust import TrustRoot
from opaas.core.hashing import file_digest, short_hash, slugify

CREDENTIAL_TYPE = ["VerifiableCredential", "OpaasModelCertificate"]
#: One certificate for a whole system: every component, every dimension (D60).
SYSTEM_CREDENTIAL_TYPE = ["VerifiableCredential", "OpaasSystemCertificate"]
#: Everything else OPAAS signs: an inventory, a smith report, an inquest
#: audit, an Auditability Card, a deployment manifest. Same proof, same
#: verification, a different subject: the hash of one evidence document (D49).
EVIDENCE_TYPE = ["VerifiableCredential", "OpaasEvidence"]
EVIDENCE_KINDS = (
    "inventory",
    "gap-scan",
    "smith-report",
    "inquest-audit",
    "swarm-analysis",
    "sentinel-overhead",
    "auditability-card",
    "deployment-manifest",
    #: The system audit's report (D60): every member by hash, the measurements read
    #: from them, the composition declared and observed.
    "system-report",
)
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
                "quantizationSource": model.get("quantizationSource"),
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


def build_evidence_credential(
    document: dict[str, Any],
    document_hash: str,
    *,
    kind: str,
    issuer: str,
    issued_at: datetime | None = None,
    validity_days: int = DEFAULT_VALIDITY_DAYS,
) -> dict[str, Any]:
    """The unsigned credential over one evidence document, identified by its file hash.

    As for a report (D2), the subject is the hash of the document's bytes:
    verifying is `sha256sum` of the file received.
    """
    if kind not in EVIDENCE_KINDS:
        raise CredentialError(
            f"Unknown evidence kind '{kind}'. Known: {', '.join(EVIDENCE_KINDS)}."
        )
    if document.get("referenceType") == "cloud-baseline":
        raise CredentialError("A cloud baseline cannot be signed as evidence (D22).")
    issued = issued_at or datetime.now(timezone.utc)
    return {
        "@context": CREDENTIAL_CONTEXT,
        "id": f"urn:opaas:evidence:{kind}:{short_hash(document_hash, 16)}",
        "type": EVIDENCE_TYPE,
        "issuer": issuer,
        "issuanceDate": _iso(issued),
        "expirationDate": _iso(issued + timedelta(days=validity_days)),
        "credentialSubject": {
            "evidence": {
                "kind": kind,
                "documentHash": document_hash,
                "opaasVersion": document.get("opaasVersion"),
                "environmentFingerprint": document.get("environmentFingerprint"),
            }
        },
        "proof": None,
    }


def build_system_credential(
    report: dict[str, Any],
    report_hash: str,
    *,
    issuer: str,
    key_id: str,
    issued_at: datetime | None = None,
    validity_days: int = DEFAULT_VALIDITY_DAYS,
    report_url: str | None = None,
) -> dict[str, Any]:
    """The unsigned certificate over a system report (D60, phase S2).

    The subject is the system report's file hash, as for every OPAAS
    credential (invariant 4), plus what a reader must see without opening it:
    the components, the coverage table and whether the system is certifiable,
    with every reason when it is not. Like `completeEvaluation` on a model
    credential, a system that is not certifiable can still be signed: the
    credential then says so, and `verify` warns. A cloud baseline cannot be a
    component of a certified system at all (invariant 10).
    """
    if report.get("kind") != "system-report":
        raise CredentialError(
            "This is not a system report. Write one with `opaas audit run system.json`."
        )
    for member in report.get("members") or []:
        if member.get("referenceType") == "cloud-baseline":
            raise CredentialError(
                f"{member.get('file')} is a CLOUD REFERENCE: a system that contains one cannot "
                "be certified as an on-prem deployment (D22). Remove it from the members, or "
                "list the service as an external component."
            )
    system = report.get("system") or {}
    if not system.get("id"):
        raise CredentialError("The system report does not name its system.")
    certifiability = report.get("certifiability") or {}
    issued = issued_at or datetime.now(timezone.utc)
    return {
        "@context": CREDENTIAL_CONTEXT,
        "id": f"urn:opaas:system:{slugify(str(system['id']))}:{short_hash(report_hash)}",
        "type": SYSTEM_CREDENTIAL_TYPE,
        "issuer": issuer,
        "issuanceDate": _iso(issued),
        "expirationDate": _iso(issued + timedelta(days=validity_days)),
        "credentialSubject": {
            "system": {
                "id": system["id"],
                "purpose": system.get("purpose"),
                "manifestHash": system.get("manifestHash"),
                "reportHash": report_hash,
                "reportUrl": report_url,
                "opaasVersion": report.get("opaasVersion"),
                "specVersion": report.get("specVersion"),
            },
            "components": [
                {k: c[k] for k in ("id", "kind", "observed") if k in c}
                for c in (report.get("composition") or {}).get("components") or []
            ],
            "members": [
                {k: m[k] for k in ("component", "kind", "hash") if k in m}
                for m in report.get("members") or []
            ],
            # Per row and dimension, whether anything was measured: never how well.
            "coverage": report.get("coverage") or {},
            "certifiable": bool(certifiability.get("certifiable")),
            "notCertifiableBecause": list(certifiability.get("reasons") or []),
            "environmentFingerprint": report.get("environmentFingerprint"),
        },
        "proof": None,
    }


def credential_id(report: dict[str, Any]) -> str:
    """`urn:opaas:cert:<model>:<short-config-hash>:<run-id>`, as in the design."""
    model = slugify(str(report.get("model", {}).get("name", "unknown")))
    config = short_hash(str(report.get("configuration", {}).get("configHash", "")))
    return f"urn:opaas:cert:{model}:{config or 'nohash'}:{report.get('runId', 'norun')}"


def public_identifier(credential: dict[str, Any]) -> str:
    """Short identifier for badges, QR codes and the certificate's heading (D58).

    It is prefixed with the issuer's domain when the issuer is a `did:web`,
    and with the neutral `opaas:` otherwise. It used to read
    `onpremialabs:cert:` for every credential, so a certificate self-issued
    under a `did:key` said onpremIALabs had issued it: the name D39 removed
    from the issuer field had survived in the identifier.
    """
    identifier = str(credential.get("id", ""))
    kind = "system" if identifier.startswith("urn:opaas:system:") else "cert"
    suffix = identifier.removeprefix(f"urn:opaas:{kind}:")
    return f"{_issuer_domain(credential) or 'opaas'}:{kind}:{suffix}"


def _issuer_domain(credential: dict[str, Any]) -> str | None:
    issuer = credential.get("issuer")
    if isinstance(issuer, dict):
        issuer = issuer.get("id")
    if not isinstance(issuer, str) or not issuer.startswith("did:web:"):
        return None
    # did:web:example.com:path encodes a port as %3A; only the host names the issuer.
    host = issuer.removeprefix("did:web:").split(":", 1)[0]
    return unquote(host).split(":", 1)[0].lower() or None


# ----------------------------------------------------------------------- signing


def sign_credential(
    credential: dict[str, Any],
    private_key: Signer,
    *,
    key_id: str | None = None,
    created: datetime | None = None,
    delegation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Returns the credential with its Data Integrity `proof`.

    With a `delegation` (D65), the signer must be the key it delegates and
    the signature must fall inside its window: otherwise nothing is signed.
    The delegation is embedded in the signed body, and the proof names the
    delegated key, so the credential carries everything `verify` needs to
    walk from the root it already holds to this signature.
    """
    moment = created or datetime.now(timezone.utc)
    unsecured = {k: v for k, v in credential.items() if k not in ("proof", DELEGATION_FIELD)}
    if delegation is not None:
        try:
            issuer, delegated_id = identity_for_signing(
                delegation, private_key.public_key(), at=moment
            )
        except DelegationError as exc:
            raise CredentialError(str(exc)) from None
        if unsecured.get("issuer") != issuer:
            raise CredentialError(
                f"The credential names {unsecured.get('issuer')!r} as issuer but the delegation "
                f"is from {issuer!r}: it would not verify against that issuer's root."
            )
        if key_id is not None and key_id != delegated_id:
            raise CredentialError(
                f"The delegation names the key {delegated_id!r}, not {key_id!r}."
            )
        key_id = delegated_id
        unsecured[DELEGATION_FIELD] = delegation
    if key_id is None:
        raise CredentialError("No verification method to sign under: pass key_id or a delegation.")
    return sign_document(unsecured, private_key, key_id=key_id, created=moment)


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
    trust: Ed25519PublicKey | TrustRoot,
    *,
    now: datetime | None = None,
    report_path: Path | None = None,
    revocation_lists: list[dict[str, Any]] | None = None,
) -> VerificationResult:
    """Verifies a credential completely offline.

    `trust` is where verification starts: a bare public key (trusted
    directly, as `--key`) or a `TrustRoot` resolved from an anchor. From it,
    the key that signed the credential is reached either directly or through
    the embedded delegation (D65), with a fixed depth of one. Then the
    signature and the validity period are checked and, if the report is
    supplied, that it is exactly the certified report.

    The browser verifier (`web/assets/verify.js`) walks the same chain with
    the same check names, and a test pins that both reach the same verdict
    on the same credentials.
    """
    result = VerificationResult()
    moment = now or datetime.now(timezone.utc)
    root = trust if isinstance(trust, TrustRoot) else TrustRoot.direct(trust)

    types = credential.get("type") or []
    is_evidence = "OpaasEvidence" in types
    is_system = "OpaasSystemCertificate" in types
    result.add(
        "structure",
        "VerifiableCredential" in types
        and ("OpaasModelCertificate" in types or is_evidence or is_system),
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

    signing_key = _walk(credential, proof, root, revocation_lists or [], result)
    if signing_key is None:
        return result

    result.add("signature", *check_proof(credential, signing_key))
    result.add("validity", *_check_validity(credential, moment))

    if report_path is not None:
        result.add("report", *_check_report(credential, report_path))
    if is_evidence:
        return result
    if is_system:
        subject = credential.get("credentialSubject", {})
        if not subject.get("certifiable"):
            reasons = subject.get("notCertifiableBecause") or ["no reason recorded"]
            result.warnings.append(
                "The credential is authentic but the system is NOT certifiable: "
                + " ".join(str(r) for r in reasons)
            )
        return result

    eval_run = credential.get("credentialSubject", {}).get("evalRun", {})
    if not eval_run.get("completeEvaluation", False):
        missing = eval_run.get("blocksNotEvaluated") or []
        result.warnings.append(
            "The credential is authentic but the evaluation is NOT complete: "
            f"unevaluated blocks {', '.join(missing) or 'unknown'}. "
            f"Evaluated blocks: {', '.join(eval_run.get('blocksEvaluated') or []) or 'none'}."
        )
    return result


def _walk(
    credential: dict[str, Any],
    proof: dict[str, Any],
    root: TrustRoot,
    revocation_lists: list[dict[str, Any]],
    result: VerificationResult,
) -> Ed25519PublicKey | None:
    """From the root to the key that signed the credential, or None after a failed step.

    A failed step is recorded and ends the walk: per D65, the credential's
    own signature is checked only once its key has been reached.
    """
    issuer = credential.get("issuer")
    method = proof.get("verificationMethod")
    if root.issuer is not None:
        ok = root.issuer == issuer
        result.add(
            "anchor",
            ok,
            f"{root.issuer} ({root.origin})"
            if ok
            else f"the anchor is {root.issuer!r}, the credential names {issuer!r}",
        )
        if not ok:
            return None

    delegation = credential.get(DELEGATION_FIELD)
    if delegation is None:
        key = root.for_proof(method)
        if revocation_lists and not _check_revocation(
            revocation_lists, root, issuer, [key], result
        ):
            return None
        return key

    if problem := delegation_problem(delegation):
        result.add("delegation", False, f"malformed delegation: {problem}")
        return None
    delegate = delegation["delegate"]
    try:
        delegated_key = public_key_from_multibase(str(delegate["publicKeyMultibase"]))
    except ValueError as exc:
        result.add("delegation", False, f"unreadable delegated key: {exc}")
        return None

    if root.is_direct and public_key_multibase(root.keys[0][1]) == delegate["publicKeyMultibase"]:
        # --key names the issuing key itself: the verifier vouched for it, so
        # the root is not needed, and neither is a list only the root can sign.
        result.add("delegation", True, "not walked: the issuing key was supplied with --key")
        result.warnings.append(
            "Revocation was not checked: the issuing key was trusted directly with --key, "
            "and only the root can vouch for a revocation list."
        )
        return delegated_key if _check_delegated_key(method, delegate, result) else None

    if delegation.get("issuer") != issuer:
        result.add(
            "delegation",
            False,
            f"the delegation is from {delegation.get('issuer')!r}, the credential names "
            f"{issuer!r}",
        )
        return None
    root_method = (delegation.get("proof") or {}).get("verificationMethod")
    root_key = root.exact(root_method)
    if root_key is None:
        result.add(
            "delegation",
            False,
            f"signed by {root_method!r}, which is not a key of the anchor ({root.origin}). A key "
            "no anchor delegates is not trusted, whatever the credential says about it.",
        )
        return None
    ok, detail = check_proof(delegation, root_key, proof_purpose=DELEGATION)
    result.add("delegation", ok, f"{delegate['id']} delegated by {root_method}: {detail}")
    if not ok:
        return None

    try:
        ok, detail = covers(delegation, _parse(proof.get("created")))
    except (TypeError, ValueError) as exc:
        ok, detail = False, f"unreadable proof.created: {exc}"
    result.add("delegation-validity", ok, detail)
    if not ok:
        return None

    if not _check_revocation(revocation_lists, root, issuer, [delegated_key, root_key], result):
        return None
    return delegated_key if _check_delegated_key(method, delegate, result) else None


def _check_delegated_key(method: Any, delegate: dict[str, Any], result: VerificationResult) -> bool:
    ok = method == delegate["id"]
    result.add(
        "delegated-key",
        ok,
        f"signed by {method}, the key the delegation names"
        if ok
        else f"signed by {method!r}, but the delegation names {delegate['id']!r}",
    )
    return ok


def _check_revocation(
    lists: list[dict[str, Any]],
    root: TrustRoot,
    issuer: Any,
    keys: list[Ed25519PublicKey],
    result: VerificationResult,
) -> bool:
    """None of `keys` is on a revocation list that verifies under the root.

    A list that does not verify fails the check rather than being skipped: a
    list someone placed among the anchors and the root did not sign is a
    problem to look at, not noise to step around.
    """
    if not lists:
        result.add(
            "revocation",
            True,
            f"no revocation list held for {issuer}: a revoked key would go undetected",
        )
        result.warnings.append(
            f"No revocation list for {issuer} was found, so revocation was not checked. "
            "Install the issuer's current list with 'opaas trust --add', or pass --revocations."
        )
        return True
    revoked: dict[str, dict[str, Any]] = {}
    for listed in lists:
        list_method = (listed.get("proof") or {}).get("verificationMethod")
        list_key = root.exact(list_method)
        if listed.get("issuer") != issuer or list_key is None:
            result.add(
                "revocation",
                False,
                f"a revocation list (updated {listed.get('updated')}) is not signed by a key of "
                f"the anchor ({list_method!r} for {listed.get('issuer')!r})",
            )
            return False
        ok, detail = check_proof(listed, list_key, proof_purpose=DELEGATION)
        if not ok:
            result.add(
                "revocation",
                False,
                f"the revocation list updated {listed.get('updated')} does not verify: {detail}",
            )
            return False
        for entry in listed.get("revoked") or []:
            if isinstance(entry, dict) and isinstance(entry.get("publicKeyMultibase"), str):
                revoked[entry["publicKeyMultibase"]] = entry
    for key in keys:
        if entry := revoked.get(public_key_multibase(key)):
            result.add(
                "revocation",
                False,
                f"{entry.get('id')} was REVOKED on {entry.get('revokedAt')} "
                f"({entry.get('reason') or 'no reason given'})",
            )
            return False
    updated = ", ".join(str(listed.get("updated")) for listed in lists)
    result.add("revocation", True, f"not revoked (list updated {updated})")
    return True


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
    subject = credential.get("credentialSubject", {})
    declared = (
        (subject.get("evalRun") or {}).get("reportHash")
        or (subject.get("system") or {}).get("reportHash")
        or (subject.get("evidence") or {}).get("documentHash")
    )
    if not declared:
        return False, "The credential does not declare a reportHash"
    if not report_path.exists():
        return False, f"Report {report_path} does not exist"
    actual = file_digest(report_path)
    if actual != declared:
        return False, f"The report is NOT the certified one: {actual} != {declared}"
    return True, f"The report matches the certified one ({declared})"
