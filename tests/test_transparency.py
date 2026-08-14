import json

import pytest

from opaas_bench.credential import CredentialError, build_credential
from opaas_bench.transparency import (
    GENESIS,
    TransparencyError,
    append_entry,
    credential_hash,
    find_entry,
    inclusion_check,
    load_log,
    verify_log,
)


def _credential(number: int) -> dict:
    return {
        "id": f"urn:opaas:cert:model-{number}:abc:2026-{number:03d}",
        "type": ["VerifiableCredential", "OpaasModelCertificate"],
        "credentialSubject": {"model": {"name": f"model-{number}"}},
        "proof": {"proofValue": f"zsignature{number}"},
    }


def _fill(path, count=3):
    return [append_entry(path, _credential(i), f"sha256:report{i}") for i in range(count)]


# -------------------------------------------------------------------- chain


def test_the_first_entry_points_at_genesis(tmp_path):
    entry = append_entry(tmp_path / "log.jsonl", _credential(0), "sha256:r0")
    assert entry.index == 0
    assert entry.previousHash == GENESIS
    assert entry.entryHash.startswith("sha256:")


def test_entries_chain_to_the_previous_one(tmp_path):
    entries = _fill(tmp_path / "log.jsonl")
    assert [e.index for e in entries] == [0, 1, 2]
    assert entries[1].previousHash == entries[0].entryHash
    assert entries[2].previousHash == entries[1].entryHash


def test_an_intact_log_verifies(tmp_path):
    path = tmp_path / "log.jsonl"
    entries = _fill(path)
    summary = verify_log(load_log(path))
    assert summary["intact"] is True
    assert summary["entries"] == 3
    assert summary["headHash"] == entries[-1].entryHash


def test_an_empty_log_is_valid_and_has_genesis_head(tmp_path):
    assert verify_log(load_log(tmp_path / "does-not-exist.jsonl")) == {
        "entries": 0,
        "headHash": GENESIS,
        "intact": True,
    }


# ---------------------------------------------------------------- tampering


def test_editing_a_past_entry_breaks_the_chain(tmp_path):
    """What the log provides: rewriting the past shows."""
    path = tmp_path / "log.jsonl"
    _fill(path)

    lines = path.read_text(encoding="utf-8").splitlines()
    altered = json.loads(lines[0])
    altered["reportHash"] = "sha256:another-report"
    lines[0] = json.dumps(altered)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(TransparencyError, match="altered hash"):
        verify_log(load_log(path))


def test_deleting_an_entry_breaks_the_chain(tmp_path):
    path = tmp_path / "log.jsonl"
    _fill(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")

    with pytest.raises(TransparencyError, match="the chain breaks"):
        verify_log(load_log(path))


def test_reordering_entries_breaks_the_chain(tmp_path):
    path = tmp_path / "log.jsonl"
    _fill(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[1], lines[0], lines[2]]) + "\n", encoding="utf-8")

    with pytest.raises(TransparencyError):
        verify_log(load_log(path))


def test_a_log_with_a_bom_still_loads(tmp_path):
    """It is shared and edited on Windows; a BOM cannot break verification."""
    path = tmp_path / "log.jsonl"
    _fill(path, 2)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert verify_log(load_log(path))["entries"] == 2


def test_a_malformed_line_points_at_the_line(tmp_path):
    path = tmp_path / "log.jsonl"
    _fill(path, 1)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{this is not json}\n")
    with pytest.raises(TransparencyError, match="log.jsonl:2"):
        load_log(path)


def test_appending_onto_a_broken_log_is_refused(tmp_path):
    path = tmp_path / "log.jsonl"
    _fill(path, 2)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(lines[1] + "\n", encoding="utf-8")

    with pytest.raises(TransparencyError):
        append_entry(path, _credential(9), "sha256:r9")


def test_reissuing_the_same_identifier_is_refused(tmp_path):
    path = tmp_path / "log.jsonl"
    append_entry(path, _credential(0), "sha256:r0")
    with pytest.raises(TransparencyError, match="is already in the log"):
        append_entry(path, _credential(0), "sha256:other")


# ---------------------------------------------------------------- inclusion


def test_a_registered_credential_is_found(tmp_path):
    path = tmp_path / "log.jsonl"
    _fill(path)
    included, detail = inclusion_check(load_log(path), _credential(1), "sha256:report1")
    assert included
    assert "entry 1" in detail


def test_an_unregistered_credential_is_flagged(tmp_path):
    path = tmp_path / "log.jsonl"
    _fill(path)
    included, detail = inclusion_check(load_log(path), _credential(99), "sha256:r99")
    assert not included
    assert "does NOT appear in the transparency log" in detail


def test_a_credential_modified_after_issuance_is_detected(tmp_path):
    """Same identifier, different content: that is substitution."""
    path = tmp_path / "log.jsonl"
    _fill(path)
    tampered = _credential(1)
    tampered["credentialSubject"]["model"]["name"] = "another-model"

    included, detail = inclusion_check(load_log(path), tampered, "sha256:report1")
    assert not included
    assert "modified it after issuance" in detail


def test_a_swapped_report_is_detected(tmp_path):
    path = tmp_path / "log.jsonl"
    _fill(path)
    included, detail = inclusion_check(load_log(path), _credential(1), "sha256:fake-report")
    assert not included
    assert "The logged report" in detail


def test_credential_hash_covers_the_proof(tmp_path):
    original = _credential(0)
    resigned = {**original, "proof": {"proofValue": "zanother-signature"}}
    assert credential_hash(original) != credential_hash(resigned)


def test_find_entry_returns_none_when_absent(tmp_path):
    path = tmp_path / "log.jsonl"
    _fill(path, 1)
    assert find_entry(load_log(path), "urn:opaas:cert:nonexistent") is None


# ---------------------------------------------------------- cloud baseline


def test_a_cloud_baseline_cannot_be_signed_as_a_certificate():
    """Signing it would claim to have audited an installation that was never measured."""
    report = {
        "runId": "20260814-001",
        "referenceType": "cloud-baseline",
        "model": {"name": "Vega-7B"},
        "configuration": {},
    }
    with pytest.raises(CredentialError, match="CLOUD REFERENCE"):
        build_credential(report, "sha256:x", issuer="did:web:x", key_id="did:web:x#key-1")


def test_an_onprem_report_signs_normally():
    report = {
        "runId": "20260814-001",
        "referenceType": "on-prem",
        "model": {"name": "Vega-7B"},
        "configuration": {},
        "coverage": {"complete": True, "blocksEvaluated": ["A"]},
    }
    credential = build_credential(
        report, "sha256:x", issuer="did:web:x", key_id="did:web:x#key-1"
    )
    assert credential["credentialSubject"]["evalRun"]["reportHash"] == "sha256:x"
