"""The root → issuing key → credential walk of D65, its revocation list, and the signers.

The root here is a `FakeSigner`: it signs like a hardware token, with the key
held inside and only `sign` crossing. No test touches a token, a PKCS#11
module or the network.
"""

import json
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner

from opaas.cli.main import app
from opaas.core.credential import (
    DELEGATION_FIELD,
    CredentialError,
    DelegationError,
    Pkcs11Signer,
    SignerError,
    TrustAnchorError,
    TrustRoot,
    build_credential,
    build_delegation,
    build_revocation_list,
    build_root_did_document,
    find_revocation_lists,
    identity_for_signing,
    public_key_multibase,
    resolve_trust,
    revocation_entry,
    sign_credential,
    sign_delegation,
    sign_revocation_list,
    verify_credential,
)
from opaas.core.credential.proof import sign_document

ROOT_DID = "did:web:onpremialabs.com"
ISSUING_ID = f"{ROOT_DID}#issuing-2026-10"
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
REPORT = {
    "runId": "20261001-001",
    "opaasVersion": "0.2.1",
    "status": "evaluated",
    "model": {"name": "fake/model-1b", "weightsHash": {"hash": "sha256:abc"}},
    "configuration": {"backend": "fake", "configHash": "sha256:1a3c5e7f9b"},
    "coverage": {"complete": True, "blocksEvaluated": ["A", "B", "C", "D", "E"]},
}

runner = CliRunner()


class FakeSigner:
    """A root key that behaves like one on a token: it signs, and never hands the key out."""

    def __init__(self) -> None:
        self._key = Ed25519PrivateKey.generate()
        self.signatures = 0

    def public_key(self):
        return self._key.public_key()

    def sign(self, data: bytes) -> bytes:
        self.signatures += 1
        return self._key.sign(data)


class Chain:
    """Two root tokens, one issuing key, and what they sign, all at fixed dates."""

    def __init__(self) -> None:
        self.token_a, self.token_b = FakeSigner(), FakeSigner()
        self.root_document = build_root_did_document(
            ROOT_DID, [self.token_a.public_key(), self.token_b.public_key()]
        )
        self.issuing = Ed25519PrivateKey.generate()

    def delegation(self, *, signer=None, key_id=f"{ROOT_DID}#key-2", start=None, days=180,
                   delegate=None, delegate_id=ISSUING_ID):
        start = start or NOW - timedelta(days=30)
        unsigned = build_delegation(
            ROOT_DID, (delegate or self.issuing).public_key(), valid_from=start,
            valid_until=start + timedelta(days=days), delegate_id=delegate_id,
        )
        return sign_delegation(unsigned, signer or self.token_b, key_id=key_id,
                               created=start)

    def credential(self, delegation=None, *, created=NOW, report_hash="sha256:deadbeef"):
        unsigned = build_credential(REPORT, report_hash, issuer=ROOT_DID, key_id=ISSUING_ID,
                                    issued_at=created)
        return sign_credential(unsigned, self.issuing, created=created,
                               delegation=delegation or self.delegation())

    def root(self) -> TrustRoot:
        return TrustRoot(
            keys=[(m["id"], key) for m, key in zip(
                self.root_document["verificationMethod"],
                [self.token_a.public_key(), self.token_b.public_key()], strict=True)],
            origin="test anchor", issuer=ROOT_DID,
        )

    def revocations(self, *keys, signer=None, key_id=f"{ROOT_DID}#key-1"):
        entries = [revocation_entry(key_id_, public_key_multibase(key), reason="compromise",
                                    revoked_at=NOW) for key_id_, key in keys]
        return sign_revocation_list(build_revocation_list(ROOT_DID, entries, updated=NOW),
                                    signer or self.token_a, key_id=key_id, created=NOW)


@pytest.fixture()
def chain():
    return Chain()


def _unsigned(issuer=ROOT_DID):
    return build_credential(REPORT, "sha256:x", issuer=issuer, key_id=ISSUING_ID, issued_at=NOW)


def _failed(result):
    return [check.name for check in result.checks if not check.ok]


def _names(result):
    return [check.name for check in result.checks]


# ------------------------------------------------------------------ the walk


def test_a_delegated_credential_verifies_from_the_root_through_its_delegation(chain):
    result = verify_credential(chain.credential(), chain.root(), now=NOW,
                               revocation_lists=[chain.revocations()])
    assert result.valid, result.checks
    assert _names(result) == [
        "structure", "cryptosuite", "anchor", "delegation", "delegation-validity",
        "revocation", "delegated-key", "signature", "validity",
    ]


def test_the_root_signs_from_its_token_and_never_hands_the_key_out(chain):
    before = chain.token_b.signatures
    chain.delegation()
    assert chain.token_b.signatures == before + 1
    assert not hasattr(chain.token_b, "private_bytes")


def test_either_root_token_can_sign_the_delegation(chain):
    """One token is the other's backup: both keys are in the anchor."""
    delegation = chain.delegation(signer=chain.token_a, key_id=f"{ROOT_DID}#key-1")
    assert verify_credential(chain.credential(delegation), chain.root(), now=NOW).valid


def test_a_self_delegated_key_is_refused(chain):
    """A delegation signed by a key no anchor lists is the credential vouching for itself."""
    impostor = FakeSigner()
    delegation = chain.delegation(signer=impostor, key_id=f"{ROOT_DID}#key-1")
    result = verify_credential(chain.credential(delegation), chain.root(), now=NOW)
    assert _failed(result) == ["delegation"]
    assert "signature" not in _names(result)


def test_a_delegation_signed_under_a_method_the_anchor_does_not_publish_is_refused(chain):
    delegation = chain.delegation(signer=FakeSigner(), key_id=f"{ROOT_DID}#key-9")
    result = verify_credential(chain.credential(delegation), chain.root(), now=NOW)
    assert _failed(result) == ["delegation"]
    assert "not a key of the anchor" in result.checks[-1].detail


def test_a_key_in_the_credential_with_no_delegation_is_refused(chain):
    """Without a delegation, the only key that counts is the anchor's (D4)."""
    unsigned = _unsigned()
    signed = sign_credential(unsigned, chain.issuing, key_id=ISSUING_ID, created=NOW)
    result = verify_credential(signed, chain.root(), now=NOW + timedelta(seconds=1))
    assert _failed(result) == ["signature"]


def test_an_altered_delegation_does_not_verify(chain):
    credential = chain.credential()
    credential[DELEGATION_FIELD]["validUntil"] = "2030-01-01T00:00:00Z"
    result = verify_credential(credential, chain.root(), now=NOW)
    assert "delegation" in _failed(result)


def test_a_credential_proof_cannot_be_replayed_as_a_delegation(chain):
    """The proof purpose is signed: a root signature for `assertionMethod` is not a delegation."""
    unsigned = build_delegation(ROOT_DID, chain.issuing.public_key(), delegate_id=ISSUING_ID,
                                valid_from=NOW - timedelta(days=1),
                                valid_until=NOW + timedelta(days=180))
    as_assertion = sign_document(unsigned, chain.token_b, key_id=f"{ROOT_DID}#key-2")
    result = verify_credential(chain.credential(as_assertion), chain.root(), now=NOW)
    assert _failed(result) == ["delegation"]
    assert "not 'capabilityDelegation'" in result.checks[-1].detail


def test_the_delegation_must_cover_the_proofs_creation(chain):
    delegation = chain.delegation(start=NOW - timedelta(days=200), days=180)
    credential = chain.credential(delegation, created=NOW - timedelta(days=100))
    # Sign-time check passed for that date; move the proof outside the window.
    credential["proof"]["created"] = "2026-09-30T00:00:00Z"
    result = verify_credential(credential, chain.root(), now=NOW)
    assert "delegation-validity" in _failed(result)


def test_a_credential_signed_inside_the_window_outlives_the_delegation(chain):
    """D65 checks the delegation at proof.created: the credential's own 90 days still run."""
    delegation = chain.delegation(start=NOW - timedelta(days=179), days=180)
    credential = chain.credential(delegation, created=NOW)
    later = NOW + timedelta(days=30)
    assert verify_credential(credential, chain.root(), now=later).valid


def test_the_proof_must_name_the_delegated_key(chain):
    delegation = chain.delegation()
    unsigned = _unsigned()
    unsigned[DELEGATION_FIELD] = delegation
    signed = sign_document(unsigned, chain.issuing, key_id=f"{ROOT_DID}#other", created=NOW)
    result = verify_credential(signed, chain.root(), now=NOW)
    assert _failed(result) == ["delegated-key"]


def test_a_delegation_cannot_nest_another(chain):
    """Depth is fixed at root → issuing key → credential: no path building."""
    inner = chain.delegation()
    outer = build_delegation(ROOT_DID, chain.issuing.public_key(), delegate_id=ISSUING_ID,
                             valid_from=NOW - timedelta(days=1),
                             valid_until=NOW + timedelta(days=180))
    outer[DELEGATION_FIELD] = inner
    outer = sign_delegation(outer, chain.token_b, key_id=f"{ROOT_DID}#key-2")
    unsigned = _unsigned()
    unsigned[DELEGATION_FIELD] = outer
    signed = sign_document(unsigned, chain.issuing, key_id=ISSUING_ID, created=NOW)
    result = verify_credential(signed, chain.root(), now=NOW)
    assert _failed(result) == ["delegation"]
    assert "depth is fixed" in result.checks[-1].detail


def test_a_delegation_for_another_issuer_is_refused(chain):
    other = build_delegation("did:web:other.example", chain.issuing.public_key(),
                             valid_from=NOW - timedelta(days=1),
                             valid_until=NOW + timedelta(days=180))
    other = sign_delegation(other, chain.token_b, key_id=f"{ROOT_DID}#key-2")
    unsigned = _unsigned()
    unsigned[DELEGATION_FIELD] = other
    signed = sign_document(unsigned, chain.issuing, key_id=other["delegate"]["id"], created=NOW)
    assert _failed(verify_credential(signed, chain.root(), now=NOW)) == ["delegation"]


def test_key_still_means_trust_this_key_directly(chain):
    """`--key` with the issuing key skips the root; with the root key, walks from it."""
    credential = chain.credential()
    direct = verify_credential(credential, chain.issuing.public_key(), now=NOW)
    assert direct.valid
    assert any("Revocation was not checked" in warning for warning in direct.warnings)

    from_root = verify_credential(credential, chain.token_b.public_key(), now=NOW)
    assert from_root.valid
    assert "delegation-validity" in _names(from_root)

    stranger = verify_credential(credential, Ed25519PrivateKey.generate().public_key(), now=NOW)
    assert _failed(stranger) == ["delegation"]


def test_credentials_without_a_delegation_verify_as_before(chain):
    """A credential signed directly by the anchor's key: same checks as before D65."""
    unsigned = _unsigned()
    signed = sign_credential(unsigned, chain.token_a, key_id=f"{ROOT_DID}#key-1", created=NOW)
    result = verify_credential(signed, chain.root(), now=NOW + timedelta(seconds=1))
    assert result.valid
    assert _names(result) == ["structure", "cryptosuite", "anchor", "signature", "validity"]
    assert _names(verify_credential(signed, chain.token_a.public_key(),
                                    now=NOW + timedelta(seconds=1))) == [
        "structure", "cryptosuite", "signature", "validity",
    ]


def test_the_anchor_must_be_the_credentials_issuer(chain):
    root = chain.root()
    root.issuer = "did:web:someone-else.example"
    assert _failed(verify_credential(chain.credential(), root, now=NOW)) == ["anchor"]


# --------------------------------------------------------------- revocation


def test_a_revoked_issuing_key_is_refused_whatever_the_proof_date(chain):
    credential = chain.credential()
    revoked = chain.revocations((ISSUING_ID, chain.issuing.public_key()))
    result = verify_credential(credential, chain.root(), now=NOW, revocation_lists=[revoked])
    assert _failed(result) == ["revocation"]
    assert "REVOKED" in result.checks[-1].detail
    assert "signature" not in _names(result)


def test_a_revoked_root_key_takes_its_delegations_with_it(chain):
    """A lost token: delegations its key signed stop verifying, the other token's still do."""
    revoked = chain.revocations((f"{ROOT_DID}#key-2", chain.token_b.public_key()))
    lost = verify_credential(chain.credential(), chain.root(), now=NOW,
                             revocation_lists=[revoked])
    assert _failed(lost) == ["revocation"]
    kept = chain.credential(chain.delegation(signer=chain.token_a, key_id=f"{ROOT_DID}#key-1"))
    assert verify_credential(kept, chain.root(), now=NOW, revocation_lists=[revoked]).valid


def test_a_revocation_list_the_root_did_not_sign_fails_rather_than_being_skipped(chain):
    forged = chain.revocations(signer=FakeSigner())
    result = verify_credential(chain.credential(), chain.root(), now=NOW,
                               revocation_lists=[forged])
    assert _failed(result) == ["revocation"]


def test_without_a_revocation_list_the_gap_is_declared(chain):
    result = verify_credential(chain.credential(), chain.root(), now=NOW)
    assert result.valid
    revocation = next(check for check in result.checks if check.name == "revocation")
    assert "would go undetected" in revocation.detail
    assert any("revocation was not checked" in warning for warning in result.warnings)


def test_revocation_lists_are_found_beside_the_anchors_never_in_the_credential(
    chain, tmp_path, monkeypatch
):
    anchors = tmp_path / "anchors"
    monkeypatch.setenv("OPAAS_ANCHOR_DIR", str(anchors))
    (anchors / "revocations").mkdir(parents=True)
    listed = chain.revocations((ISSUING_ID, chain.issuing.public_key()))
    (anchors / "revocations" / "root.json").write_text(json.dumps(listed), encoding="utf-8")
    (anchors / "root.json").write_text(json.dumps(chain.root_document), encoding="utf-8")

    found = find_revocation_lists(ROOT_DID)
    assert [document for document, _ in found] == [listed]
    assert find_revocation_lists("did:web:other.example") == []
    # The anchor listing never mistakes a list for a DID document.
    assert resolve_trust(ROOT_DID).issuer == ROOT_DID

    elsewhere = tmp_path / "other.json"
    elsewhere.write_text(json.dumps({**listed, "issuer": "did:web:x.example"}), encoding="utf-8")
    with pytest.raises(TrustAnchorError, match="belongs to"):
        find_revocation_lists(ROOT_DID, path=elsewhere)


def test_an_anchor_with_two_root_keys_resolves_both(chain, tmp_path):
    path = tmp_path / "did.json"
    path.write_text(json.dumps(chain.root_document), encoding="utf-8")
    root = resolve_trust(ROOT_DID, did_document_path=path)
    assert [key_id for key_id, _ in root.keys] == [f"{ROOT_DID}#key-1", f"{ROOT_DID}#key-2"]
    assert root.exact(f"{ROOT_DID}#key-3") is None


# ------------------------------------------------------------------- signing


def test_signing_refuses_an_expired_delegation(chain):
    expired = chain.delegation(start=NOW - timedelta(days=200), days=180)
    unsigned = _unsigned()
    with pytest.raises(CredentialError, match="does not cover"):
        sign_credential(unsigned, chain.issuing, delegation=expired, created=NOW)


def test_signing_refuses_a_delegation_for_another_key(chain):
    other = chain.delegation(delegate=Ed25519PrivateKey.generate())
    unsigned = _unsigned()
    with pytest.raises(CredentialError, match="for another key"):
        sign_credential(unsigned, chain.issuing, delegation=other, created=NOW)


def test_signing_refuses_an_unsigned_delegation(chain):
    unsigned_delegation = build_delegation(
        ROOT_DID, chain.issuing.public_key(), valid_from=NOW, valid_until=NOW + timedelta(days=99)
    )
    with pytest.raises(DelegationError, match="not signed"):
        identity_for_signing(unsigned_delegation, chain.issuing.public_key(), at=NOW)


def test_signing_refuses_an_issuer_other_than_the_delegations(chain):
    unsigned = _unsigned(issuer="did:key:z6Mk")
    with pytest.raises(CredentialError, match="as issuer"):
        sign_credential(unsigned, chain.issuing, delegation=chain.delegation(), created=NOW)


def test_a_delegated_key_id_lives_under_the_issuer(chain):
    with pytest.raises(DelegationError, match="not under"):
        build_delegation(ROOT_DID, chain.issuing.public_key(), valid_from=NOW,
                         valid_until=NOW + timedelta(days=180),
                         delegate_id="did:web:elsewhere.example#k")


def test_a_root_document_refuses_one_key_on_two_tokens(chain):
    with pytest.raises(ValueError, match="twice"):
        build_root_did_document(ROOT_DID, [chain.token_a.public_key()] * 2)


# ------------------------------------------------------------- PKCS#11 signer


def _fake_pkcs11(monkeypatch, private_key, *, der=True):
    """A stand-in for python-pkcs11 exposing only what Pkcs11Signer calls."""
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    point = b"\x04\x20" + raw if der else raw
    module = types.ModuleType("pkcs11")
    module.KeyType = types.SimpleNamespace(EC_EDWARDS="ec-edwards")
    module.ObjectClass = types.SimpleNamespace(PRIVATE_KEY="private", PUBLIC_KEY="public")
    module.Attribute = types.SimpleNamespace(EC_POINT="ec-point")
    module.Mechanism = types.SimpleNamespace(EDDSA="eddsa")
    opened = {}

    class Key:
        def __init__(self, object_class):
            self.object_class = object_class

        def __getitem__(self, attribute):
            assert attribute == "ec-point"
            return point

        def sign(self, data, mechanism):
            assert mechanism == "eddsa"
            return private_key.sign(data)

    class Session:
        def get_key(self, object_class, key_type, label):
            assert key_type == "ec-edwards" and label == "root-a"
            return Key(object_class)

        def close(self):
            opened["closed"] = True

    class Token:
        def open(self, user_pin):
            opened["pin"] = user_pin
            return Session()

    class Library:
        def get_token(self, token_label):
            opened["token"] = token_label
            return Token()

    module.lib = lambda path: (opened.setdefault("module", path), Library())[1]
    monkeypatch.setitem(sys.modules, "pkcs11", module)
    return opened


@pytest.mark.parametrize("der", [True, False])
def test_a_pkcs11_token_signs_a_delegation_that_verifies(chain, monkeypatch, der):
    token_key = Ed25519PrivateKey.generate()
    opened = _fake_pkcs11(monkeypatch, token_key, der=der)
    signer = Pkcs11Signer(module="libykcs11.so", key_label="root-a", pin="123456",
                          token_label="YubiKey A").open()
    assert public_key_multibase(signer.public_key()) == public_key_multibase(
        token_key.public_key()
    )
    assert opened == {"module": "libykcs11.so", "token": "YubiKey A", "pin": "123456"}
    assert "123456" not in repr(signer)

    root_document = build_root_did_document(ROOT_DID, [signer.public_key()])
    delegation = build_delegation(ROOT_DID, chain.issuing.public_key(), delegate_id=ISSUING_ID,
                                  valid_from=NOW - timedelta(days=1),
                                  valid_until=NOW + timedelta(days=180))
    delegation = sign_delegation(delegation, signer, key_id=f"{ROOT_DID}#key-1")
    root = TrustRoot(keys=[(root_document["verificationMethod"][0]["id"], signer.public_key())],
                     origin="token", issuer=ROOT_DID)
    assert verify_credential(chain.credential(delegation), root, now=NOW).valid


def test_without_the_token_extra_the_signer_says_how_to_get_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "pkcs11", None)
    with pytest.raises(SignerError, match="'token' extra"):
        Pkcs11Signer(module="x.so", key_label="root-a", pin="0").open()


# ----------------------------------------------------------------------- CLI


def _write_pem(path, private_key, *, public=False):
    if public:
        data = private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    else:
        data = private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    path.write_bytes(data)
    return path


def _ceremony(tmp_path):
    """Root and issuing keys as files: the CLI's file signer stands in for the token."""
    root_key = Ed25519PrivateKey.generate()
    issuing = Ed25519PrivateKey.generate()
    root_did = tmp_path / "root" / "did.json"
    root_did.parent.mkdir()
    root_did.write_text(json.dumps(build_root_did_document(ROOT_DID, [root_key.public_key()])),
                        encoding="utf-8")
    keys = tmp_path / "issuing"
    keys.mkdir()
    return {
        "root_did": root_did,
        "root_pem": _write_pem(tmp_path / "root" / "root.pem", root_key),
        "issuing_pem": _write_pem(keys / "issuing.pem", issuing),
        "issuing_pub": _write_pem(keys / "issuing.pub.pem", issuing, public=True),
        "report": _report(tmp_path),
    }


def _report(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(REPORT), encoding="utf-8")
    return path


def _delegate(files, tmp_path, *extra):
    return runner.invoke(app, [
        "delegate", "--issuing-key", str(files["issuing_pub"]),
        "--root-did-document", str(files["root_did"]), "--root-key", str(files["root_pem"]),
        "--id", ISSUING_ID, "--out", str(tmp_path / "delegation.json"), *extra,
    ])


def _sign(files, tmp_path):
    return runner.invoke(app, [
        "sign", "--input", str(files["report"]), "--key", str(files["issuing_pem"]),
        "--delegation", str(tmp_path / "delegation.json"),
        "--out", str(tmp_path / "credential.json"),
    ])


def test_delegate_sign_verify_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("OPAAS_ANCHOR_DIR", str(tmp_path / "anchors"))
    files = _ceremony(tmp_path)
    delegated = _delegate(files, tmp_path)
    assert delegated.exit_code == 0, delegated.output

    signed = _sign(files, tmp_path)
    assert signed.exit_code == 0, signed.output
    credential = json.loads((tmp_path / "credential.json").read_text(encoding="utf-8"))
    assert credential["issuer"] == ROOT_DID
    assert credential["proof"]["verificationMethod"] == ISSUING_ID
    assert credential[DELEGATION_FIELD]["delegate"]["id"] == ISSUING_ID

    # No anchor: refused, even though the credential carries a key and a delegation.
    refused = runner.invoke(app, ["verify", "--credential", str(tmp_path / "credential.json")])
    assert refused.exit_code == 2

    runner.invoke(app, ["trust", "--add", str(files["root_did"]), "--yes"])
    verified = runner.invoke(app, ["verify", "--credential", str(tmp_path / "credential.json"),
                                   "--report", str(files["report"])])
    assert verified.exit_code == 0, verified.output
    assert "delegation-validity" in verified.output

    revoked = runner.invoke(app, [
        "revoke", "--root-did-document", str(files["root_did"]),
        "--root-key", str(files["root_pem"]), "--delegation", str(tmp_path / "delegation.json"),
        "--reason", "compromise", "--out", str(tmp_path / "revocations.json"),
    ])
    assert revoked.exit_code == 0, revoked.output
    installed = runner.invoke(app, ["trust", "--add", str(tmp_path / "revocations.json"),
                                    "--yes"])
    assert installed.exit_code == 0, installed.output
    assert list((tmp_path / "anchors" / "revocations").glob("*.json"))

    after = runner.invoke(app, ["verify", "--credential", str(tmp_path / "credential.json")])
    assert after.exit_code == 1
    assert "REVOKED" in after.output

    # The site carries the same list, so the browser refuses what the CLI refuses.
    published = runner.invoke(app, [
        "publish", "--report", str(files["report"]),
        "--credential", str(tmp_path / "credential.json"),
        "--did-document", str(files["root_did"]),
        "--revocations", str(tmp_path / "revocations.json"), "--out", str(tmp_path / "site"),
    ])
    assert published.exit_code == 0, published.output
    assert (tmp_path / "site" / ".well-known" / "opaas-revocations.json").is_file()
    page = next((tmp_path / "site" / "cert").glob("*/index.html")).read_text(encoding="utf-8")
    assert '<script id="revocations-data"' in page


def test_delegate_refuses_a_delegation_shorter_than_a_credential(tmp_path):
    files = _ceremony(tmp_path)
    result = _delegate(files, tmp_path, "--valid-days", "60")
    assert result.exit_code == 2
    assert not (tmp_path / "delegation.json").exists()


def test_delegate_refuses_a_root_key_its_document_does_not_publish(tmp_path):
    files = _ceremony(tmp_path)
    _write_pem(files["root_pem"], Ed25519PrivateKey.generate())
    result = _delegate(files, tmp_path)
    assert result.exit_code == 2
    assert "does not list the key" in " ".join(result.output.split())


def test_sign_refuses_an_expired_delegation(tmp_path):
    files = _ceremony(tmp_path)
    _delegate(files, tmp_path, "--valid-from", "2020-01-01T00:00:00Z")
    result = _sign(files, tmp_path)
    assert result.exit_code == 2
    assert not (tmp_path / "credential.json").exists()


def test_revoke_will_not_extend_a_list_the_root_did_not_sign(tmp_path):
    files = _ceremony(tmp_path)
    forged = Chain().revocations()
    forged["issuer"] = ROOT_DID
    (tmp_path / "forged.json").write_text(json.dumps(forged), encoding="utf-8")
    result = runner.invoke(app, [
        "revoke", "--root-did-document", str(files["root_did"]),
        "--root-key", str(files["root_pem"]), "--id", f"{ROOT_DID}#key-1",
        "--reason", "lost token", "--list", str(tmp_path / "forged.json"),
    ])
    assert result.exit_code == 2
