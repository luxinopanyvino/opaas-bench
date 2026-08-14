import json
from datetime import UTC, datetime, timedelta

import pytest

from opaas_bench.credential import (
    CredentialError,
    TrustAnchorError,
    build_credential,
    generate_root_keypair,
    load_private_key,
    load_public_key,
    public_identifier,
    resolve_public_key,
    sign_credential,
    verify_credential,
)

ISSUER = "did:web:onpremialabs.com"
KEY_ID = f"{ISSUER}#key-1"

REPORT = {
    "runId": "20260806-001",
    "opaasBenchVersion": "1.0-draft",
    "status": "evaluated-partial",
    "model": {
        "name": "Llama-3.3-70B-Instruct",
        "source": "meta-llama/Llama-3.3-70B-Instruct",
        "quantization": "GGUF Q4_K_M",
        "weightsHash": {"available": True, "hash": "sha256:abc"},
    },
    "configuration": {
        "backend": "vLLM 0.9.1",
        "hardware": "1x RTX 4090 24GB",
        "os": "Ubuntu 24.04",
        "configHash": "sha256:1a3c5e7f9b",
    },
    "coverage": {
        "complete": False,
        "blocksEvaluated": ["A"],
        "blocksMissing": [{"block": "B"}, {"block": "C"}, {"block": "D"}, {"block": "E"}],
    },
}


@pytest.fixture()
def keypair(tmp_path):
    paths = generate_root_keypair(tmp_path / "keys")
    return load_private_key(paths.private_key), load_public_key(paths.public_key), paths


def _signed(keypair, report=None, **build_kwargs):
    private_key, _, _ = keypair
    credential = build_credential(
        report or REPORT, "sha256:deadbeef", issuer=ISSUER, key_id=KEY_ID, **build_kwargs
    )
    return sign_credential(credential, private_key, key_id=KEY_ID)


def test_credential_follows_the_designed_shape(keypair):
    credential = _signed(keypair)
    assert credential["type"] == ["VerifiableCredential", "OpaasModelCertificate"]
    assert credential["issuer"] == ISSUER
    assert credential["id"] == "urn:opaas:cert:llama-3.3-70b-instruct:1a3c5e7f:20260806-001"
    assert public_identifier(credential).startswith("onpremialabs:cert:")

    subject = credential["credentialSubject"]
    assert subject["model"]["quantization"] == "GGUF Q4_K_M"
    assert subject["configuration"]["backend"] == "vLLM 0.9.1"
    assert subject["evalRun"]["reportHash"] == "sha256:deadbeef"


def test_default_validity_is_about_six_months(keypair):
    credential = _signed(keypair)
    issued = datetime.fromisoformat(credential["issuanceDate"])
    expires = datetime.fromisoformat(credential["expirationDate"])
    assert 180 <= (expires - issued).days <= 185


def test_report_without_run_id_is_rejected():
    with pytest.raises(CredentialError, match="runId"):
        build_credential(
            {"model": {}, "configuration": {}}, "sha256:x", issuer=ISSUER, key_id=KEY_ID
        )


def test_signature_verifies(keypair):
    _, public_key, _ = keypair
    result = verify_credential(_signed(keypair), public_key)
    assert result.valid
    assert any(check.name == "signature" and check.ok for check in result.checks)


def test_tampering_any_field_breaks_the_signature(keypair):
    _, public_key, _ = keypair
    credential = _signed(keypair)
    credential["credentialSubject"]["configuration"]["hardware"] = "8x H100"
    result = verify_credential(credential, public_key)
    assert not result.valid


def test_tampering_the_proof_metadata_breaks_the_signature(keypair):
    """The proof configuration is part of the signature, not just the document."""
    _, public_key, _ = keypair
    credential = _signed(keypair)
    credential["proof"]["created"] = "2020-01-01T00:00:00Z"
    assert not verify_credential(credential, public_key).valid


def test_another_key_does_not_verify(keypair, tmp_path):
    credential = _signed(keypair)
    other = load_public_key(generate_root_keypair(tmp_path / "others").public_key)
    assert not verify_credential(credential, other).valid


def test_expired_credential_is_invalid(keypair):
    _, public_key, _ = keypair
    credential = _signed(keypair, validity_days=1)
    result = verify_credential(credential, public_key, now=datetime.now(UTC) + timedelta(days=2))
    assert not result.valid
    assert any("EXPIRED" in check.detail for check in result.checks)


def test_unsigned_credential_is_rejected(keypair):
    _, public_key, _ = keypair
    credential = build_credential(REPORT, "sha256:x", issuer=ISSUER, key_id=KEY_ID)
    result = verify_credential(credential, public_key)
    assert not result.valid
    assert any(check.name == "proof" for check in result.checks)


def test_partial_coverage_travels_into_the_credential(keypair):
    _, public_key, _ = keypair
    credential = _signed(keypair)
    eval_run = credential["credentialSubject"]["evalRun"]
    assert eval_run["completeEvaluation"] is False
    assert eval_run["blocksEvaluated"] == ["A"]
    assert eval_run["blocksNotEvaluated"] == ["B", "C", "D", "E"]

    result = verify_credential(credential, public_key)
    assert result.valid  # authentic...
    assert any("NOT complete" in warning for warning in result.warnings)  # ...but partial


def test_complete_coverage_raises_no_warning(keypair):
    _, public_key, _ = keypair
    report = {**REPORT, "coverage": {"complete": True, "blocksEvaluated": list("ABCDE")}}
    result = verify_credential(_signed(keypair, report=report), public_key)
    assert result.valid
    assert result.warnings == []


def test_report_hash_mismatch_is_detected(keypair, tmp_path):
    _, public_key, _ = keypair
    report_file = tmp_path / "result.json"
    report_file.write_text(json.dumps({"other": "report"}), encoding="utf-8")
    result = verify_credential(_signed(keypair), public_key, report_path=report_file)
    assert not result.valid
    assert any("The report is NOT the certified one" in check.detail for check in result.checks)


def test_trust_anchor_resolution_prefers_explicit_key(keypair):
    _, _, paths = keypair
    _, origin = resolve_public_key(ISSUER, KEY_ID, key_path=paths.public_key)
    assert "key supplied" in origin


def test_trust_anchor_reads_a_did_document(keypair):
    _, public_key, paths = keypair
    resolved, origin = resolve_public_key(ISSUER, KEY_ID, did_document_path=paths.did_document)
    assert "DID document supplied" in origin
    assert resolved.public_bytes_raw() == public_key.public_bytes_raw()


def test_did_document_for_another_issuer_is_refused(keypair):
    _, _, paths = keypair
    with pytest.raises(TrustAnchorError, match="belongs to"):
        resolve_public_key("did:web:impostor.example", did_document_path=paths.did_document)


def test_installed_anchor_is_found(keypair, tmp_path, monkeypatch):
    _, _, paths = keypair
    anchors = tmp_path / "anchors"
    anchors.mkdir()
    (anchors / "onpremialabs.json").write_text(
        paths.did_document.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setenv("OPAAS_ANCHOR_DIR", str(anchors))
    _, origin = resolve_public_key(ISSUER, KEY_ID)
    assert "local anchor" in origin


def test_without_any_anchor_it_refuses_rather_than_trusting_the_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("OPAAS_ANCHOR_DIR", str(tmp_path / "empty"))
    with pytest.raises(TrustAnchorError, match="There is no trust anchor"):
        resolve_public_key("did:web:unknown.example")
