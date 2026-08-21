import json

import pytest

from opaas.credential import (
    build_did_document,
    fingerprint,
    generate_root_keypair,
    load_private_key,
    load_public_key,
    public_key_multibase,
)


def test_generate_creates_keypair_and_did_document(tmp_path):
    paths = generate_root_keypair(tmp_path)
    assert paths.private_key.exists()
    assert paths.public_key.exists()

    document = json.loads(paths.did_document.read_text(encoding="utf-8"))
    assert document["id"] == "did:web:onpremialabs.com"
    method = document["verificationMethod"][0]
    assert method["id"] == "did:web:onpremialabs.com#key-1"
    assert method["type"] == "Ed25519VerificationKey2020"
    assert method["publicKeyMultibase"].startswith("z")


def test_private_key_signs_what_public_key_verifies(tmp_path):
    paths = generate_root_keypair(tmp_path)
    private_key = load_private_key(paths.private_key)
    public_key = load_public_key(paths.public_key)
    signature = private_key.sign(b"report")
    public_key.verify(signature, b"report")  # does not raise => the pair matches


def test_passphrase_protects_private_key(tmp_path):
    paths = generate_root_keypair(tmp_path, passphrase="a-long-passphrase")
    with pytest.raises(TypeError):
        load_private_key(paths.private_key)
    assert load_private_key(paths.private_key, "a-long-passphrase") is not None


def test_refuses_to_clobber_existing_root_key(tmp_path):
    generate_root_keypair(tmp_path)
    with pytest.raises(FileExistsError, match="would invalidate every credential"):
        generate_root_keypair(tmp_path)
    assert generate_root_keypair(tmp_path, overwrite=True) is not None


def test_multibase_encoding_has_ed25519_multicodec_prefix(tmp_path):
    paths = generate_root_keypair(tmp_path)
    encoded = public_key_multibase(load_public_key(paths.public_key))
    # 0xed01 + 32 bytes in base58btc takes 47-48 characters after the 'z'.
    assert encoded.startswith("z6Mk")
    assert 47 <= len(encoded) <= 49


def test_fingerprint_is_short_and_stable(tmp_path):
    public_key = load_public_key(generate_root_keypair(tmp_path).public_key)
    assert len(fingerprint(public_key)) == 16
    assert fingerprint(public_key) == fingerprint(public_key)


def test_did_document_honours_custom_did(tmp_path):
    public_key = load_public_key(generate_root_keypair(tmp_path).public_key)
    document = build_did_document(
        public_key, did="did:web:example.org", key_id="did:web:example.org#k"
    )
    assert document["assertionMethod"] == ["did:web:example.org#k"]
